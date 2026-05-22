# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Flask web app that snapshots a live website (HTML + every asset) into a self-contained zip viewable from `file://`. The hard part is not the download — it's making JS-heavy sites (Next.js, Aura/Webflow previews, GSAP, Unicorn Studio, Vite SPAs) keep working after they're divorced from their origin.

## Commands

```bash
# Local dev (Python 3.12, uv-managed)
uv sync                          # install deps from uv.lock
uv run playwright install chromium
uv run python app.py             # runs Flask on PORT (default 5002), debug=True

# Production (Railway/Render — Docker)
docker build -t grabber .
docker run -p 8080:8080 grabber  # entrypoint.sh launches gunicorn
```

There are no tests, no linter config, no build step. `requirements.txt` mirrors `pyproject.toml` because Railway's Dockerfile uses pip, not uv.

## Architecture

Two files do everything: `app.py` (web/session glue) and `grabber.py` (the capture pipeline).

### Request lifecycle (`app.py`)

1. `POST /start-download` → spawns a `_worker` thread, returns `session_id`.
2. `GET /stream/<sid>` → SSE stream backed by a per-session `queue.Queue` that the grabber writes log lines into. Auto-closes on `complete`/`error`/35-min hard cap.
3. `GET /download-file/<sid>` → serves the zip, then schedules `_purge` 3 s later.

State is in-memory (`_sessions`, `_queues`, guarded by `_lock`). A `_janitor` thread sweeps every 5 min: complete sessions after 30 min, errors after 10 min, "processing" sessions after 30 min (zombies), plus orphan files in `downloads/`. **Do not move state to a DB or multi-worker setup without rethinking this** — see deployment constraints below.

### Capture pipeline (`grabber.py` → `SiteGrabber.grab()`)

The whole point is the ordering of these phases. Reordering breaks things.

- **Phase 1 — Browser capture**: Playwright Chromium (stealth context — patches `navigator.webdriver` etc.). A `page.on("response")` handler tees every response body into `self._captured` keyed by URL. `_navigate` retries with progressively relaxed wait conditions (`networkidle` → `load` → `domcontentloaded`).
- **Iframe wrapper detection** (`_extract_iframe_content`): Aura/Webflow site-builder previews host the real page inside a fullscreen iframe. We **stay on the outer page** and pull `frame.content()` so all child responses still flow through `on_response`. Frames are scored (HTML size + body text + SPA root children) and we wait up to 30 s for a hydrated SPA.
- **Phase 2 — Persist**: Every captured body is written to `assets/` with hash-deduped filenames (`{sha256[:16]}_{stem}{ext}`). `_url_map` is the single source of truth: `original_url → "assets/filename"`.
- **Phase 2.5 — Fallback** (`_fallback_download`): Some assets in the DOM weren't fetched during the live load (different srcset descriptor, lazy-attribute-only). `requests` re-fetches them before rewriting starts, so they end up in `_url_map` for the rewrite phases.
- **Phase 2.7 — Vite chunks** (`_resolve_vite_chunks`): BFS through saved `.js` bundles, regex-find dynamic imports (`"./chunk-XYZ.js"` and `"assets/chunk-XYZ.js"` from `__vite__mapDeps`), download chunks **under their exact original filename** because Vite resolves them relative to the bundle URL at runtime. Walks recursively.
- **Phase 3 — CSS rewrite**: Two-pass design. We rewrite CSS *after* all assets (especially fonts/images referenced by `url()` and `@import`) are mapped, so `_rewrite_css` can resolve them. CSS files use `in_assets=True` (bare filename siblings); inline `<style>`/`style=""` use `in_assets=False` (`assets/filename`).
- **Phase 4 — HTML rewrite** (`_rewrite_html`): Strips `<base>`, SRI/CORS attrs (block local loads), removes "already-initialized" markers from libs like Unicorn Studio (`data-us-initialized` + child `<canvas>`), then rewrites `src`/`href`/`srcset`/lazy-data-attrs/SVG `<use>`/inline styles.

### Critical: the injected scripts in the rewritten HTML

These are why dynamically-built URLs work offline. Don't strip them lightly. All are stringified Python in `_rewrite_html` / its helpers.

