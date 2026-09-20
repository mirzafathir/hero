"""Text cleanup, reflow and the OCR fallback."""
import pymupdf
import pytest

from hero.config import OcrSettings
from hero.extract.ocr import resolve_languages, tesseract_available
from hero.extract.pdf import (
    clean_text, extract_pdf, is_pdf, probe_pdf, reflow_paragraphs,
    strip_repeated_lines,
)
from hero.models import PageText


def make_pdf(path, pages_text):
    doc = pymupdf.open()
    for text in pages_text:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    doc.save(path)
    doc.close()
    return path


def test_is_pdf_uses_magic_bytes(tmp_path):
    fake = tmp_path / "fake.pdf"
    fake.write_text("this is not a PDF at all")
    assert not is_pdf(fake)

    real = make_pdf(tmp_path / "real.pdf", ["Halo"])
    assert is_pdf(real)


def test_probe_reports_invalid_file(tmp_path):
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"\x00\x01\x02")
    info = probe_pdf(fake)
    assert not info["valid"]
    assert "not a PDF" in info["error"]


def test_clean_text_joins_hyphenated_breaks():
    assert clean_text("perundang-\nundangan") == "perundangundangan"
    assert clean_text("ﬁnansial") == "finansial"


def test_reflow_rejoins_justified_words():
    raw = "Pelapor\nmenyusun\ndan\nmenyampaikan\nLaporan Insidental."
    assert reflow_paragraphs(raw) == (
        "Pelapor menyusun dan menyampaikan Laporan Insidental.")


def test_reflow_keeps_pasal_headings_standalone():
    raw = "Pasal 5\nDalam hal terdapat kesalahan isian data."
    assert reflow_paragraphs(raw).split("\n")[0] == "Pasal 5"


def test_reflow_attaches_bare_list_markers():
    raw = "meliputi:\na.\nbursa efek;\nb.\nlembaga kliring;"
    out = reflow_paragraphs(raw)
    assert "a. bursa efek;" in out
    assert "b. lembaga kliring;" in out


def test_strip_repeated_lines_keeps_pasal_headings():
    # Six short pages, each opening with a Pasal heading and a banner.
    pgs = [
        PageText(number=i, source="text-layer", text=(
            f"OTORITAS JASA KEUANGAN\nPasal {i}\nIsi ketentuan nomor {i}.\n- {i} -"
        ))
        for i in range(1, 7)
    ]
    strip_repeated_lines(pgs)
    joined = "\n".join(p.text for p in pgs)
    assert "OTORITAS JASA KEUANGAN" not in joined   # running header removed
    assert "- 1 -" not in joined                    # page number removed
    for i in range(1, 7):
        assert f"Pasal {i}" in joined               # headings survive


def test_extract_text_layer(tmp_path):
    pdf = make_pdf(tmp_path / "doc.pdf", ["Peraturan Otoritas Jasa Keuangan"])
    result = extract_pdf(pdf, OcrSettings(enabled=False))
    assert result.error is None
    assert result.page_count == 1
    assert result.ocr_pages == 0
    assert "Otoritas Jasa Keuangan" in result.text
    assert result.pages[0].source == "text-layer"


def test_extract_rejects_non_pdf(tmp_path):
    fake = tmp_path / "x.pdf"
    fake.write_text("nope")
    result = extract_pdf(fake, OcrSettings(enabled=False))
    assert result.error is not None
    assert result.pages == []


@pytest.mark.skipif(not tesseract_available(), reason="tesseract not installed")
def test_ocr_fallback_on_scanned_pdf(scanned_pdf):
    result = extract_pdf(scanned_pdf, OcrSettings(enabled=True, dpi=200))
    assert result.page_count == 4
    assert result.ocr_pages == 4          # no text layer at all
    assert result.is_scanned
    assert all(p.source == "ocr" for p in result.pages)
    assert "OTORITAS JASA KEUANGAN" in result.text.upper()
    # Tesseract reports a per-page mean confidence.
    assert all(p.ocr_confidence and p.ocr_confidence > 60 for p in result.pages)


@pytest.mark.skipif(not tesseract_available(), reason="tesseract not installed")
def test_scanned_pdf_still_yields_metadata_and_structure(scanned_pdf):
    from hero.extract.metadata import extract_metadata
    from hero.extract.structure import parse_structure

    result = extract_pdf(scanned_pdf, OcrSettings(enabled=True, dpi=200))
    md = extract_metadata(result.text)
    assert md.doc_type == "POJK"
    assert md.number == "9 Tahun 2026"
    struct = parse_structure(result.pages)
    assert [a.number for a in struct.articles][:3] == ["1", "2", "3"]


def test_resolve_languages_drops_missing():
    resolved = resolve_languages("ind+eng+klingon")
    assert "klingon" not in resolved
    assert resolved


def test_leading_page_number_is_stripped_from_page_start():
    # Some layouts emit the "-2-" footer glued to the next page's first word.
    pgs = [PageText(number=1, source="text-layer",
                    text="-2Indonesia Nomor 106/OJK);\nlanjutan teks.")]
    strip_repeated_lines(pgs)
    assert pgs[0].text.startswith("Indonesia Nomor")
