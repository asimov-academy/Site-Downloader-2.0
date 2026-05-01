"""
SiteGrabber - Complete static website snapshot for offline viewing.

Algorithm:
  Phase 1    — Playwright loads the page, all JS executes, all responses intercepted.
  Phase 2    — Every captured asset body is saved to assets/ with hash-dedup.
  Phase 2.5  — Any remote URL still present in the DOM is fetched via requests (fallback).
  Phase 3    — CSS files are rewritten in-place (url() / @import → local siblings).
  Phase 4    — HTML is parsed; every URL attribute + inline style is rewritten.
  Output     — index.html + assets/ directory ready to zip.

Design goals
  • Keep ALL JS — don't strip animation libraries (GSAP, Lenis, etc.).
  • Two-pass CSS rewrite: fonts/images are already mapped when we touch CSS.
  • Handle iframe-wrapper sites (Aura, Webflow previews) by navigating into them.
  • Scroll the page to trigger IntersectionObserver / lazy loading before capture.
  • Fallback requests download for assets whose CDN URL differed from what Playwright saw.
"""

import base64
import os
import re
import hashlib
import shutil
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

MAX_ASSET_BYTES = 30 * 1024 * 1024  # skip single assets >30 MB (huge videos)

CONTENT_TYPE_EXT = {
    "text/css": ".css",
    "text/javascript": ".js",
    "application/javascript": ".js",
    "application/x-javascript": ".js",
    "text/html": ".html",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/ico": ".ico",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "application/font-woff": ".woff",
    "application/font-woff2": ".woff2",
    "application/x-font-ttf": ".ttf",
    "application/json": ".json",
    "application/manifest+json": ".json",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "application/octet-stream": ".bin",
}

# Analytics/tracker domains whose resources we don't need for design snapshots
SKIP_DOMAINS = {
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "connect.facebook.net",
    "hotjar.com",
    "segment.io",
    "segment.com",
    "amplitude.com",
    "mixpanel.com",
    "clarity.ms",
    "bat.bing.com",
    "snap.licdn.com",
    "analytics.twitter.com",
}

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def get_site_name(url: str) -> str:
    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")
    clean = re.sub(r"[^a-zA-Z0-9.-]", "_", domain)
    if parsed.path and parsed.path != "/":
        path_part = re.sub(r"[^a-zA-Z0-9]", "_", parsed.path.strip("/"))[:30]
        clean = f"{clean}_{path_part}"
    return clean


def zip_directory(folder_path: str, output_path: str) -> str:
    base_name = output_path.replace(".zip", "")
    shutil.make_archive(base_name, "zip", folder_path)
    return base_name + ".zip"


# ──────────────────────────────────────────────────────────────────────────────
# Core grabber
# ──────────────────────────────────────────────────────────────────────────────


