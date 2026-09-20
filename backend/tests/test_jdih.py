"""JDIH OJK adapter: JSON listing parsing, detail->download resolution,
the dynamic sektor/jenis axes, and status normalisation.

Payload shapes below are copied from the live portal (2026-09-15), so a
schema change upstream shows up here as a failing test rather than as a
silent zero-document scrape.
"""
from __future__ import annotations

import pytest

from hero.config import SiteSource
from hero.ingest.jdih import (
    JENIS_LABELS, SEKTOR_LABELS, JdihEntry, discover_matrix, find_document_link,
    is_jdih_url, parse_index_url, parse_listing_rows, resolve_pairs,
)

# One real row, trimmed. Column layout: [anchor, number, sector, _, _, type, _, status]
REAL_ROW = [
    "<a href='http://jdih.ojk.go.id/Web/ViewPeraturan/Detail/"
    "e036e7ad-82e6-7ea5-d849-e5ebdb745985/02/06'>Peraturan Otoritas Jasa "
    "Keuangan Republik Indonesia Nomor 9/POJK.04/2015 tentang Pedoman "
    "Transaksi Repurchase Agreement Bagi Lembaga Jasa Keuangan</a>",
    "9", "Pasar Modal, Keuangan Derivatif, dan Bursa Karbon",
    None, None, "Peraturan OJK", "", "Berlaku",
]


def test_is_jdih_url():
    assert is_jdih_url("https://jdih.ojk.go.id/Web/ViewPeraturan/Index?sektor=02")
    assert not is_jdih_url("https://ojk.go.id/id/regulasi/default.aspx")


def test_parse_index_url_extracts_dynamic_axes():
    sektor, jenis = parse_index_url(
        "https://jdih.ojk.go.id/Web/ViewPeraturan/Index?sektor=02&jenisPeraturan=01")
    assert (sektor, jenis) == ("02", "01")


def test_parse_index_url_tolerates_missing_params():
    assert parse_index_url("https://jdih.ojk.go.id/Web/ViewPeraturan/Index") == (None, None)


def test_parse_listing_rows_extracts_entry():
    entries = parse_listing_rows({"aaData": [REAL_ROW]}, "02", "06")
    assert len(entries) == 1
    e = entries[0]
    assert e.detail_url.startswith("https://")     # http:// is upgraded
    assert "9/POJK.04/2015" in e.title
    assert "<a" not in e.title                     # tags stripped
    assert e.number == "9"
    assert e.sektor == "02" and e.jenis == "06"
    assert e.jenis_label == "Peraturan OJK"
    assert e.status == "Berlaku"
    assert e.category == "pasar-modal"


def test_parse_listing_rows_infers_axes_from_detail_url():
    """Rows fetched without knowing the axes still recover them."""
    entries = parse_listing_rows({"aaData": [REAL_ROW]})
    assert entries[0].sektor == "02"
    assert entries[0].jenis == "06"


def test_parse_listing_rows_handles_empty_and_malformed():
    assert parse_listing_rows({"aaData": []}) == []
    assert parse_listing_rows({}) == []
    assert parse_listing_rows({"aaData": [["no anchor here", "1"]]}) == []


def test_parse_listing_rows_survives_short_rows():
    """A column-count change upstream must not crash the ingest."""
    short = ["<a href='https://jdih.ojk.go.id/Web/ViewPeraturan/Detail/x/02/06'>T</a>"]
    entries = parse_listing_rows({"aaData": [short]}, "02", "06")
    assert len(entries) == 1
    assert entries[0].title == "T"
    assert entries[0].number is None


