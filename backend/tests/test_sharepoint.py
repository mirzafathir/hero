"""ojk.go.id SharePoint adapter, against real pages saved on 2026-09-15."""
from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from hero.ingest import sharepoint as sp

FIX = Path(__file__).parent / "fixtures" / "html"
REG_URL = "https://ojk.go.id/id/regulasi/default.aspx"
DRAFT_URL = ("https://www.ojk.go.id/id/regulasi/otoritas-jasa-keuangan/"
             "rancangan-regulasi/Default.aspx")


def soup(name: str) -> BeautifulSoup:
    return BeautifulSoup((FIX / name).read_text(encoding="utf-8", errors="replace"), "lxml")


def html(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8", errors="replace")


def test_regulasi_listing_rows():
    rows = sp.parse_regulasi_rows(soup("ojk_regulasi_list.html"), REG_URL, 1)
    assert len(rows) == 10
    first = rows[0]
    assert first.number == "45/PADK.06/2025"
    assert first.jenis == "Peraturan ADK"
    assert first.sektor == "PVML"
    assert first.tahun == "2027"
    assert first.url.startswith("https://ojk.go.id/id/regulasi/Pages/")


def test_rancangan_listing_rows_strip_zero_width_spaces():
    rows = sp.parse_rancangan_rows(soup("ojk_rancangan_list.html"), DRAFT_URL, 1)
    assert len(rows) == 10
    assert "​" not in (rows[0].description or "")
    assert rows[0].description.startswith("Dalam rangka penyusunan")


def test_pager_points_to_page_two_and_form_carries_viewstate():
    s = soup("ojk_regulasi_list.html")
    current, target = sp.next_page_target(s)
    assert current == 1
    assert target and "DataPagerArticles" in target
    fields = sp.form_fields(s)
    assert "__VIEWSTATE" in fields and len(fields["__VIEWSTATE"]) > 1000


def test_pager_uses_ellipsis_to_open_next_block():
    markup = """<span id="x_DataPagerArticles">
      <a href="javascript:__doPostBack('p$ctl01$ctl00','')">1</a>
      <span class="currentPagingButton">10</span>
      <a href="javascript:__doPostBack('p$ctl01$ctl10','')">...</a>
    </span>"""
    current, target = sp.next_page_target(BeautifulSoup(markup, "lxml"))
    assert current == 10
    assert target == "p$ctl01$ctl10"


def test_pager_reports_last_page():
    markup = """<span id="x_DataPagerArticles">
      <a href="javascript:__doPostBack('p$ctl01$ctl00','')">156</a>
      <span class="currentPagingButton">157</span></span>"""
    assert sp.next_page_target(BeautifulSoup(markup, "lxml")) == (157, None)


def test_regulasi_detail_fields_and_attachments():
    d = sp.parse_detail(html("ojk_regulasi_detail.html"), "https://ojk.go.id/id/regulasi/Pages/x.aspx")
    assert d["fields"]["Nomor Regulasi"] == "45/PADK.06/2025"
    assert d["fields"]["SubSektor"] == "Lembaga Pembiayaan"
    assert d["fields"]["Tanggal Berlaku"] == "7/1/2027"
    kinds = [a["kind"] for a in d["attachments"]]
    assert kinds[:3] == ["utama", "abstrak", "faq"]
    primary = sp.primary_attachment(d["attachments"])
    assert primary["kind"] == "utama" and primary["ext"] == "pdf"


def test_rancangan_detail_primary_is_the_zip():
    d = sp.parse_detail(html("ojk_rancangan_detail.html"), DRAFT_URL)
    assert d["fields"]["Kategori"] == "PPDP"
    assert d["fields"]["Tanggal"] == "9/14/2026"
    assert sp.primary_attachment(d["attachments"])["ext"] == "zip"


@pytest.mark.parametrize("raw,iso", [
    ("7/1/2027", "2027-07-01"),        # SharePoint renders en-US M/D/YYYY
    ("9/14/2026", "2026-09-14"),
    ("13/1/2026", None),               # month 13 is invalid, not a D/M date
    ("", None), (None, None), ("2026-01-01", None),
])
def test_us_date_to_iso(raw, iso):
    assert sp.us_date_to_iso(raw) == iso


@pytest.mark.parametrize("name,kind", [
    ("PADK 45-PADK06-2025 Laporan Bulanan.pdf", "utama"),
    ("Abstrak PADK 45.pdf", "abstrak"),
    ("FAQ PADK 45.pdf", "faq"),
    ("Lampiran I POJK 9.pdf", "lampiran"),
    ("Matriks tanggapan.docx", "matriks"),
])
def test_attachment_kind(name, kind):
    assert sp.attachment_kind(name) == kind


def test_primary_attachment_prefers_pdf_over_companions():
    atts = [{"name": "Abstrak.pdf", "kind": "abstrak", "ext": "pdf", "url": "a"},
            {"name": "POJK.pdf", "kind": "utama", "ext": "pdf", "url": "b"}]
    assert sp.primary_attachment(atts)["url"] == "b"
    assert sp.primary_attachment([]) is None


def test_listing_kind_detection():
    assert sp.detect_listing_kind(DRAFT_URL) == "rancangan"
    assert sp.detect_listing_kind(REG_URL) == "regulasi"
