"""Scrape PDFs from a manually-supplied list of sites (URD 3.2, jalur 1).

Deliberately *not* an auto-crawler: URD 4.2 puts scheduled crawling
out of scope, so this only walks the pages an operator explicitly registered,
one hop deep at most via ``follow_patterns`` (plus an optional sitemap seed —
still scoped to that one registered site, never a general discovery step).

Beyond the basic fetch, this module adds the things a scraper needs to run
unattended against a real government site over many repeats:
  * retries with exponential backoff for transient failures (timeouts,
    connection resets, 429/502/503/504) instead of losing a whole site to
    one bad response;
  * per-host rate limiting so several configured sites are not serialised
    behind one global delay, while any single host is never hit faster than
    ``delay_seconds`` allows;
  * URL normalisation so the same page/PDF reached through different query
    strings or tracking parameters is not treated as new;
  * an optional sitemap.xml seed and a bounded HEAD-based sniff of
    extension-less "download" links, since many CMS-driven regulator sites
    serve PDFs without a `.pdf` suffix;
  * a conditional-GET cache (ETag / Last-Modified) so a repeat run can skip
    re-downloading a listing page the server reports as unchanged.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import urllib.robotparser as robotparser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlsplit

import requests
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from hero.config import ScraperSettings, SiteSource
from hero.models import sha256_bytes

log = logging.getLogger(__name__)

PDF_CONTENT_TYPES = {"application/pdf", "application/x-pdf",
                     "application/octet-stream", "binary/octet-stream"}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\- ]+")
_MAX_NAME = 150

# Query parameters that identify the visitor/campaign, never the resource —
# safe to drop everywhere without risking a broken link.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "gclsrc", "mc_cid", "mc_eid", "_ga", "ref", "ref_src",
    "spm", "igshid",
}
# A link whose text or URL contains one of these is worth a HEAD-request
# sniff even without a .pdf extension — common on CMS "download handler" URLs.
_DOWNLOAD_HINTS = ("download", "unduh", "getfile", "getdoc", "attachment",
                   "viewfile", "dokumen", "lampiran")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = exc.response
        return resp is not None and resp.status_code in _RETRYABLE_STATUS
    return False


@dataclass
class PdfLink:
    url: str
    anchor_text: str = ""
    found_on: str = ""

    def __hash__(self) -> int:
        return hash(self.url)


@dataclass
class FetchResult:
    link: PdfLink
    ok: bool
    path: Path | None = None
    sha256: str | None = None
    size_bytes: int = 0
    reason: str | None = None


@dataclass
class SiteReport:
    site: str
    pages_visited: int = 0
    pages_cached: int = 0    # served from the conditional-GET cache (304)
    links_found: int = 0
    fetched: list[FetchResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_known: int = 0   # already in the knowledge base, not re-downloaded

    @property
    def ok_count(self) -> int:
        return sum(1 for f in self.fetched if f.ok)


class PageCache(Protocol):
    """What the scraper needs from a cache backend — matches ``Catalog``."""

    def get_page_cache(self, url: str) -> object | None: ...
    def put_page_cache(self, url: str, etag: str | None,
                       last_modified: str | None, content_hash: str | None,
                       body: str | None, status_code: int) -> None: ...


def safe_filename(url: str, fallback: str = "document.pdf") -> str:
    """Derive a filesystem-safe .pdf name from a URL."""
    name = unquote(Path(urlsplit(url).path).name) or fallback
    name = _SAFE_NAME.sub("_", name).strip(" ._") or fallback
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    if len(name) > _MAX_NAME:
        name = name[: _MAX_NAME - 4] + ".pdf"
    return name


def filename_from_disposition(header: str | None) -> str | None:
    """Extract a safe .pdf filename from a Content-Disposition header.

    Handles both ``filename="x.pdf"`` and RFC 5987 ``filename*=UTF-8''x.pdf``.
    Returns ``None`` when the header is absent or carries nothing usable, so
    the caller can fall back to a URL-derived name.
    """
    if not header:
        return None
    raw: str | None = None
    ext = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", header, re.IGNORECASE)
    if ext:
        raw = unquote(ext.group(1).strip())
    else:
        basic = re.search(r'filename\s*=\s*"([^"]+)"', header, re.IGNORECASE) \
            or re.search(r"filename\s*=\s*([^;]+)", header, re.IGNORECASE)
        if basic:
            raw = basic.group(1).strip().strip('"')
    if not raw:
        return None
    # Strip any path components a server might include before sanitising.
    raw = raw.replace("\\", "/").split("/")[-1]
    name = _SAFE_NAME.sub("_", raw).strip(" ._")
    if not name:
        return None
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    if len(name) > _MAX_NAME:
        name = name[: _MAX_NAME - 4] + ".pdf"
    return name


def normalize_url(url: str) -> str:
    """Canonicalise a URL so the same resource dedups regardless of noise.

    Drops the fragment, strips known tracking parameters, sorts what is
    left, and lower-cases the scheme/host (paths keep their case — many
    servers are case-sensitive there).
    """
    parsed = urlparse(url)
    query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query.sort()
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return parsed._replace(
        scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(),
        path=path, query=urlencode(query), fragment="",
    ).geturl()


def _matches(text: str, patterns: list[str]) -> bool:
    low = text.lower()
    return any(p.lower() in low for p in patterns)


def discover_pdf_links(
    html: str, base_url: str, site: SiteSource | None = None
) -> tuple[list[PdfLink], list[str], list[PdfLink]]:
    """Return (pdf links, same-host pages to follow, ambiguous candidates).

    Ambiguous candidates look like a download but lack a `.pdf` extension —
    the caller decides whether to spend a HEAD request confirming each one.
    """
    soup = BeautifulSoup(html, "lxml")
    base_host = urlparse(base_url).netloc
    pdfs: dict[str, PdfLink] = {}
    ambiguous: dict[str, PdfLink] = {}
    follow: list[str] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            continue
        anchor = " ".join(a.get_text(" ", strip=True).split())[:250]
        clean = normalize_url(url)
        path_low = parsed.path.lower()

        looks_pdf = path_low.endswith(".pdf") or ".pdf?" in url.lower() \
            or "pdf" in (a.get("type") or "").lower()
        haystack = f"{url} {anchor}"

        if looks_pdf:
            if site:
                if site.include_patterns and not _matches(haystack, site.include_patterns):
                    continue
                if site.exclude_patterns and _matches(haystack, site.exclude_patterns):
                    continue
            pdfs.setdefault(clean, PdfLink(clean, anchor, base_url))
        elif (site is None or site.sniff_ambiguous_links) \
                and parsed.netloc == base_host and _matches(haystack, _DOWNLOAD_HINTS):
            if site:
                if site.include_patterns and not _matches(haystack, site.include_patterns):
                    continue
                if site.exclude_patterns and _matches(haystack, site.exclude_patterns):
                    continue
            ambiguous.setdefault(clean, PdfLink(clean, anchor, base_url))
        elif site and site.follow_patterns and parsed.netloc == base_host:
            if _matches(url, site.follow_patterns):
                follow.append(clean)

    # Preserve order while de-duplicating follow targets.
    seen: set[str] = set()
    follow = [u for u in follow if not (u in seen or seen.add(u))]
    return list(pdfs.values()), follow, list(ambiguous.values())


def discover_sitemap_urls(
    xml_bytes: bytes, follow_patterns: list[str], limit: int = 500
) -> list[str]:
    """Extract <loc> entries from a sitemap or sitemap-index document.

    One level of sitemap-index nesting is expanded inline by the caller
    (this function itself only reads whichever document it was given);
    entries are pre-filtered by ``follow_patterns`` so a site's full sitemap
    doesn't flood the crawl queue with unrelated pages.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        log.warning("could not parse sitemap XML: %s", exc)
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [el.text.strip() for el in root.findall(".//sm:loc", ns) if el.text] \
        or [el.text.strip() for el in root.findall(".//loc") if el.text]
    if follow_patterns:
        locs = [u for u in locs if _matches(u, follow_patterns) or u.lower().endswith(".pdf")]
    return locs[:limit]