# -- status normalisation: the compound labels are the tricky part ---------
@pytest.mark.parametrize("raw,expected", [
    ("Berlaku", "berlaku"),
    ("Tidak Berlaku", "dicabut"),
    # Still in force, only partly revoked — must NOT read as fully revoked,
    # or harmonisation would skip a regulation that still binds.
    ("Berlaku (Dicabut Sebagian)", "berlaku"),
    ("Berlaku (Perubahan) (Diubah)", "diubah"),
    ("Berlaku (Perubahan) (Mengubah)", "berlaku"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_status_normalisation(raw, expected):
    assert JdihEntry(detail_url="x", title="t", status=raw).normalised_status() == expected


def test_find_document_link():
    html = """
    <html><body>
      <a href="/Web/ViewPeraturan/PreviewDokumen/abc">Preview</a>
      <a href="/Web/ViewPeraturan/DownloadDokumen/f74ddda9-39e9">Unduh</a>
    </body></html>
    """
    url = find_document_link(html)
    assert url == "https://jdih.ojk.go.id/Web/ViewPeraturan/DownloadDokumen/f74ddda9-39e9"


def test_find_document_link_returns_none_when_absent():
    assert find_document_link("<html><body>no document</body></html>") is None


def test_discover_matrix_reads_sidebar():
    html = """
    <html><body>
      <a href="http://jdih.ojk.go.id/Web/ViewPeraturan/Index?sektor=01&jenisPeraturan=06">Peraturan OJK</a>
      <a href="http://jdih.ojk.go.id/Web/ViewPeraturan/Index?sektor=02&jenisPeraturan=06">Peraturan OJK</a>
      <a href="http://jdih.ojk.go.id/Web/ViewPeraturan/Index?sektor=01&jenisPeraturan=06">dup</a>
      <a href="/Web/FAQ/Index">FAQ</a>
    </body></html>
    """
    pairs = discover_matrix(html)
    assert pairs == [("01", "06"), ("02", "06")]   # de-duplicated, order kept


def test_labels_cover_the_documented_codes():
    assert SEKTOR_LABELS["01"] == "Perbankan"
    assert JENIS_LABELS["06"] == "Peraturan OJK"


# -- axis resolution -------------------------------------------------------
class DummyScraper:
    """Stands in for WebScraper; records whether the index page was needed."""

    def __init__(self, html=None):
        self._html = html
        self.fetched = []

    def get_html(self, url, cache=None):
        self.fetched.append(url)
        return self._html, False


def test_resolve_pairs_prefers_explicit_config():
    site = SiteSource(name="t", url="https://jdih.ojk.go.id/Web/ViewPeraturan/Index",
                      sektor=["01", "02"], jenis_peraturan=["06"])
    scraper = DummyScraper()
    pairs, errors = resolve_pairs(site, scraper)
    assert pairs == [("01", "06"), ("02", "06")]
    assert errors == []
    assert scraper.fetched == []   # no network needed when config is explicit


def test_resolve_pairs_falls_back_to_url_query():
    site = SiteSource(
        name="t",
        url="https://jdih.ojk.go.id/Web/ViewPeraturan/Index?sektor=03&jenisPeraturan=09")
    pairs, errors = resolve_pairs(site, DummyScraper())
    assert pairs == [("03", "09")]
    assert errors == []


def test_resolve_pairs_discovers_matrix_when_unspecified():
    html = """
      <a href="/Web/ViewPeraturan/Index?sektor=01&jenisPeraturan=06">a</a>
      <a href="/Web/ViewPeraturan/Index?sektor=02&jenisPeraturan=09">b</a>
    """
    site = SiteSource(name="t", url="https://jdih.ojk.go.id/Web/ViewPeraturan/Index")
    pairs, errors = resolve_pairs(site, DummyScraper(html))
    assert set(pairs) == {("01", "06"), ("02", "09")}
    assert errors == []


def test_resolve_pairs_filters_discovered_matrix_by_sektor():
    html = """
      <a href="/Web/ViewPeraturan/Index?sektor=01&jenisPeraturan=06">a</a>
      <a href="/Web/ViewPeraturan/Index?sektor=02&jenisPeraturan=06">b</a>
    """
    site = SiteSource(name="t", url="https://jdih.ojk.go.id/Web/ViewPeraturan/Index",
                      sektor=["02"])
    pairs, _ = resolve_pairs(site, DummyScraper(html))
    assert pairs == [("02", "06")]


def test_resolve_pairs_reports_unreachable_index():
    site = SiteSource(name="t", url="https://jdih.ojk.go.id/Web/ViewPeraturan/Index")
    pairs, errors = resolve_pairs(site, DummyScraper(None))
    assert pairs == []
    assert any("could not read" in e for e in errors)


def test_site_source_auto_detects_adapters():
    jdih = SiteSource(name="t", url="https://jdih.ojk.go.id/Web/ViewPeraturan/Index")
    ojk = SiteSource(name="t", url="https://ojk.go.id/id/regulasi/default.aspx")
    draft = SiteSource(name="t", url="https://www.ojk.go.id/id/regulasi/"
                       "otoritas-jasa-keuangan/rancangan-regulasi/Default.aspx")
    other = SiteSource(name="t", url="https://example.org/peraturan")
    assert jdih.resolved_adapter() == "jdih_ojk"
    assert ojk.resolved_adapter() == "ojk_sharepoint"
    assert draft.resolved_adapter() == "ojk_sharepoint"
    assert other.resolved_adapter() == "generic"


def test_explicit_adapter_overrides_host_detection():
    site = SiteSource(name="t", url="https://jdih.ojk.go.id/anything",
                      adapter="generic")
    assert site.resolved_adapter() == "generic"