class SiteGrabber:
    def __init__(self, url: str, output_dir: str, log=print):
        self.url = url
        self.output_dir = output_dir
        self.assets_dir = os.path.join(output_dir, "assets")
        self.log = log

        self._url_map: dict[str, str] = {}   # original_url → "assets/filename"
        self._hash_map: dict[str, str] = {}  # sha256[:16]  → "assets/filename"
        self._captured: dict[str, dict] = {} # url → {body, content_type}
        self._base_url: str = url
        self._is_csr: bool = False           # True → CSR app, strip JS on output

        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        os.makedirs(self.assets_dir, exist_ok=True)

    # ── Asset persistence ─────────────────────────────────────────────────────

    def _ext_for(self, url: str, content_type: str = "") -> str:
        """Best-effort file extension from URL path or Content-Type."""
        parsed = urlparse(url)
        path_ext = os.path.splitext(parsed.path)[1].lower()
        if path_ext and len(path_ext) <= 6 and path_ext.isascii() and "." in path_ext:
            return path_ext
        ct = (content_type or "").split(";")[0].strip().lower()
        return CONTENT_TYPE_EXT.get(ct, "")

    def _save_asset(self, url: str, body: bytes, content_type: str = "") -> str | None:
        """Save body bytes to assets/, return 'assets/filename'. Content-deduplicates."""
        if not body:
            return None
        if url in self._url_map:
            return self._url_map[url]

        h = hashlib.sha256(body).hexdigest()[:16]

        if h in self._hash_map:
            # Same content already on disk — just add the alias
            self._url_map[url] = self._hash_map[h]
            return self._hash_map[h]

        ext = self._ext_for(url, content_type)
        parsed = urlparse(url)
        raw_name = os.path.basename(parsed.path) or "file"
        stem = re.sub(r"[^a-zA-Z0-9._-]", "_", os.path.splitext(raw_name)[0])[:30]
        filename = f"{h}_{stem}{ext}"

        filepath = os.path.join(self.assets_dir, filename)
        with open(filepath, "wb") as f:
            f.write(body)

        rel = f"assets/{filename}"
        self._url_map[url] = rel
        self._hash_map[h] = rel
        return rel

    # ── URL helpers ───────────────────────────────────────────────────────────

    def _should_skip(self, url: str) -> bool:
        try:
            netloc = urlparse(url).netloc.lower()
            return any(d in netloc for d in SKIP_DOMAINS)
        except Exception:
            return False

    def _local_of(self, url: str, base: str) -> str | None:
        """Resolve url relative to base; look up in _url_map. Returns local path or None."""
        if not url:
            return None
        url = url.strip()
        if url.startswith(("data:", "blob:", "#", "javascript:", "mailto:", "tel:")):
            return None
        abs_url = urljoin(base, url)
        return self._url_map.get(abs_url)

    def _rewrite_srcset(self, srcset: str, base: str) -> str:
        """Rewrite every URL in a srcset attribute."""
        parts = []
        # Split on commas that are followed by a URL (not a descriptor)
        for item in re.split(r",\s*(?=\S)", srcset):
            item = item.strip()
            if not item:
                continue
            tokens = item.split(None, 1)
            url_part = tokens[0]
            descriptor = tokens[1] if len(tokens) > 1 else ""
            local = self._local_of(url_part, base)
            entry = f"{local} {descriptor}".strip() if local else item
            parts.append(entry)
        return ", ".join(parts)

    # ── CSS rewriting ─────────────────────────────────────────────────────────

    def _make_local_css_ref(self, raw: str, base: str, in_assets: bool) -> str | None:
        """
        Resolve a raw CSS URL string to a local reference.
        in_assets=True  → bare filename (CSS sibling in assets/)
        in_assets=False → full 'assets/filename' path (inline CSS in HTML root)
        """
        url = raw.strip().strip("\"'")
        if not url or url.startswith(("data:", "blob:", "#")):
            return None
        abs_url = urljoin(base, url)
        local = self._url_map.get(abs_url)
        if not local:
            return None
        return os.path.basename(local) if in_assets else local

    def _rewrite_css(self, css_text: str, base_url: str, in_assets: bool = True) -> str:
        """
        Rewrite url() and @import references in CSS text.
        in_assets controls the prefix (see _make_local_css_ref).
        """

        # url("..."), url('...'), url(...)
        url_re = re.compile(r'url\(\s*(?:"([^"]*)"|\'([^\']*)\'|([^)\s\'"]*))\s*\)')

        def replace_url(m: re.Match) -> str:
            raw = m.group(1) if m.group(1) is not None else (
                m.group(2) if m.group(2) is not None else (m.group(3) or "")
            )
            ref = self._make_local_css_ref(raw, base_url, in_assets)
            return f'url("{ref}")' if ref else m.group(0)

        css_text = url_re.sub(replace_url, css_text)

        # @import url(...) or @import "..." (optional media query after)
        import_re = re.compile(
            r'@import\s+(?:url\(\s*["\']?([^"\')\s]+)["\']?\s*\)|["\']([^"\']+)["\'])'
        )

        def replace_import(m: re.Match) -> str:
            raw = m.group(1) or m.group(2) or ""
            ref = self._make_local_css_ref(raw, base_url, in_assets)
            return f'@import "{ref}"' if ref else m.group(0)

        css_text = import_re.sub(replace_import, css_text)

        # Chrome blocks file:// fetches for mask-image (treats them as
        # cross-origin) and the masked element renders fully invisible.
        # Inline the mask images as data: URIs so they bypass the network.
        css_text = self._inline_mask_data_uris(css_text)
        return css_text

    # ── Mask-image data: URI inlining ─────────────────────────────────────────

    _MASK_MIME = {
        ".png":  "image/png",  ".jpg":  "image/jpeg", ".jpeg": "image/jpeg",
        ".gif":  "image/gif",  ".svg":  "image/svg+xml", ".webp": "image/webp",
        ".avif": "image/avif",
    }

    def _local_ref_to_path(self, ref: str) -> str | None:
        """Resolve 'assets/foo.png' or bare 'foo.png' against assets_dir."""
        if not ref or ref.startswith(("data:", "http:", "https:", "//", "#")):
            return None
        if ref.startswith("assets/"):
            ref = ref[len("assets/"):]
        if "/" in ref or ref in ("", "."):
            return None
        path = os.path.join(self.assets_dir, ref)
        return path if os.path.isfile(path) else None

    def _to_data_uri(self, path: str) -> str | None:
        ext = os.path.splitext(path)[1].lower()
        mime = self._MASK_MIME.get(ext)
        if not mime:
            return None
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception:
            return None
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    # Match `mask-image:`, `-webkit-mask-image:`, `mask:` or `-webkit-mask:`
    # declarations and capture every url(...) inside them.
    _MASK_DECL_RE = re.compile(
        r"((?:-webkit-)?mask(?:-image)?\s*:\s*[^;{}]*?)"
        r"url\(\s*(['\"]?)([^)'\"\s]+)\2\s*\)",
        re.IGNORECASE,
    )

    def _inline_mask_data_uris(self, css_text: str) -> str:
        """Replace url() inside mask declarations with data: URIs."""
        if "mask" not in css_text.lower():
            return css_text

        def replace(m: re.Match) -> str:
            prefix, quote, ref = m.group(1), m.group(2), m.group(3)
            path = self._local_ref_to_path(ref)
            if not path:
                return m.group(0)
            data_uri = self._to_data_uri(path)
            if not data_uri:
                return m.group(0)
            return f"{prefix}url({quote}{data_uri}{quote})"

        # Apply repeatedly because one declaration may have multiple url()s
        return self._MASK_DECL_RE.sub(replace, css_text)

    # ── Playwright helpers ────────────────────────────────────────────────────

    def _stealth_context(self, browser):
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            ignore_https_errors=True,
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = window.chrome || { runtime: {} };
        """)
        return context

    def _serialize_runtime_stylesheets(self, page_or_frame) -> None:
        """
        Force CSSOM-only stylesheets back into <style> text content.

        styled-components (and other CSS-in-JS libs) read the SSR <style>
        element on hydration, take ownership, and *empty its textContent*
        while keeping the rules live in `document.styleSheets[i].cssRules`.
        Constructable stylesheets via `document.adoptedStyleSheets` are
        even worse: they have no <style> element at all.

        Either way, `page.content()` returns outerHTML which only sees
        literal <style> text — not CSSOM rules — so the snapshot loses
        all post-hydration styling. Call this BEFORE page.content() to
        materialise everything back into the DOM.
        """
        page_or_frame.evaluate("""() => {
          // 1) Re-fill empty <style> elements whose CSSOM rules still exist.
          for (const sheet of document.styleSheets) {
            const owner = sheet.ownerNode;
            if (!owner || owner.tagName !== 'STYLE') continue;
            if ((owner.textContent || '').trim().length > 0) continue;
            try {
              const txt = Array.from(sheet.cssRules || [])
                .map(r => r.cssText).join('\\n');
              if (txt) owner.textContent = txt;
            } catch (e) { /* cross-origin sheet — unreadable */ }
          }
          // 2) Materialise constructable stylesheets (adoptedStyleSheets).
          const adopted = document.adoptedStyleSheets || [];
          for (let i = 0; i < adopted.length; i++) {
            try {
              const txt = Array.from(adopted[i].cssRules || [])
                .map(r => r.cssText).join('\\n');
              if (!txt) continue;
              const el = document.createElement('style');
              el.setAttribute('data-adopted', String(i));
              el.textContent = txt;
              (document.head || document.documentElement).appendChild(el);
            } catch (e) {}
          }
        }""")

    def _navigate(self, page, url: str) -> None:
        """Try progressively relaxed wait conditions until the page loads."""
        for wait_until, timeout in [
            ("networkidle", 60_000),
            ("load", 60_000),
            ("domcontentloaded", 45_000),
        ]:
            try:
                page.goto(url, wait_until=wait_until, timeout=timeout)
                self.log(f"✓ Carregado ({wait_until})")
                return
            except Exception as exc:
                self.log(f"⚠️  {wait_until}: {str(exc)[:80]}")

        # If we're past about:blank the page has *some* content — proceed.
        if page.url not in ("", "about:blank"):
            self.log("⚠️  Prosseguindo com conteúdo parcial.")
            return

        raise RuntimeError(f"Não foi possível carregar {url}")

    def _extract_iframe_content(self, page) -> tuple[str | None, str | None]:
        """
        Detect full-screen iframe wrappers (e.g. Aura editor, Webflow previews).
        Uses Playwright frame objects — stays on the outer page so that all captured
        responses remain in _captured and the frame's JS runs in its original context.
        Returns (html_content, base_url) or (None, None) if no suitable frame found.
        """
        # --- Is the outer page a thin wrapper? ---
        # The iframe wrapper may appear/grow asynchronously after the initial
        # render (Aura site builders inject it via React). Poll up to 6 s for
        # signs that we're on a wrapper page.
        body_len = 0
        iframe_count = 0
        cover_ratio_max = 0.0
        for _ in range(6):
            try:
                body_len = page.evaluate("() => document.body.innerText.trim().length")
                iframe_count = page.evaluate(
                    "() => document.querySelectorAll('iframe').length"
                )
                cover_ratio_max = page.evaluate("""
                    () => Math.max(0, ...Array.from(document.querySelectorAll('iframe')).map(f =>
                        (f.offsetWidth * f.offsetHeight) / (window.innerWidth * window.innerHeight)
                    ))
                """)
            except Exception:
                return None, None

            # Found a fullscreen iframe → enough signal to commit to the wrapper path
            if iframe_count > 0 and cover_ratio_max >= 0.75:
                break
            page.wait_for_timeout(1000)

        if iframe_count == 0:
            return None, None

        is_wrapper = body_len < 500 or cover_ratio_max >= 0.75
        if not is_wrapper:
            return None, None

        self.log("🔍 Página wrapper detectada, aguardando iframe renderizar...")

        # --- Score a frame as potential "real content" ---
        def _score_frame(frame) -> int:
            try:
                html = frame.content()
                if len(html) < 1000:
                    return -1
                # Blank frames that have just been navigated to show generic shell
                body_text = frame.evaluate(
                    "() => document.body?.innerText?.trim()?.length || 0"
                )
                score = len(html) // 500 + body_text // 5
                # Big bonus: SPA root has children → React/Vue has rendered
                has_root = frame.evaluate("""
                    () => {
                        const r = document.querySelector('#root,#app,#__next,#__nuxt');
                        return r ? r.children.length : 0;
                    }
                """)
                if has_root > 0:
                    score += 300
                return score
            except Exception:
                return -1

        # Poll for a good frame (up to 30 s)
        deadline = 30_000
        poll = 1_000
        elapsed = 0
        best: tuple[int, object] | None = None  # (score, frame)

        while elapsed < deadline:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                sc = _score_frame(frame)
                if sc > 0 and (best is None or sc > best[0]):
                    best = (sc, frame)

            if best and best[0] >= 300:  # rendered SPA → stop early
                break

            page.wait_for_timeout(poll)
            elapsed += poll

        if best is None:
            self.log("⚠️  Nenhum frame com conteúdo renderizado encontrado")
            return None, None

        frame = best[1]
        frame_url = frame.url or ""
        base = frame_url if frame_url not in ("", "about:blank", "about:srcdoc") else page.url

        # Scroll the frame so all sections render (the SPA only paints what's visible)
        self._scroll_frame(frame, page)

        # Final wait for any lazy-loaded resources triggered by the scroll
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        page.wait_for_timeout(1500)

        # Materialise CSSOM-only stylesheets (styled-components, emotion,
        # adoptedStyleSheets) so frame.content() can serialise them.
        try:
            self._serialize_runtime_stylesheets(frame)
        except Exception as exc:
            self.log(f"⚠️  Serializar CSSOM (frame): {exc}")

        self.log(f"✓ Conteúdo do frame capturado ({frame_url[:70] or 'srcdoc'})")
        return frame.content(), base

    def _scroll_frame(self, frame, page) -> None:
        """
        Scroll a child frame in half-viewport steps so that all sections
        render (React/IntersectionObserver-driven sites only paint once
        the section enters the viewport).
        """
        try:
            total = frame.evaluate(
                "() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
            )
            self.log(f"📜 Rolando frame interno ({total}px)...")
            step = 500
            pos = 0
            guard = 0
            max_steps = 40
            last_pct = -1

            while pos < total and guard < max_steps:
                frame.evaluate(f"window.scrollTo(0, {pos})")
                page.wait_for_timeout(400)
                pos += step
                guard += 1

                pct = min(100, int(pos * 100 / max(total, 1)))
                if pct // 25 > last_pct // 25:
                    self.log(f"   📜 Rolando frame... {pct}%")
                    last_pct = pct

                new_total = frame.evaluate(
                    "() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
                )
                if new_total > total:
                    total = min(new_total, total + 5000)

            frame.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(800)
        except Exception as exc:
            self.log(f"⚠️  Frame scroll: {exc}")

    def _scroll_for_lazy_load(self, page) -> None:
        """
        Scroll the page top-to-bottom in half-viewport steps so that
        IntersectionObserver / lazy-load triggers fire for all elements.
        """
        try:
            total = page.evaluate("""
                Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)
            """)
            step = 600   # pixels per step (~half a typical viewport)
            pos = 0
            guard = 0
            max_steps = 40
            last_pct = -1

            while pos < total and guard < max_steps:
                page.evaluate(f"window.scrollTo(0, {pos})")
                page.wait_for_timeout(350)
                pos += step
                guard += 1

                pct = min(100, int(pos * 100 / total))
                if pct // 25 > last_pct // 25:  # report at 25%, 50%, 75%, 100%
                    self.log(f"   📜 Rolando... {pct}%")
                    last_pct = pct

                # Page might grow (infinite scroll)
                new_total = page.evaluate("""
                    Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)
                """)
                if new_total > total:
                    total = min(new_total, total + 5000)  # cap growth

            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(400)
        except Exception as exc:
            self.log(f"⚠️  Scroll: {exc}")

    # ── Fallback download ─────────────────────────────────────────────────────

    def _collect_remote_urls(self, html: str, base_url: str) -> list[str]:
        """
        Parse raw HTML and collect every remote URL that isn't in the url_map yet.
        Used to feed the fallback downloader before CSS and HTML rewriting.
        """
        soup = BeautifulSoup(html, "html.parser")
        pending: set[str] = set()

        def add(raw: str) -> None:
            if not raw:
                return
            raw = raw.strip()
            if raw.startswith(("data:", "blob:", "#", "javascript:", "mailto:", "tel:")):
                return
            abs_url = urljoin(base_url, raw)
            if abs_url.startswith("http") and abs_url not in self._url_map:
                pending.add(abs_url)

        for tag in soup.find_all(True):
            for attr in ("src", "href", "poster", "data-src", "data-lazy-src",
                         "data-original", "data-background", "data-bg", "data-image"):
                add(tag.get(attr, ""))
            for sattr in ("srcset", "data-srcset"):
                val = tag.get(sattr, "")
                if val:
                    for item in re.split(r",\s*(?=\S)", val):
                        tokens = item.strip().split()
                        if tokens:
                            add(tokens[0])

        return list(pending)

    def _resolve_vite_chunks(self) -> int:
        """
        Find dynamic import() calls in saved JS bundles (e.g. Unicorn Studio,
        Sandpack) and download the referenced chunks. Vite emits relative imports
        like `import("./index-CHubWH17.js")` that resolve relative to the bundle's
        URL — when opened locally the browser tries to fetch them as siblings of
        the bundle, so we must save the chunks under their EXACT original filename.
        Walks recursively (chunks may import other chunks).
        """
        # Build local_path → original_url map
        local_to_orig: dict[str, str] = {}
        for orig_url, local in self._url_map.items():
            if local not in local_to_orig:
                local_to_orig[local] = orig_url

        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": self._base_url,
        })

        saved = 0
        # BFS through bundles → their imported chunks → those chunks' imports → …
        queue: list[tuple[str, str]] = []  # (asset_filename, original_url)
        seen: set[str] = set()

        for filename in os.listdir(self.assets_dir):
            if filename.endswith(".js"):
                local_rel = f"assets/{filename}"
                orig = local_to_orig.get(local_rel)
                if orig:
                    queue.append((filename, orig))

        # Match relative ESM references in two flavours:
        #   "./chunk-hash.js"        ← dynamic import / static import / export from
        #   "assets/chunk-hash.js"   ← __vite__mapDeps preload manifest entries
        # The captured group is always the bare filename (siblings of the bundle).
        import_re = re.compile(
            r'''["'](?:\./|assets/)([A-Za-z0-9][A-Za-z0-9._-]*\.(?:js|mjs))["']'''
        )

        while queue:
            filename, parent_url = queue.pop(0)
            if filename in seen:
                continue
            seen.add(filename)

            filepath = os.path.join(self.assets_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception:
                continue

            chunks = set(import_re.findall(content))
            if not chunks:
                continue

            parent_dir = parent_url.rsplit("/", 1)[0]
            for chunk_name in chunks:
                chunk_path = os.path.join(self.assets_dir, chunk_name)
                if os.path.exists(chunk_path):
                    continue

                chunk_url = f"{parent_dir}/{chunk_name}"
                try:
                    r = session.get(chunk_url, timeout=15, verify=False)
                    if r.status_code == 200 and r.content:
                        with open(chunk_path, "wb") as f:
                            f.write(r.content)
                        saved += 1
                        # Walk into this chunk too
                        queue.append((chunk_name, chunk_url))
                        # Also expose in the URL map (in case CSS rewriting needs it)
                        self._url_map[chunk_url] = f"assets/{chunk_name}"
                except Exception:
                    pass

        try:
            session.close()
        except Exception:
            pass

        return saved

    def _fallback_download(self, urls: list[str]) -> int:
        """
        Download any URLs not already in the url_map using requests.
        Returns count of newly saved assets.
        """
        if not urls:
            return 0

        pending = [u for u in urls if u not in self._url_map and not self._should_skip(u)]
        if not pending:
            return 0

        self.log(f"   ⬇️  {len(pending)} URLs para baixar via fallback...")

        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": self._base_url,
        })

        saved = 0
        for url in pending:
            if url in self._url_map:
                continue
            try:
                r = session.get(url, timeout=15, verify=False, stream=False)
                if r.status_code == 200 and r.content:
                    body = r.content
                    if len(body) <= MAX_ASSET_BYTES:
                        ct = r.headers.get("content-type", "")
                        if self._save_asset(url, body, ct):
                            saved += 1
            except Exception:
                pass

        try:
            session.close()
        except Exception:
            pass

        return saved

    # ── CSR detection ─────────────────────────────────────────────────────────

    def _detect_csr(self, html: str) -> bool:
        """
        Returns True if the page is pure Client-Side Rendering:
        body has only an empty SPA root div and no meaningful text.
        These pages cannot work offline with JS enabled (API calls will fail),
        so we strip all scripts and keep the rendered DOM as-is.
        """
        soup = BeautifulSoup(html, "html.parser")
        body = soup.find("body")
        if not body:
            return False
        text = (body.get_text(strip=True) or "")
        # SPA root marker (Vite/CRA, Next.js, Nuxt) + nearly-empty body → definite CSR
        spa_root = body.find(id=re.compile(r"^(root|app|__next|__nuxt)$"))
        if spa_root is not None and len(text) < 200:
            return True
        # Fallback heuristic: tiny body with very few divs (generic SPA shell)
        divs = body.find_all("div")
        return len(text) < 50 and len(divs) <= 3

    def _detect_csr_from_origin(self) -> bool:
        """
        Fetch the ORIGINAL server HTML (no JS executed) and check if it's an
        SPA shell. The Playwright-rendered DOM is always full of content for
        SPAs (because React/Vue has mounted), so _detect_csr applied to it
        always returns False. The pre-JS HTML is what reveals the real shape.
        """
        try:
            r = requests.get(
                self.url,
                timeout=10,
                verify=False,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
                },
            )
            if r.status_code == 200 and r.text:
                return self._detect_csr(r.text)
        except Exception:
            pass
        return False

    def _detect_nextjs_app_router(self, html: str) -> bool:
        """
        Next.js 13+ App Router with React streaming SSR. The body is fully
        server-rendered (so the empty-shell heuristic misses it), but the
        hydration runtime is just as destructive offline as CSR: webpack
        rebuilds chunk URLs at runtime, route prefetches hit /_next/data/,
        CSS chunks load lazily, and the $RC() Suspense swap re-runs against
        a DOM that's already been resolved. Treat as CSR.
        """
        if "/_next/static/chunks/" not in html:
            return False
        soup = BeautifulSoup(html, "html.parser")
        if soup.find("meta", attrs={"name": "next-size-adjust"}) is not None:
            return True
        if soup.find("script", id="_R_") is not None:
            return True
        if soup.find("template", id=re.compile(r"^B:\d+$")) is not None:
            return True
        return False

    # ── HTML processing ───────────────────────────────────────────────────────

    def _rewrite_html(self, html: str, base_url: str) -> str:
        soup = BeautifulSoup(html, "html.parser")

        # Remove <base> — it would resolve local paths against the original host
        for tag in soup.find_all("base"):
            tag.decompose()

        # Strip SRI / CORS attributes that block local file loading
        for tag in soup.find_all(["script", "link"]):
            for attr in ("integrity", "crossorigin", "nonce"):
                tag.attrs.pop(attr, None)

        # Neutralize JS smooth-scroll libraries (Lenis, etc.). These attach a
        # `wheel` listener that preventDefault()s the event and animates scroll
        # via RAF. Offline that animation loop usually breaks (init failures,
        # missing deps), and the page becomes scroll-locked — wheel/trackpad
        # gestures land on the listener but the scroll position never updates.
        # Runs in both CSR and non-CSR modes.
        SMOOTH_SCROLL_CLASS_PREFIXES = ("lenis",)  # extend if more libs surface
        SMOOTH_SCROLL_SCRIPT_NAMES = ("lenis",)
        html_root = soup.find("html")
        if html_root is not None:
            cls = html_root.get("class") or []
            if isinstance(cls, str):
                cls = cls.split()
            kept = [c for c in cls if not any(c.startswith(p) for p in SMOOTH_SCROLL_CLASS_PREFIXES)]
            if kept != cls:
                if kept:
                    html_root["class"] = kept
                else:
                    del html_root["class"]
        for tag in soup.find_all("script", src=True):
            src_lower = (tag.get("src") or "").lower()
            if any(name in src_lower for name in SMOOTH_SCROLL_SCRIPT_NAMES):
                tag.decompose()

        # Reset post-init markers from libraries that "remember" they've already
        # initialized via DOM attributes/canvas children. When we capture the
        # rendered DOM, those markers are baked in — and the next page load sees
        # them, skips re-init, and the visual element (canvas) stays empty.
        # Pattern is generic: any element whose existence relies on a JS lib
        # finding a placeholder div + creating a child <canvas>.
        for el in soup.find_all(attrs={"data-us-project": True}):
            for attr in ("data-us-initialized", "data-scene-id"):
                el.attrs.pop(attr, None)
            for canvas in el.find_all("canvas"):
                canvas.decompose()

        # ── <script src> ──────────────────────────────────────────────────────
        self.log("📝 Processando scripts...")

        if self._is_csr:
            # CSR app: JS makes API calls that will fail offline and blank the page.
            # The rendered HTML is already in the DOM — strip all scripts to preserve it.
            removed = 0
            for tag in soup.find_all("script"):
                tag.decompose()
                removed += 1
            # Skip preloads/prefetches that the now-removed scripts would have used,
            # but spare font preloads — the CSS @font-face still needs the bytes.
            def _is_strippable_link(tag):
                rel = tag.get("rel", [])
                rel_str = rel if isinstance(rel, str) else " ".join(rel or [])
                if not any(x in rel_str for x in ("preload", "modulepreload", "prefetch")):
                    return False
                return tag.get("as", "") != "font"
            for tag in soup.find_all("link"):
                if _is_strippable_link(tag):
                    tag.decompose()
            # Streaming Suspense leftovers from React 19: the swap already
            # happened during capture, but empty <template id="B:N"> shells
            # remain. Harmless visually but not useful either; clean up.
            tmpl_removed = 0
            for tag in soup.find_all("template", id=re.compile(r"^B:\d+$")):
                tag.decompose()
                tmpl_removed += 1
            note = f"{removed} scripts removidos"
            if tmpl_removed:
                note += f", {tmpl_removed} <template> de streaming"
            self.log(f"   🛡️  App CSR detectado — {note} (conteúdo já no DOM)")
        else:
            scripts_done = 0
            for tag in soup.find_all("script", src=True):
                local = self._local_of(tag["src"], base_url)
                if local:
                    tag["src"] = local
                    scripts_done += 1
            self.log(f"   ✅ {scripts_done} scripts localizados")

        # ── <link href> (CSS, preload, icons, manifests) ──────────────────────
        self.log("🎨 Processando stylesheets e links...")
        links_done = 0
        for tag in soup.find_all("link"):
            href = tag.get("href", "")
            if href and not href.startswith(("data:", "#")):
                local = self._local_of(href, base_url)
                if local:
                    tag["href"] = local
                    links_done += 1
        self.log(f"   ✅ {links_done} links reescritos")

        # ── Media elements ─────────────────────────────────────────────────────
        self.log("🖼️  Processando imagens e mídia...")
        media_done = 0
        for tag in soup.find_all(["img", "source", "video", "audio", "track"]):
            # Lazy-load data-src variants → promote to src
            for lazy_attr in ("data-src", "data-lazy-src", "data-original", "data-url"):
                val = tag.get(lazy_attr)
                if val and not val.startswith(("data:", "blob:", "{")):
                    local = self._local_of(val, base_url)
                    if local:
                        tag["src"] = local
                        del tag[lazy_attr]
                        media_done += 1
                        break

            # Regular src
            src = tag.get("src", "")
            if src and not src.startswith(("data:", "blob:")):
                local = self._local_of(src, base_url)
                if local:
                    tag["src"] = local
                    media_done += 1

            # srcset / data-srcset
            for sattr in ("srcset", "data-srcset"):
                val = tag.get(sattr)
                if val:
                    tag[sattr] = self._rewrite_srcset(val, base_url)

            # <video poster>
            if tag.name == "video":
                poster = tag.get("poster", "")
                if poster:
                    local = self._local_of(poster, base_url)
                    if local:
                        tag["poster"] = local
                        media_done += 1

        self.log(f"   ✅ {media_done} elementos de mídia processados")

        # ── Inline style attributes ────────────────────────────────────────────
        self.log("✨ Processando estilos inline...")
        inline_done = 0
        for tag in soup.find_all(style=True):
            if "url(" in tag["style"]:
                tag["style"] = self._rewrite_css(tag["style"], base_url, in_assets=False)
                inline_done += 1

        # ── <style> block contents ─────────────────────────────────────────────
        style_blocks = 0
        for tag in soup.find_all("style"):
            if tag.get("data-offline"):
                continue
            if tag.string and "url(" in tag.string:
                tag.string = self._rewrite_css(tag.string, base_url, in_assets=False)
                style_blocks += 1
        self.log(f"   ✅ {inline_done} atributos style + {style_blocks} blocos <style> reescritos")

        # ── Custom data attributes used by parallax / lazy libs ───────────────
        self.log("🔗 Processando atributos de dados (parallax, lazy)...")
        data_done = 0
        for attr in ("data-background", "data-bg", "data-image"):
            for tag in soup.find_all(attrs={attr: True}):
                val = tag[attr]
                if val and not val.startswith(("data:", "blob:", "#", "{")):
                    local = self._local_of(val, base_url)
                    if local:
                        tag[attr] = local
                        data_done += 1

        # ── SVG <use href> ────────────────────────────────────────────────────
        for tag in soup.find_all("use"):
            for attr in ("href", "xlink:href"):
                val = tag.get(attr, "")
                if val and not val.startswith("#"):
                    local = self._local_of(val, base_url)
                    if local:
                        tag[attr] = local
                        data_done += 1
        self.log(f"   ✅ {data_done} atributos de dados reescritos")

        # ── Inject offline-compatibility CSS ─────────────────────────────────
        head = soup.find("head")
        if head:
            style = soup.new_tag("style")
            style["data-offline"] = "1"
            css = (
                "/* offline: ensure content is visible regardless of JS init state */\n"
                "html,body{opacity:1!important;visibility:visible!important}\n"
                ".page-loader,.site-loader,[class*='loading-screen'],"
                "[id*='loading-screen']{display:none!important}\n"
            )
            if self._is_csr:
                # GSAP ScrollTrigger pin-spacer: only collapse when scripts are
                # stripped — without GSAP running the spacer's reserved scroll
                # distance becomes a black gap. When scripts run (non-CSR), GSAP
                # actually pins and NEEDS the computed height to provide the
                # scroll-through animation distance; forcing it to auto would
                # collapse multi-viewport sticky animations into one screen.
                css += (
                    "/* GSAP ScrollTrigger pin-spacer (CSR mode only): without\n"
                    "   GSAP running, the spacer leaves a black gap of reserved\n"
                    "   scroll. Collapse it to its content's natural height. */\n"
                    ".pin-spacer{height:auto!important;min-height:0!important;\n"
                    "  padding:0!important;max-height:none!important}\n"
                    ".pin-spacer>*{position:relative!important;\n"
                    "  inset:auto!important;top:auto!important;left:auto!important}\n"
                )
            style.string = css
            head.append(style)

        # ── Inject early URL-resolver script (top of <head>) ──────────────────
        # Frameworks like Next.js construct asset URLs at runtime
        # (e.g. `/_next/static/chunks/foo.js`) and assign them via
        # element.src / setAttribute. We patch those setters BEFORE any other
        # script runs so the browser fetches our local copy from `assets/`.
        if head:
            import json as _json
            asset_map = {orig: local for orig, local in self._url_map.items()}
            asset_map_json = _json.dumps(asset_map)

            early = soup.new_tag("script")
            early["data-offline-resolve"] = "1"
            early.string = (
                "(function(){\n"
                "var ASSET_MAP = " + asset_map_json + ";\n"
                "// Pre-populate path+query keys: when opened via file://, JS\n"
                "// resolves '/foo.js' against file://… so we lose the original\n"
                "// origin. Indexing by pathname+search lets the lookup succeed.\n"
                "var _add = {};\n"
                "for (var _k in ASSET_MAP) {\n"
                "  try { var _u = new URL(_k); _add[_u.pathname + _u.search] = ASSET_MAP[_k]; }\n"
                "  catch(e){}\n"
                "}\n"
                "for (var _k in _add) if (!ASSET_MAP[_k]) ASSET_MAP[_k] = _add[_k];\n"
                "function resolveLocal(u){\n"
                "  if (!u || typeof u !== 'string') return null;\n"
                "  if (u.indexOf('data:') === 0 || u.indexOf('blob:') === 0) return null;\n"
                "  if (ASSET_MAP[u]) return ASSET_MAP[u];\n"
                "  try {\n"
                "    var url = new URL(u, location.href);\n"
                "    var pq = url.pathname + url.search;\n"
                "    if (ASSET_MAP[pq]) return ASSET_MAP[pq];\n"
                "    // Next.js image optimization wrapper — peel the inner CDN URL\n"
                "    if (/_next\\/image$/.test(url.pathname)) {\n"
                "      var t = url.searchParams.get('url');\n"
                "      if (t) {\n"
                "        var dec = decodeURIComponent(t);\n"
                "        if (ASSET_MAP[dec]) return ASSET_MAP[dec];\n"
                "        var bare = dec.split('?')[0];\n"
                "        for (var k in ASSET_MAP) {\n"
                "          if (k.split('?')[0] === bare) return ASSET_MAP[k];\n"
                "        }\n"
                "      }\n"
                "    }\n"
                "  } catch(e){}\n"
                "  return null;\n"
                "}\n"
                "function rewriteSrcset(s){\n"
                "  if (!s || typeof s !== 'string') return s;\n"
                "  return s.split(',').map(function(it){\n"
                "    var p = it.trim().split(/\\s+/);\n"
                "    var loc = resolveLocal(p[0]);\n"
                "    if (loc) p[0] = loc;\n"
                "    return p.join(' ');\n"
                "  }).join(', ');\n"
                "}\n"
                "// Patch property setters: el.src = '...' / el.href = '...'\n"
                "// IMPORTANT: skip rewrite when the element has crossOrigin set.\n"
                "// WebGL textures (UnicornStudio, Three.js, etc.) are loaded via\n"
                "//   img.crossOrigin = 'anonymous'; img.src = 'https://cdn/...'\n"
                "// and consumed via gl.texImage2D. file:// resources have no CORS\n"
                "// headers, so rewriting to local makes WebGL reject the texture\n"
                "// (Access blocked by CORS policy → black/missing 3D scene).\n"
                "// Better to keep the original URL: works online, fails offline,\n"
                "// matches non-patched behaviour.\n"
                "function patchSetter(klass, prop, transform){\n"
                "  if (!klass || !klass.prototype) return;\n"
                "  var desc = Object.getOwnPropertyDescriptor(klass.prototype, prop);\n"
                "  if (!desc || !desc.set) return;\n"
                "  Object.defineProperty(klass.prototype, prop, {\n"
                "    configurable: true,\n"
                "    get: desc.get,\n"
                "    set: function(v){\n"
                "      try {\n"
                "        if (this.crossOrigin) {\n"
                "          /* preserve cross-origin URL — see comment above */\n"
                "        } else if (transform === 'srcset') {\n"
                "          v = rewriteSrcset(v);\n"
                "        } else {\n"
                "          var loc = resolveLocal(v); if (loc) v = loc;\n"
                "        }\n"
                "      } catch(e){}\n"
                "      desc.set.call(this, v);\n"
                "    }\n"
                "  });\n"
                "}\n"
                "patchSetter(window.HTMLScriptElement, 'src');\n"
                "patchSetter(window.HTMLLinkElement, 'href');\n"
                "patchSetter(window.HTMLImageElement, 'src');\n"
                "patchSetter(window.HTMLImageElement, 'srcset', 'srcset');\n"
                "patchSetter(window.HTMLSourceElement, 'src');\n"
                "patchSetter(window.HTMLSourceElement, 'srcset', 'srcset');\n"
                "patchSetter(window.HTMLMediaElement, 'src');\n"
                "patchSetter(window.HTMLIFrameElement, 'src');\n"
                "// Patch setAttribute too — some libs use it instead of property set\n"
                "var _setAttr = Element.prototype.setAttribute;\n"
                "Element.prototype.setAttribute = function(name, value){\n"
                "  try {\n"
                "    if (typeof value === 'string' && !this.crossOrigin) {\n"
                "      if (name === 'src' || name === 'href') {\n"
                "        var loc = resolveLocal(value);\n"
                "        if (loc) value = loc;\n"
                "      } else if (name === 'srcset') {\n"
                "        value = rewriteSrcset(value);\n"
                "      }\n"
                "    }\n"
                "  } catch(e){}\n"
                "  return _setAttr.call(this, name, value);\n"
                "};\n"
                "// Expose for the late-init script in body\n"
                "window.__resolveLocal = resolveLocal;\n"
                "window.__rewriteSrcset = rewriteSrcset;\n"
                "})();"
            )
            # Insert at the very top of <head> so it runs before any other script
            head.insert(0, early)

        # ── Inject late offline-fix at end of body ────────────────────────────
        body = soup.find("body")
        if body:
            fix = soup.new_tag("script")
            fix["data-offline-fix"] = "1"
            fix.string = (
                "(function(){\n"
                "var IS_CSR = " + ("true" if self._is_csr else "false") + ";\n"
                "var resolveLocal = window.__resolveLocal || function(){return null;};\n"
                "var rewriteSrcset = window.__rewriteSrcset || function(s){return s;};\n"
                "function fixImg(el){\n"
                "  if (!el || el.tagName !== 'IMG') return;\n"
                "  var src = el.getAttribute('src');\n"
                "  var loc = resolveLocal(src);\n"
                "  if (loc && src !== loc) el.setAttribute('src', loc);\n"
                "  var ss = el.getAttribute('srcset');\n"
                "  if (ss) {\n"
                "    var nss = rewriteSrcset(ss);\n"
                "    if (nss !== ss) el.setAttribute('srcset', nss);\n"
                "  }\n"
                "}\n"
                "function fixAll(){ document.querySelectorAll('img').forEach(fixImg); }\n"
                "function hasSlideOffset(t){\n"
                "  // True if a transform indicates a 'parked off-screen' starting\n"
                "  // state: translation in px (>= 30) or % (>= 5), or a matrix\n"
                "  // with non-zero translation. Returns false for crossfade-only\n"
                "  // companions like scale(0.9) or pure centering translateX(-50%).\n"
                "  if (!t || t === 'none') return false;\n"
                "  // matrix(a,b,c,d,tx,ty) — parse tx/ty; matrix3d & friends → assume slide.\n"
                "  var matMatch = t.match(/matrix\\(([^)]+)\\)/);\n"
                "  if (matMatch) {\n"
                "    var parts = matMatch[1].split(',').map(function(x){return parseFloat(x.trim());});\n"
                "    if (parts.length === 6) {\n"
                "      if (Math.abs(parts[4]) >= 30 || Math.abs(parts[5]) >= 30) return true;\n"
                "    } else { return true; }\n"
                "  }\n"
                "  if (/matrix3d/i.test(t)) return true;\n"
                "  var px = t.match(/(-?\\d+\\.?\\d*)px/g) || [];\n"
                "  for (var i = 0; i < px.length; i++) {\n"
                "    if (Math.abs(parseFloat(px[i])) >= 30) return true;\n"
                "  }\n"
                "  var pct = t.match(/(-?\\d+\\.?\\d*)%/g) || [];\n"
                "  for (var j = 0; j < pct.length; j++) {\n"
                "    if (Math.abs(parseFloat(pct[j])) >= 5) return true;\n"
                "  }\n"
                "  return false;\n"
                "}\n"
                "function isHiddenStart(s){\n"
                "  // True if the element's inline style is parked at a 'before'\n"
                "  // animation state. opacity:0 alone is ambiguous (could be a\n"
                "  // crossfade companion); pair it with a slide transform OR an\n"
                "  // explicit visibility:hidden (GSAP/SplitType signature) to be\n"
                "  // confident it's a scroll-reveal waiting to fire.\n"
                "  if (s.opacity !== '0' && s.visibility !== 'hidden') return false;\n"
                "  if (s.visibility === 'hidden') return true;\n"
                "  return hasSlideOffset(s.transform) || hasSlideOffset(s.translate);\n"
                "}\n"
                "function revealEl(el){\n"
                "  var s = el.style;\n"
                "  s.opacity = '1';\n"
                "  if (s.visibility === 'hidden') s.visibility = 'visible';\n"
                "  if (s.transform) s.transform = 'none';\n"
                "  if (s.translate) s.translate = 'none';\n"
                "  if (s.rotate)    s.rotate = 'none';\n"
                "  if (s.scale)     s.scale = 'none';\n"
                "  if (s.pointerEvents === 'none') s.pointerEvents = '';\n"
                "}\n"
                "function snapReveal(){\n"
                "  // Safety net: any 'before-state' element still hidden gets\n"
                "  // forced visible. Used as a deadline pass for non-CSR mode\n"
                "  // (after GSAP/etc had a chance to play) and as a final guard.\n"
                "  // Skip pinned-chain elements — same reason as findScrollAnchor.\n"
                "  var n = 0;\n"
                "  document.querySelectorAll('[style]').forEach(function(el){\n"
                "    if (!isHiddenStart(el.style)) return;\n"
                "    if (isInsideFixed(el)) return;\n"
                "    revealEl(el); n++;\n"
                "  });\n"
                "  if (window.console && n) console.log('[offline-fix] snap-revealed', n);\n"
                "}\n"
                "function isInsideFixed(el){\n"
                "  var p = el;\n"
                "  while (p && p !== document.documentElement) {\n"
                "    if (getComputedStyle(p).position === 'fixed') return true;\n"
                "    p = p.parentElement;\n"
                "  }\n"
                "  return false;\n"
                "}\n"
                "function findScrollAnchor(el){\n"
                "  // Pinned-narrative sections (one position:fixed ancestor wrapping\n"
                "  // many sequenced headings the live JS reveals one-by-one across\n"
                "  // scroll progress) can't be orchestrated offline — revealing all\n"
                "  // of them at once produces an overlapping mess. Skip them: leave\n"
                "  // the parked state intact, matching the live site at scroll=0.\n"
                "  // For sticky chains, observe the sticky container itself (fires\n"
                "  // when the user has scrolled past its stuck threshold).\n"
                "  if (isInsideFixed(el)) return null;\n"
                "  var p = el;\n"
                "  while (p && p !== document.documentElement) {\n"
                "    if (getComputedStyle(p).position === 'sticky') return p;\n"
                "    p = p.parentElement;\n"
                "  }\n"
                "  return el;\n"
                "}\n"
                "function progressiveReveal(){\n"
                "  // CSR mode: scripts stripped → no GSAP/IO is going to fire.\n"
                "  // Mimic a scroll-driven reveal: each parked element gets a\n"
                "  // CSS transition + IntersectionObserver. As it enters viewport\n"
                "  // we transition to the 'after' state, with a small stagger by\n"
                "  // document order so SplitType chars still feel letter-by-letter.\n"
                "  var targets = [];\n"
                "  document.querySelectorAll('[style]').forEach(function(el){\n"
                "    if (isHiddenStart(el.style)) targets.push(el);\n"
                "  });\n"
                "  if (!targets.length) return;\n"
                "  var EASE = 'cubic-bezier(.16,1,.3,1)';\n"
                "  targets.forEach(function(el){\n"
                "    el.style.transition =\n"
                "      'opacity .6s ' + EASE + ', transform .6s ' + EASE + ', ' +\n"
                "      'translate .6s ' + EASE + ', scale .6s ' + EASE + ', ' +\n"
                "      'visibility 0s linear';\n"
                "  });\n"
                "  if (typeof IntersectionObserver === 'undefined') {\n"
                "    targets.forEach(revealEl);\n"
                "    return;\n"
                "  }\n"
                "  // Group targets by their scroll anchor. Anchors in sticky\n"
                "  // sections share one observation point — when that anchor\n"
                "  // intersects, we reveal all its parked descendants.\n"
                "  // Targets with null anchor (inside position:fixed) are skipped.\n"
                "  var groups = new Map();\n"
                "  targets.forEach(function(el){\n"
                "    var anchor = findScrollAnchor(el);\n"
                "    if (!anchor) return;\n"
                "    if (!groups.has(anchor)) groups.set(anchor, []);\n"
                "    groups.get(anchor).push(el);\n"
                "  });\n"
                "  var io = new IntersectionObserver(function(entries){\n"
                "    entries.forEach(function(entry){\n"
                "      if (!entry.isIntersecting) return;\n"
                "      var children = groups.get(entry.target) || [entry.target];\n"
                "      children.sort(function(a, b){\n"
                "        var pos = a.compareDocumentPosition(b);\n"
                "        return (pos & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1;\n"
                "      });\n"
                "      children.forEach(function(child, i){\n"
                "        var delay = Math.min(i * 18, 700);\n"
                "        setTimeout(function(){ revealEl(child); }, delay);\n"
                "      });\n"
                "      io.unobserve(entry.target);\n"
                "    });\n"
                "  }, { threshold: 0.05, rootMargin: '0px 0px -8% 0px' });\n"
                "  groups.forEach(function(_, anchor){ io.observe(anchor); });\n"
                "  // Deadline guard: anything that never intersects still gets revealed.\n"
                "  setTimeout(snapReveal, 8000);\n"
                "}\n"
                "function initUnicornStudio(){\n"
                "  // Captured page already has the loaded UMD script + the inline\n"
                "  // loader that says `if(!window.UnicornStudio)…`. The loader bails\n"
                "  // because UnicornStudio is already defined, so init() never runs.\n"
                "  if (window.UnicornStudio && typeof window.UnicornStudio.init === 'function'\n"
                "      && !window.UnicornStudio.isInitialized) {\n"
                "    try { window.UnicornStudio.init(); window.UnicornStudio.isInitialized = true; }\n"
                "    catch(e){ if(window.console) console.warn('[offline-fix] UnicornStudio init failed:', e); }\n"
                "  }\n"
                "}\n"
                "// Initial img sweep + observer for hydration-time updates\n"
                "fixAll();\n"
                "var obs = new MutationObserver(function(muts){\n"
                "  for (var i = 0; i < muts.length; i++) {\n"
                "    var m = muts[i];\n"
                "    if (m.type === 'attributes' && m.target.tagName === 'IMG') fixImg(m.target);\n"
                "    for (var j = 0; j < m.addedNodes.length; j++) {\n"
                "      var n = m.addedNodes[j];\n"
                "      if (n && n.nodeType === 1) {\n"
                "        if (n.tagName === 'IMG') fixImg(n);\n"
                "        if (n.querySelectorAll) n.querySelectorAll('img').forEach(fixImg);\n"
                "      }\n"
                "    }\n"
                "  }\n"
                "});\n"
                "obs.observe(document, {childList:true, subtree:true,\n"
                "  attributes:true, attributeFilter:['src','srcset']});\n"
                "setTimeout(fixAll, 1000);\n"
                "setTimeout(fixAll, 3000);\n"
                "var go = function(){\n"
                "  // CSR: scripts stripped, so 'before-state' elements stay parked\n"
                "  // forever unless we do something. Use IntersectionObserver to\n"
                "  // reveal them progressively as the user scrolls — preserves the\n"
                "  // scroll-triggered animation feel for SplitType chars, etc.\n"
                "  // Non-CSR: GSAP/Framer may still play; let them, then catch any\n"
                "  // leftovers with a snap pass at 5 s.\n"
                "  if (IS_CSR) progressiveReveal();\n"
                "  else setTimeout(snapReveal, 5000);\n"
                "  initUnicornStudio();\n"
                "  setTimeout(initUnicornStudio, 500);\n"
                "  setTimeout(initUnicornStudio, 2000);\n"
                "};\n"
                "if (document.readyState === 'complete') go();\n"
                "else window.addEventListener('load', go);\n"
                "})();"
            )
            body.append(fix)

        return str(soup)

    # ── Main entry point ──────────────────────────────────────────────────────

    def grab(self) -> bool:
        # ── Phase 1: Browser capture ──────────────────────────────────────────
        with sync_playwright() as p:
            self.log("🚀 Iniciando navegador...")
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-gpu",
                    "--mute-audio",
                    "--no-first-run",
                    # NOTE: --disable-web-security intentionally omitted — it
                    # breaks ESM module loading (import maps / esm.sh) used by
                    # site-builder previews like Aura.
                ],
            )

            context = self._stealth_context(browser)
            page = context.new_page()

            # Intercept all responses and store body+content-type
            def on_response(response):
                try:
                    url = response.url
                    if response.status not in (200, 203, 206):
                        return
                    if url.startswith(("data:", "blob:")):
                        return
                    if self._should_skip(url):
                        return
                    ct = response.headers.get("content-type", "")
                    ct_base = ct.split(";")[0].strip().lower()
                    is_heavy_media = ct_base.startswith(("video/", "audio/"))
                    try:
                        body = response.body()
                    except Exception:
                        return
                    if not body:
                        return
                    if is_heavy_media and len(body) > 5 * 1024 * 1024:
                        return
                    if len(body) > MAX_ASSET_BYTES:
                        return
                    data = {"body": body, "content_type": ct}
                    self._captured[url] = data
                    # Also store under the original request URL (handles redirects)
                    try:
                        req_url = response.request.url
                        if req_url != url:
                            self._captured[req_url] = data
                    except Exception:
                        pass
                except Exception:
                    pass

            page.on("response", on_response)

            # Navigate
            self.log(f"🌐 Carregando {self.url}...")
            self._navigate(page, self.url)
            page.wait_for_timeout(3000)

            # Handle iframe-wrapper sites (Aura, Webflow previews, etc.)
            # This approach stays on the outer page so all frame responses are
            # captured, and the frame's JS app renders in its original context.
            iframe_html, iframe_base = self._extract_iframe_content(page)

            if iframe_html:
                html_content = iframe_html
                self._base_url = iframe_base or page.url
                self.log(f"✓ URL base: {self._base_url}")
                self._is_csr = self._detect_csr(html_content)
            else:
                self._base_url = page.url
                self.log(f"✓ URL base: {self._base_url}")

                # Scroll to trigger lazy loading (only for non-iframe pages)
                self.log("📜 Rolando para carregar conteúdo lazy...")
                self._scroll_for_lazy_load(page)

                # One final wait for post-scroll network activity
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)

                # Materialise CSSOM-only stylesheets (styled-components, emotion,
                # adoptedStyleSheets) so page.content() can serialise them.
                # Without this step, post-hydration styles vanish from the
                # snapshot and the offline page renders unstyled.
                try:
                    self._serialize_runtime_stylesheets(page)
                except Exception as exc:
                    self.log(f"⚠️  Serializar CSSOM: {exc}")

                html_content = page.content()
                # The rendered DOM is always full for SPAs (React already mounted),
                # so also probe the raw server HTML to catch Vite/CRA/Next shells.
                # Next.js App Router pages are SSR'd (body has content) but their
                # hydration runtime breaks offline just like CSR — treat as CSR.
                self._is_csr = (
                    self._detect_csr(html_content)
                    or self._detect_csr_from_origin()
                    or self._detect_nextjs_app_router(html_content)
                )

            self.log(f"📦 {len(self._captured)} recursos de rede capturados")
            if self._is_csr:
                self.log("⚠️  App CSR detectado (conteúdo renderizado pelo JS)")

            try:
                page.close()
                context.close()
                browser.close()
            except Exception:
                pass

        # ── Phase 2: Persist all captured assets ──────────────────────────────
        self.log(f"💾 Salvando {len(self._captured)} recursos capturados...")

        for url, data in self._captured.items():
            self._save_asset(url, data["body"], data["content_type"])

        self.log(f"   ✅ {len(self._url_map)} assets únicos em disco")

        # ── Phase 2.5: Fallback download for assets not captured by Playwright ─
        self.log("⬇️  Verificando assets ainda remotos no DOM...")
        pending_urls = self._collect_remote_urls(html_content, self._base_url)
        fallback_count = self._fallback_download(pending_urls)
        if fallback_count:
            self.log(f"   ✅ {fallback_count} assets baixados via fallback")
        else:
            self.log("   ✅ Nenhum asset adicional necessário")

        # ── Phase 2.7: Resolve Vite dynamic-import chunks ─────────────────────
        # Catches lazy-loaded bundles (Unicorn Studio, Sandpack, etc.) whose
        # dynamic imports never fire during the capture window.
        self.log("🧩 Resolvendo dynamic imports (chunks Vite)...")
        chunks_saved = self._resolve_vite_chunks()
        if chunks_saved:
            self.log(f"   ✅ {chunks_saved} chunks dinâmicos baixados")
        else:
            self.log("   ✅ Nenhum chunk dinâmico necessário")

        # ── Phase 3: Rewrite saved CSS files ─────────────────────────────────
        css_files = [f for f in os.listdir(self.assets_dir) if f.endswith(".css")]
        self.log(f"🎨 Reescrevendo URLs em {len(css_files)} arquivo(s) CSS...")

        local_to_orig: dict[str, str] = {}
        for orig_url, local in self._url_map.items():
            if local not in local_to_orig:
                local_to_orig[local] = orig_url

        rewritten_css = 0
        for filename in css_files:
            filepath = os.path.join(self.assets_dir, filename)
            local_rel = f"assets/{filename}"
            original_url = local_to_orig.get(local_rel, self._base_url)
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                rewritten = self._rewrite_css(text, original_url, in_assets=True)
                if rewritten != text:
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(rewritten)
                    rewritten_css += 1
            except Exception as exc:
                self.log(f"⚠️  CSS {filename}: {exc}")

        self.log(f"   ✅ {rewritten_css} arquivo(s) CSS reescritos")

        # ── Phase 4: Rewrite & save HTML ──────────────────────────────────────
        self.log("🔧 Processando HTML...")
        final_html = self._rewrite_html(html_content, self._base_url)

        with open(os.path.join(self.output_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(final_html)

        asset_count = len(os.listdir(self.assets_dir))
        self.log(f"✅ Concluído! {asset_count} arquivos em assets/")
        return True