class WebScraper:
    """Polite HTTP client: shared session, robots.txt, retries, per-host rate limiting."""

    def __init__(self, settings: ScraperSettings | None = None):
        self.settings = settings or ScraperSettings()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
            "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
        })
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._host_last_request: dict[str, float] = {}
        self._host_lock = threading.Lock()

    # -- politeness ------------------------------------------------------
    def _throttle(self, url: str) -> None:
        """Sleep, if needed, so this host sees at most one request per
        ``delay_seconds``. Locking around the whole wait means concurrent
        downloads to the *same* host still queue up correctly instead of
        racing past the check simultaneously."""
        host = urlparse(url).netloc
        with self._host_lock:
            last = self._host_last_request.get(host, 0.0)
            wait = self.settings.delay_seconds - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            self._host_last_request[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        """Check robots.txt, fetched through ``requests`` (not stdlib
        ``robotparser.read()``, which opens a socket with no timeout at all
        — a single slow or black-holed robots.txt would otherwise freeze
        every future request to that host, and every site queued after it,
        for good). A missing OR unreachable robots.txt is treated the same
        way: allowed, with the failure logged so it is visible, not silent.
        """
        if not self.settings.respect_robots:
            return True
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        if root not in self._robots:
            self._robots[root] = self._fetch_robots(root)
        rp = self._robots[root]
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.settings.user_agent, url)
        except Exception:  # noqa: BLE001
            return True

    def _fetch_robots(self, root: str) -> robotparser.RobotFileParser | None:
        robots_url = urljoin(root, "/robots.txt")
        try:
            resp = self.session.get(
                robots_url, timeout=min(self.settings.request_timeout, 10))
        except requests.RequestException as exc:
            log.warning(
                "robots.txt unreachable for %s (%s) — treating as allowed", root, exc)
            return None
        if resp.status_code >= 400:
            return None  # no robots.txt published: nothing to restrict
        rp = robotparser.RobotFileParser()
        try:
            rp.parse(resp.text.splitlines())
        except Exception as exc:  # noqa: BLE001 - malformed robots.txt
            log.warning("could not parse robots.txt for %s (%s) — treating "
                       "as allowed", root, exc)
            return None
        return rp

    # -- transport ---------------------------------------------------
    def _retrying(self):
        """Build a ``tenacity`` retry controller from the current settings.

        Built fresh per call (not a decorator) so ``max_retries`` stays a
        runtime, per-instance value rather than baked in at import time.
        """
        return retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(max(1, self.settings.max_retries)),
            wait=wait_exponential_jitter(initial=self.settings.retry_backoff_seconds,
                                         max=30),
            reraise=True,
        )

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        @self._retrying()
        def _do() -> requests.Response:
            resp = self.session.request(
                method, url, timeout=self.settings.request_timeout,
                verify=self.settings.verify_tls, **kwargs,
            )
            if resp.status_code in _RETRYABLE_STATUS:
                resp.raise_for_status()
            return resp
        return _do()

    def head(self, url: str) -> requests.Response | None:
        if not self.allowed(url):
            return None
        self._throttle(url)
        try:
            return self._request("HEAD", url, allow_redirects=True)
        except requests.RequestException as exc:
            log.debug("HEAD %s failed: %s", url, exc)
            return None

    def get_html(self, url: str, cache: PageCache | None = None) -> tuple[str | None, bool]:
        """Fetch a page's HTML. Returns (html_or_None, served_from_cache).

        When ``cache`` is given and the site returns 304 Not Modified for a
        conditional GET, the previously stored body is returned unchanged —
        the page counts as visited but no bandwidth was spent re-reading it.
        """
        if not self.allowed(url):
            log.warning("robots.txt disallows %s", url)
            return None, False

        headers: dict[str, str] = {}
        cached_row = cache.get_page_cache(url) if cache else None
        if cached_row is not None:
            if cached_row["etag"]:
                headers["If-None-Match"] = cached_row["etag"]
            if cached_row["last_modified"]:
                headers["If-Modified-Since"] = cached_row["last_modified"]

        self._throttle(url)
        try:
            resp = self._request("GET", url, allow_redirects=True, headers=headers)
        except requests.RequestException as exc:
            log.warning("GET %s failed: %s", url, exc)
            return None, False

        if resp.status_code == 304 and cached_row is not None:
            return cached_row["body"], True

        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("GET %s failed: %s", url, exc)
            return None, False

        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype and "xml" not in ctype:
            log.debug("skipping non-HTML %s (%s)", url, ctype)
            return None, False
        resp.encoding = resp.encoding or resp.apparent_encoding
        text = resp.text

        if cache is not None:
            cache.put_page_cache(
                url, resp.headers.get("ETag"), resp.headers.get("Last-Modified"),
                sha256_bytes(text.encode("utf-8", "ignore"))[:16],
                text, resp.status_code,
            )
        return text, False

    def get_sitemap(self, url: str) -> bytes | None:
        if not self.allowed(url):
            return None
        self._throttle(url)
        try:
            resp = self._request("GET", url)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            log.debug("sitemap GET %s failed: %s", url, exc)
            return None

    def post_html(self, url: str, data: dict) -> str | None:
        """POST a form and return the HTML response.

        Needed for ASP.NET WebForms pagers (ojk.go.id), where "next page" is
        a postback of the page's hidden state rather than a link. Same
        politeness, throttle and retry policy as every GET.
        """
        if not self.allowed(url):
            log.warning("robots.txt disallows %s", url)
            return None
        self._throttle(url)
        try:
            resp = self._request("POST", url, data=data, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("POST %s failed: %s", url, exc)
            return None
        resp.encoding = resp.encoding or resp.apparent_encoding
        return resp.text

    def request_json(self, url: str) -> dict | list | None:
        """GET a JSON endpoint, with the same politeness and retry policy.

        Needed by portals whose document list is an AJAX grid rather than
        links in the HTML (see ``hero.ingest.jdih``).
        """
        if not self.allowed(url):
            log.warning("robots.txt disallows %s", url)
            return None
        self._throttle(url)
        resp = self._request(
            "GET", url, allow_redirects=True,
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Accept": "application/json, text/javascript, */*"},
        )
        resp.raise_for_status()
        return resp.json()

    def sniff_is_pdf(self, url: str) -> bool:
        """Confirm an extension-less link is really a PDF via a HEAD request."""
        resp = self.head(url)
        if resp is None:
            return False
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        return ctype in PDF_CONTENT_TYPES or ctype == "application/pdf"

    def download_pdf(self, link: PdfLink, dest_dir: Path,
                     accept_zip: bool = False) -> FetchResult:
        """Stream a PDF to ``dest_dir``, validating type and magic bytes."""
        if not self.allowed(link.url):
            return FetchResult(link, False, reason="blocked by robots.txt")

        dest_dir.mkdir(parents=True, exist_ok=True)
        self._throttle(link.url)
        max_bytes = self.settings.max_file_mb * 1024 * 1024
        try:
            with self._request("GET", link.url, stream=True,
                               allow_redirects=True) as resp:
                resp.raise_for_status()
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                disposition_header = resp.headers.get("Content-Disposition")
                disposition_name = filename_from_disposition(disposition_header)
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    return FetchResult(
                        link, False,
                        reason=f"too large ({int(declared) // 1048576} MB)")

                chunks, total = [], 0
                for chunk in resp.iter_content(1 << 16):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        return FetchResult(
                            link, False,
                            reason=f"exceeded {self.settings.max_file_mb} MB limit")
                data = b"".join(chunks)
        except requests.RequestException as exc:
            return FetchResult(link, False, reason=f"download failed: {exc}")

        if not data:
            return FetchResult(link, False, reason="empty response")
        # Content-Type lies often enough that the magic bytes are the real check.
        # ZIP is accepted only when the caller expects an archive (OJK drafts).
        is_zip = accept_zip and data[:4] == b"PK\x03\x04"
        if b"%PDF-" not in data[:1024] and not is_zip:
            return FetchResult(
                link, False,
                reason=f"not a PDF (content-type={ctype or 'unknown'})")

        digest = sha256_bytes(data)
        if is_zip:
            raw = (filename_from_disposition(disposition_header) or "").removesuffix(".pdf") \
                or unquote(Path(urlsplit(link.url).path).stem) or "arsip"
            name = _SAFE_NAME.sub("_", raw).strip(" ._")[: _MAX_NAME - 4] + ".zip"
        else:
            # Prefer the server's own filename: portals that serve PDFs from
            # a GUID endpoint (jdih.ojk.go.id) carry the only human-readable
            # name in Content-Disposition, and a URL-derived name would make
            # every download land as "DownloadDokumen.pdf".
            name = disposition_name or safe_filename(link.url)
        # Hash-prefixed name keeps concurrent downloads from colliding.
        path = dest_dir / f"{digest[:12]}_{name}"
        path.write_bytes(data)
        return FetchResult(link, True, path=path, sha256=digest, size_bytes=len(data))

    # -- orchestration ---------------------------------------------------
    def harvest_site(
        self, site: SiteSource, dest_dir: Path, limit: int | None = None,
        skip_url: Callable[[str], bool] | None = None,
        cache: PageCache | None = None,
    ) -> SiteReport:
        """Visit the site's entry page (+ followed pages) and fetch its PDFs.

        ``skip_url`` is consulted before each download so a PDF already in
        the knowledge base is not fetched again. ``cache`` enables
        conditional GET on the HTML pages walked to find those PDFs.
        """
        report = SiteReport(site=site.name)
        budget = limit if limit is not None else site.max_documents

        page_budget = max(site.max_pages, 1)
        seed = normalize_url(site.url)
        queue: list[str] = [seed]
        queued: set[str] = {seed}

        if site.sitemap_url:
            sitemap_bytes = self.get_sitemap(site.sitemap_url)
            if sitemap_bytes is None:
                report.errors.append(f"could not read sitemap: {site.sitemap_url}")
            else:
                for u in discover_sitemap_urls(sitemap_bytes, site.follow_patterns):
                    nu = normalize_url(u)
                    if nu not in queued and len(queued) < page_budget:
                        queued.add(nu)
                        queue.append(nu)

        visited: set[str] = set()
        links: list[PdfLink] = []
        seen_urls: set[str] = set()

        # Breadth-first over the entry page, its sitemap seeds, and the
        # pages they link to, capped by page_budget so a listing page cannot
        # pull in the whole site.
        while queue and report.pages_visited < page_budget:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            html, from_cache = self.get_html(url, cache=cache)
            report.pages_visited += 1
            if from_cache:
                report.pages_cached += 1
            if html is None:
                report.errors.append(f"could not read page: {url}")
                continue
            found, follow, ambiguous = discover_pdf_links(html, url, site)

            if ambiguous and site.sniff_ambiguous_links:
                # Bounded: a HEAD storm defeats the point of being polite.
                for cand in ambiguous[:10]:
                    if self.sniff_is_pdf(cand.url):
                        found.append(cand)

            for lk in found:
                if lk.url not in seen_urls:
                    seen_urls.add(lk.url)
                    links.append(lk)
            # Stop queueing once enough PDFs are in hand or the budget is spent.
            if len(links) >= budget:
                continue
            room = page_budget - report.pages_visited - len(queue)
            for candidate in follow:
                if room <= 0:
                    break
                if candidate not in queued:
                    queued.add(candidate)
                    queue.append(candidate)
                    room -= 1

        report.links_found = len(links)
        to_fetch: list[PdfLink] = []
        for link in links:
            if len(to_fetch) >= budget:
                break
            if skip_url is not None and skip_url(link.url):
                report.skipped_known += 1
                continue
            to_fetch.append(link)

        # Concurrent downloads: each still passes through the per-host
        # throttle lock, so a burst of links on one host is not actually
        # requested faster than delay_seconds allows — the concurrency only
        # pays off when a site's PDFs are spread across multiple hosts/CDNs.
        workers = max(1, self.settings.max_concurrent_downloads)
        if workers == 1 or len(to_fetch) <= 1:
            report.fetched = [self.download_pdf(lk, dest_dir) for lk in to_fetch]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                report.fetched = list(pool.map(
                    lambda lk: self.download_pdf(lk, dest_dir), to_fetch))
        return report

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> WebScraper:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
