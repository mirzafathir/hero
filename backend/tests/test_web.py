"""PDF link discovery and download validation (no network)."""
import pytest
import requests

from hero.config import ScraperSettings, SiteSource
from hero.ingest.web import (
    PdfLink, WebScraper, discover_pdf_links, discover_sitemap_urls,
    filename_from_disposition, normalize_url, safe_filename,
)

HTML = """
<html><body>
  <a href="/docs/POJK%209%20Tahun%202026.pdf">POJK 9 Tahun 2026</a>
  <a href="/docs/Abstrak POJK 9.pdf">Abstrak POJK 9</a>
  <a href="https://other.example.com/x.pdf">External PDF</a>
  <a href="/id/regulasi/Pages/POJK-9.aspx">Detail POJK 9</a>
  <a href="/id/berita/Pages/siaran-pers.aspx">Siaran Pers</a>
  <a href="#top">anchor</a>
  <a href="mailto:a@b.c">mail</a>
</body></html>
"""
BASE = "https://www.ojk.go.id/id/regulasi/Default.aspx"


def test_discovers_pdfs_and_follow_targets():
    site = SiteSource(name="t", url=BASE, follow_patterns=["/regulasi/Pages/"])
    pdfs, follow, _ambiguous = discover_pdf_links(HTML, BASE, site)
    urls = {p.url for p in pdfs}
    assert any("POJK%209%20Tahun%202026.pdf" in u for u in urls)
    assert any("other.example.com" in u for u in urls)   # cross-host PDFs are fine
    assert follow == ["https://www.ojk.go.id/id/regulasi/Pages/POJK-9.aspx"]


def test_exclude_patterns_drop_companion_documents():
    site = SiteSource(name="t", url=BASE, exclude_patterns=["abstrak"])
    pdfs, _, _ = discover_pdf_links(HTML, BASE, site)
    assert not any("Abstrak" in p.anchor_text for p in pdfs)


def test_include_patterns_are_a_whitelist():
    site = SiteSource(name="t", url=BASE, include_patterns=["pojk"])
    pdfs, _, _ = discover_pdf_links(HTML, BASE, site)
    assert pdfs and all("pojk" in (p.url + p.anchor_text).lower() for p in pdfs)


def test_follow_targets_stay_on_the_same_host():
    site = SiteSource(name="t", url=BASE, follow_patterns=["Pages"])
    _, follow, _ = discover_pdf_links(HTML, BASE, site)
    assert all(u.startswith("https://www.ojk.go.id") for u in follow)


def test_ambiguous_download_links_are_flagged_not_followed():
    html = """
    <html><body>
      <a href="/download?id=42">Unduh Peraturan</a>
      <a href="/id/berita/Pages/siaran-pers.aspx">Siaran Pers</a>
    </body></html>
    """
    site = SiteSource(name="t", url=BASE, follow_patterns=["/regulasi/Pages/"])
    pdfs, follow, ambiguous = discover_pdf_links(html, BASE, site)
    assert not pdfs
    assert not follow
    assert any("download?id=42" in a.url for a in ambiguous)


def test_ambiguous_sniffing_can_be_disabled():
    html = '<a href="/download?id=42">Unduh Peraturan</a>'
    site = SiteSource(name="t", url=BASE, sniff_ambiguous_links=False)
    _, _, ambiguous = discover_pdf_links(html, BASE, site)
    assert ambiguous == []


