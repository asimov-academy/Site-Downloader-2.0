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
        return css_text

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
        try:
            body_len = page.evaluate("() => document.body.innerText.trim().length")
            # Count ALL iframes regardless of src (src may be set dynamically by JS)
            iframe_count = page.evaluate("""
                () => document.querySelectorAll('iframe').length
            """)
            # Also check for fullscreen iframes via CSS cover ratio
            cover_ratio_max = page.evaluate("""
                () => Math.max(0, ...Array.from(document.querySelectorAll('iframe')).map(f =>
                    (f.offsetWidth * f.offsetHeight) / (window.innerWidth * window.innerHeight)
                ))
            """)
        except Exception:
            return None, None

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
        divs = body.find_all("div")
        # CSR signal: very little text AND only 1-2 top-level divs (SPA root shell)
        return len(text) < 50 and len(divs) <= 3

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

        # ── <script src> ──────────────────────────────────────────────────────
        self.log("📝 Processando scripts...")

        if self._is_csr:
            # CSR app: JS makes API calls that will fail offline and blank the page.
            # The rendered HTML is already in the DOM — strip all scripts to preserve it.
            removed = 0
            for tag in soup.find_all("script"):
                tag.decompose()
                removed += 1
            for tag in soup.find_all("link", rel=lambda r: r and any(
                x in (r if isinstance(r, str) else " ".join(r))
                for x in ("preload", "modulepreload", "prefetch")
            )):
                tag.decompose()
            self.log(f"   🛡️  App CSR detectado — {removed} scripts removidos (conteúdo já no DOM)")
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

        # ── Inject offline-compatibility CSS + reveal-fallback JS ─────────────
        head = soup.find("head")
        if head:
            style = soup.new_tag("style")
            style["data-offline"] = "1"
            style.string = (
                "/* offline: ensure content is visible regardless of JS init state */\n"
                "html,body{opacity:1!important;visibility:visible!important}\n"
                ".page-loader,.site-loader,[class*='loading-screen'],"
                "[id*='loading-screen']{display:none!important}\n"
            )
            head.append(style)

        body = soup.find("body")
        if body:
            # Build the image-only URL → local-path map. We use this client-side
            # to handle two cases that pure HTML rewriting can't:
            #   1. Sites whose JS (Next.js Image, dynamic React img setters…) re-
            #      writes <img src> *after* hydration to URLs like
            #      `/_next/image?url=…&w=…&q=…` — those resolve to file:// in the
            #      saved page and 404.
            #   2. Bare CDN URLs (Sanity, Contentful) substituted in by client-side
            #      JS even though we already rewrote the static src in HTML.
            import json as _json
            image_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
                          ".avif", ".ico", ".bmp")
            image_map = {
                orig: local
                for orig, local in self._url_map.items()
                if local.lower().endswith(image_exts)
            }
            image_map_json = _json.dumps(image_map)

            # Combined runtime fixer:
            #   • Reveal: undoes `opacity:0` initial states left behind by GSAP
            #     ScrollTrigger when scroll triggers don't fire offline.
            #   • Image rewriter: intercepts <img src/srcset> and resolves either
            #     direct CDN URLs or `/_next/image?url=…` wrappers to local files.
            fix = soup.new_tag("script")
            fix["data-offline-fix"] = "1"
            fix.string = (
                "(function(){\n"
                "var IMG_MAP = " + image_map_json + ";\n"
                "// Pre-populate path+query keys so file:// lookups succeed even when\n"
                "// the original map is keyed by the captured https://… URL.\n"
                "var _add = {};\n"
                "for (var _k in IMG_MAP) {\n"
                "  try { var _u = new URL(_k); _add[_u.pathname + _u.search] = IMG_MAP[_k]; } catch(e){}\n"
                "}\n"
                "for (var _k in _add) if (!IMG_MAP[_k]) IMG_MAP[_k] = _add[_k];\n"
                "function resolveLocal(u){\n"
                "  if (!u) return null;\n"
                "  if (IMG_MAP[u]) return IMG_MAP[u];\n"
                "  try {\n"
                "    var url = new URL(u, location.href);\n"
                "    // Path+query lookup (handles file:// resolution mismatch)\n"
                "    var pq = url.pathname + url.search;\n"
                "    if (IMG_MAP[pq]) return IMG_MAP[pq];\n"
                "    // Next.js image optimization endpoint — peel the inner CDN URL\n"
                "    if (/_next\\/image$/.test(url.pathname)) {\n"
                "      var t = url.searchParams.get('url');\n"
                "      if (t) {\n"
                "        var decoded = decodeURIComponent(t);\n"
                "        if (IMG_MAP[decoded]) return IMG_MAP[decoded];\n"
                "        var bare = decoded.split('?')[0];\n"
                "        for (var k in IMG_MAP) {\n"
                "          if (k.split('?')[0] === bare) return IMG_MAP[k];\n"
                "        }\n"
                "      }\n"
                "    }\n"
                "  } catch(e){}\n"
                "  return null;\n"
                "}\n"
                "function rewriteSrcset(s){\n"
                "  if (!s || s.indexOf('http') === -1 && s.indexOf('/_next') === -1) return s;\n"
                "  return s.split(',').map(function(it){\n"
                "    var p = it.trim().split(/\\s+/);\n"
                "    var loc = resolveLocal(p[0]);\n"
                "    if (loc) p[0] = loc;\n"
                "    return p.join(' ');\n"
                "  }).join(', ');\n"
                "}\n"
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
                "function fixAll(){\n"
                "  document.querySelectorAll('img').forEach(fixImg);\n"
                "}\n"
                "function reveal(){\n"
                "  var n = 0;\n"
                "  document.querySelectorAll('[style]').forEach(function(el){\n"
                "    var s = el.style;\n"
                "    if (s.opacity === '0' && el.offsetParent !== null) {\n"
                "      s.opacity = '1';\n"
                "      if (s.transform) s.transform = 'none';\n"
                "      if (s.translate) s.translate = 'none';\n"
                "      if (s.rotate)    s.rotate = 'none';\n"
                "      if (s.scale)     s.scale = 'none';\n"
                "      n++;\n"
                "    }\n"
                "  });\n"
                "  if (window.console && n) console.log('[offline-fix] revealed', n);\n"
                "}\n"
                "// Initial sweep\n"
                "fixAll();\n"
                "// React/Next.js will re-render imgs after hydration — watch for that\n"
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
                "// Periodic re-scan (catches src updates we missed)\n"
                "setTimeout(fixAll, 1000);\n"
                "setTimeout(fixAll, 3000);\n"
                "// Wait for animations to play before forcing visibility\n"
                "var go = function(){ setTimeout(reveal, 5000); };\n"
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

                html_content = page.content()
                self._is_csr = self._detect_csr(html_content)

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

        # Helper: small launcher script so the user can view the captured site
        # over HTTP (some browsers block ESM/CORS over file://).
        self._write_serve_script()

        asset_count = len(os.listdir(self.assets_dir))
        self.log(f"✅ Concluído! {asset_count} arquivos em assets/")
        self.log("ℹ️  Para visualizar: cd no diretório e rode `python3 serve.py`")
        return True

    def _write_serve_script(self) -> None:
        """Write a minimal HTTP server launcher next to index.html."""
        script = (
            "#!/usr/bin/env python3\n"
            '"""Launch a local HTTP server and open the captured site in the browser."""\n'
            "import http.server, socketserver, webbrowser, os, sys\n"
            "\n"
            "PORT = 8765\n"
            "os.chdir(os.path.dirname(os.path.abspath(__file__)))\n"
            "\n"
            "class Handler(http.server.SimpleHTTPRequestHandler):\n"
            "    def log_message(self, *a, **k): pass\n"
            "\n"
            "with socketserver.TCPServer(('', PORT), Handler) as httpd:\n"
            "    url = f'http://localhost:{PORT}/'\n"
            "    print(f'→ {url}')\n"
            "    try: webbrowser.open(url)\n"
            "    except Exception: pass\n"
            "    try: httpd.serve_forever()\n"
            "    except KeyboardInterrupt: print('\\nbye')\n"
        )
        path = os.path.join(self.output_dir, "serve.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        try:
            os.chmod(path, 0o755)
        except Exception:
            pass