1. **Early head script (`data-offline-resolve`)**: Inlines the full `_url_map` as JS, then patches the property setters of `HTMLScriptElement.src`, `HTMLLinkElement.href`, `HTMLImageElement.src/srcset`, `HTMLSourceElement`, `HTMLMediaElement`, `HTMLIFrameElement`, plus `Element.prototype.setAttribute`. So when Next.js does `el.src = "/_next/static/chunks/foo.js"` at runtime, the setter rewrites it to the local asset before the browser fetches. Map is keyed by both full URL and `pathname+search` (because `file://` resolution loses the origin). Special-cases `/_next/image?url=...` by peeling the inner CDN URL. Prefers `window.__offlineDataUri` (see below) for textures.
2. **Late body script (`data-offline-fix`)**: MutationObserver that re-rewrites img `src`/`srcset` after hydration. Also force-reveals GSAP-pinned elements (`opacity:0` + transforms) after 5 s, since ScrollTrigger pinning often misbehaves locally. Also retries `UnicornStudio.init()` because the captured page already has the loader script + an `if(!window.UnicornStudio)…` guard that bails out in offline mode.
3. **Runtime cache (`data-offline-runtime`)**: Patches `fetch`/`XMLHttpRequest` to answer from an in-page base64 cache (`_build_runtime_cache` — UnicornStudio scene JSON, its textures, icon-set JSON). `file://` blocks every `fetch()`, and a `file://` image used as a WebGL texture CORS-taints the canvas; serving from the cache (Response objects / `__offlineDataUri` data: URIs) sidesteps both.
4. **Aura sandbox scripts** / **ES-module SPA loader (`data-offline-esm`)** — see below; emitted only when the page needs them.

### ES-module SPAs (`_inject_module_loader`)

Native ES module loading is **forbidden from `file://`** — the origin is `null`, so every `<script type="module">` fetch (and every `import`) counts as cross-origin and Chrome blocks it. So a Vite/Angular/etc. SPA whose entry is `<script type="module" src>` cannot run offline at all; the old answer was to CSR-strip it to a frozen static snapshot.

When `_rewrite_html` sees an external module script it sets `self._module_app` (and forces `_is_csr = False` so the scripts survive). `_collect_app_modules` then BFS-walks the module graph from each entry, rewriting every intra-graph import to a bare asset filename. `_inject_module_loader` rebuilds each module as a `blob:` URL (exempt from the `file://` restriction), installs a runtime import map (`filename` / `./filename` / `assets/filename` → blob URL), and re-adds the entry as a module script. It also injects two shims an offline SPA needs: a `<base href>` equal to the document URL (so a History-API router resolves `/` instead of the file path → otherwise blank page) and a `history.pushState/replaceState` wrapper that swallows the `SecurityError` thrown against a null origin. `<link rel=modulepreload>` is dropped.

### Aura site-builder previews (`_inject_aura_sandbox`)

Aura previews don't ship a static page — a sandbox iframe Babel-transpiles the project at runtime. The project source arrives via a `postMessage` `UPDATE_MODULES` from the parent and **never touches the DOM**, so a plain snapshot freezes (dead canvases, no animations). Three things make it re-render offline:

- **Module capture**: a Playwright `add_init_script` records the `UPDATE_MODULES` message into `self._aura_modules_msg` during the grab.
- **`data-offline-sandbox` responder**: replays that message on every `SANDBOX_READY` the sandbox emits (offline there is no parent). Also swaps `BrowserRouter`→`MemoryRouter` in the bundle — a `file://` document path matches no route → blank page.
- **`data-offline-esm` loader**: native ES module loading is forbidden from `file://` (null origin → every fetch is cross-origin). `_collect_esm_modules` rewrites each captured esm.sh module's imports to bare filenames; the loader rebuilds them as `blob:` URLs (exempt from the restriction) and installs a runtime import map. The original esm.sh `<script type="importmap">` is dropped.

### CSR detection

`_detect_csr` flags pages where the body has <50 chars of text and ≤3 divs (pure SPA shell). For these, we **strip all scripts** during HTML rewrite — the rendered DOM is already in the snapshot, and keeping the JS would just trigger failed API calls that blank the page. **Exception:** if the page is an ES-module SPA (`_module_app`), the script-strip is skipped — `_inject_module_loader` re-runs the app via `blob:` URLs instead, which restores animations/interactivity a frozen snapshot loses.

## Deployment constraints

`entrypoint.sh` runs gunicorn with `--workers 1 --threads 4`. **Do not raise worker count.** Each download spawns a Chromium instance (~150-300 MB) plus all response bodies in RAM; Railway's free tier is 512 MB. The threading model handles concurrent SSE streams + healthchecks during a single download. If you need horizontal scale, you must externalize session state (currently in-process dicts) and the `downloads/` directory first.

`MAX_ASSET_BYTES = 30 MB` cap, plus a stricter 5 MB cap for `video/`/`audio/` content types in the response interceptor — these are the main memory levers.

## When modifying the grabber

- Test against a real JS-heavy site (Next.js + Aura/Webflow previews are the stress cases) and **open the resulting `index.html` from `file://` with the network blocked** — `http://`, or even `file://` while online, masks the bugs: remote `esm.sh`/CDN/font requests silently succeed and hide that they were never made local. Block all non-`file:`/`data:`/`blob:` requests when verifying.
- The `_url_map` must be populated before CSS rewriting (Phase 3) and HTML rewriting (Phase 4). Adding a new asset source means hooking it before Phase 3.
- New URL attributes (custom `data-*`, framework-specific) go in both `_collect_remote_urls` (so fallback fetches them) and `_rewrite_html` (so the saved HTML points to the local copy).
- The injected runtime scripts are stringified Python — escaping matters. Test in a browser console after a real grab, not just by reading the source.