@pytest.mark.parametrize("raw,expected", [
    ("https://X.com/a/?utm_source=fb&b=2&a=1", "https://x.com/a?a=1&b=2"),
    ("https://x.com/a/", "https://x.com/a"),
    ("https://x.com/a#section", "https://x.com/a"),
    ("https://x.com/", "https://x.com/"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_discover_sitemap_urls_filters_by_pattern():
    xml = b"""<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://x.com/id/regulasi/Pages/a.aspx</loc></url>
      <url><loc>https://x.com/id/berita/b.aspx</loc></url>
      <url><loc>https://x.com/files/c.pdf</loc></url>
    </urlset>"""
    urls = discover_sitemap_urls(xml, follow_patterns=["/regulasi/Pages/"])
    assert "https://x.com/id/regulasi/Pages/a.aspx" in urls
    assert "https://x.com/files/c.pdf" in urls          # .pdf always kept
    assert "https://x.com/id/berita/b.aspx" not in urls  # doesn't match pattern


def test_discover_sitemap_urls_handles_malformed_xml():
    assert discover_sitemap_urls(b"not xml at all", follow_patterns=[]) == []


@pytest.mark.parametrize("url,expected", [
    ("https://x/id/POJK%209%20Tahun%202026.pdf", "POJK 9 Tahun 2026.pdf"),
    ("https://x/a/b/", "b.pdf"),
    ("https://x/report", "report.pdf"),
    ("https://x/a<b>c.pdf", "a_b_c.pdf"),
])
def test_safe_filename(url, expected):
    assert safe_filename(url) == expected


def test_download_rejects_non_pdf_payload(tmp_path, monkeypatch):
    scraper = WebScraper(ScraperSettings(delay_seconds=0.0, respect_robots=False))

    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "text/html"}

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            yield b"<html>404 not found</html>"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(scraper.session, "request", lambda *a, **k: FakeResponse())
    result = scraper.download_pdf(PdfLink("https://x/y.pdf"), tmp_path)
    assert not result.ok
    assert "not a PDF" in result.reason
    assert not list(tmp_path.iterdir())


def test_download_enforces_size_limit(tmp_path, monkeypatch):
    settings = ScraperSettings(delay_seconds=0.0, respect_robots=False, max_file_mb=1)
    scraper = WebScraper(settings)

    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "application/pdf",
                   "Content-Length": str(5 * 1024 * 1024)}

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            yield b"%PDF-" + b"0" * 1024

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(scraper.session, "request", lambda *a, **k: FakeResponse())
    result = scraper.download_pdf(PdfLink("https://x/big.pdf"), tmp_path)
    assert not result.ok
    assert "too large" in result.reason


def test_transient_failure_is_retried_then_succeeds(monkeypatch):
    """A 503 followed by a 200 must succeed, not bubble up as an error."""
    settings = ScraperSettings(delay_seconds=0.0, respect_robots=False,
                               max_retries=3, retry_backoff_seconds=0.01)
    scraper = WebScraper(settings)

    calls = {"n": 0}

    class FlakyResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.headers = {"Content-Type": "text/html"}
            self.text = "<html>ok</html>"
            self.apparent_encoding = "utf-8"
            self.encoding = "utf-8"

        def raise_for_status(self):
            if self.status_code >= 400:
                err = requests.exceptions.HTTPError(response=self)
                raise err

    def fake_request(method, url, **kwargs):
        calls["n"] += 1
        return FlakyResponse(503 if calls["n"] == 1 else 200)

    monkeypatch.setattr(scraper.session, "request", fake_request)
    html, from_cache = scraper.get_html("https://x.com/page")
    assert html == "<html>ok</html>"
    assert not from_cache
    assert calls["n"] == 2


def test_permanent_failure_gives_up_after_max_retries(monkeypatch):
    settings = ScraperSettings(delay_seconds=0.0, respect_robots=False,
                               max_retries=2, retry_backoff_seconds=0.01)
    scraper = WebScraper(settings)
    calls = {"n": 0}

    class DeadResponse:
        status_code = 503
        headers = {"Content-Type": "text/html"}

        def raise_for_status(self):
            raise requests.exceptions.HTTPError(response=self)

    def fake_request(method, url, **kwargs):
        calls["n"] += 1
        return DeadResponse()

    monkeypatch.setattr(scraper.session, "request", fake_request)
    html, from_cache = scraper.get_html("https://x.com/page")
    assert html is None
    assert calls["n"] == 2  # stopped at max_retries, did not loop forever


def test_conditional_get_uses_cached_body_on_304(monkeypatch):
    settings = ScraperSettings(delay_seconds=0.0, respect_robots=False)
    scraper = WebScraper(settings)

    class FakeCache:
        def __init__(self):
            self.stored = {"etag": '"abc"', "last_modified": None,
                           "body": "<html>cached body</html>"}

        def get_page_cache(self, url):
            return self.stored

        def put_page_cache(self, *a, **k):
            raise AssertionError("must not overwrite cache on a 304")

    class NotModified:
        status_code = 304
        headers = {}

    monkeypatch.setattr(scraper.session, "request",
                        lambda *a, **k: NotModified())
    html, from_cache = scraper.get_html("https://x.com/page", cache=FakeCache())
    assert html == "<html>cached body</html>"
    assert from_cache is True


def test_per_host_throttle_is_independent_per_host():
    """Two different hosts must not wait on each other's clock."""
    settings = ScraperSettings(delay_seconds=1.0, respect_robots=False)
    scraper = WebScraper(settings)
    start = __import__("time").monotonic()
    scraper._throttle("https://a.com/x")
    scraper._throttle("https://b.com/y")
    elapsed = __import__("time").monotonic() - start
    assert elapsed < 0.5   # neither call should have waited a full second


# -- robots.txt: must never hang, regardless of what the host does ---------
def test_robots_txt_fetch_has_a_bounded_timeout(monkeypatch):
    """A host whose robots.txt hangs (e.g. bi.go.id in practice) must not
    freeze the scraper — the fetch has to pass an explicit timeout, unlike
    stdlib robotparser.read(), which has none at all."""
    settings = ScraperSettings(delay_seconds=0.0, respect_robots=True,
                               request_timeout=5)
    scraper = WebScraper(settings)
    seen_timeout = {}

    def fake_get(url, timeout=None, **kwargs):
        seen_timeout["value"] = timeout
        raise requests.exceptions.Timeout("simulated hang")

    monkeypatch.setattr(scraper.session, "get", fake_get)
    assert scraper.allowed("https://x.com/page") is True   # fails open
    assert seen_timeout["value"] is not None
    assert seen_timeout["value"] <= 10


def test_missing_robots_txt_allows_everything(monkeypatch):
    settings = ScraperSettings(delay_seconds=0.0, respect_robots=True)
    scraper = WebScraper(settings)
    monkeypatch.setattr(scraper.session, "get",
                        lambda url, **kw: type("R", (), {"status_code": 404})())
    assert scraper.allowed("https://x.com/page") is True


def test_robots_txt_disallow_is_respected_when_reachable(monkeypatch):
    settings = ScraperSettings(delay_seconds=0.0, respect_robots=True)
    scraper = WebScraper(settings)
    body = "User-agent: *\nDisallow: /private/\n"

    class RobotsResp:
        status_code = 200
        text = body

    monkeypatch.setattr(scraper.session, "get", lambda url, **kw: RobotsResp())
    assert scraper.allowed("https://x.com/public/page") is True
    assert scraper.allowed("https://x.com/private/page") is False


def test_malformed_robots_txt_fails_open(monkeypatch):
    settings = ScraperSettings(delay_seconds=0.0, respect_robots=True)
    scraper = WebScraper(settings)

    class WeirdResp:
        status_code = 200
        text = "\x00\x01 not really a robots file %%%"

    monkeypatch.setattr(scraper.session, "get", lambda url, **kw: WeirdResp())
    assert scraper.allowed("https://x.com/page") is True


# -- Content-Disposition filenames ----------------------------------------
@pytest.mark.parametrize("header,expected", [
    # Parentheses are sanitised to underscores, same as safe_filename().
    ('attachment; filename="UU Nomor 8 Tahun 1995 (official).pdf"',
     "UU Nomor 8 Tahun 1995 _official_.pdf"),
    ('attachment; filename=POJK9.pdf', "POJK9.pdf"),
    ("attachment; filename*=UTF-8''POJK%209-2018.pdf", "POJK 9-2018.pdf"),
    ('attachment; filename="report"', "report.pdf"),        # extension added
    ('attachment; filename="../../etc/passwd"', "passwd.pdf"),  # path stripped
    (None, None),
    ("attachment", None),
])
def test_filename_from_disposition(header, expected):
    assert filename_from_disposition(header) == expected


def test_download_uses_server_filename(tmp_path, monkeypatch):
    """A GUID download endpoint must not save every file as the same name."""
    scraper = WebScraper(ScraperSettings(delay_seconds=0.0, respect_robots=False))

    class Resp:
        status_code = 200
        headers = {"Content-Type": "application/octet-stream",
                   "Content-Disposition": 'attachment; filename="POJK 9-2018.pdf"'}

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            yield b"%PDF-1.4 body"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(scraper.session, "request", lambda *a, **k: Resp())
    result = scraper.download_pdf(
        PdfLink("https://jdih.ojk.go.id/Web/ViewPeraturan/DownloadDokumen/abc-123"),
        tmp_path)
    assert result.ok
    assert result.path.name.endswith("POJK 9-2018.pdf")
