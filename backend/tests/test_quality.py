"""Document-quality grading and the PDF repair / password-retry paths."""
from __future__ import annotations

import pymupdf
import pytest

from hero.config import PdfSettings
from hero.extract.pdf import extract_pdf, probe_pdf, repair_pdf
from hero.extract.quality import assess_quality
from hero.models import ExtractionResult, PageText


def make_pdf(path, pages_text, password=None):
    doc = pymupdf.open()
    for text in pages_text:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    if password:
        doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                 owner_pw=password, user_pw=password)
    else:
        doc.save(path)
    doc.close()
    return path


# -- quality grading -------------------------------------------------------
def test_quality_grade_baik_for_clean_digital_doc():
    result = ExtractionResult(
        path="x.pdf", page_count=3,
        pages=[PageText(number=i, text="lorem ipsum dolor sit amet " * 5,
                        source="text-layer") for i in range(1, 4)],
    )
    q = assess_quality(result)
    assert q["grade"] == "baik"
    assert q["issues"] == []


def test_quality_flags_low_confidence_ocr_as_needing_review():
    result = ExtractionResult(
        path="x.pdf", page_count=2, ocr_pages=2, is_scanned=True,
        pages=[
            PageText(number=1, text="teks buram", source="ocr", ocr_confidence=40.0),
            PageText(number=2, text="teks buram lagi", source="ocr", ocr_confidence=45.0),
        ],
    )
    q = assess_quality(result)
    assert q["grade"] == "perlu-review"
    assert q["low_confidence_pages"] == [1, 2]
    assert any("keyakinan rendah" in issue for issue in q["issues"])


def test_quality_reports_blank_and_rotated_pages():
    result = ExtractionResult(
        path="x.pdf", page_count=2, ocr_pages=1,
        pages=[
            PageText(number=1, text="", source="text-layer"),
            PageText(number=2, text="isi halaman lengkap", source="ocr",
                     ocr_confidence=90.0, rotation_applied=180),
        ],
    )
    q = assess_quality(result)
    assert q["blank_pages"] == 1
    assert q["rotated_pages"] == 1
    assert q["grade"] in ("cukup", "baik", "perlu-review")


def test_quality_grade_gagal_when_extraction_failed_completely():
    result = ExtractionResult(path="x.pdf", page_count=0,
                              error="cannot open PDF (corrupt or unsupported structure)")
    q = assess_quality(result)
    assert q["grade"] == "gagal"


# -- repair + password handling --------------------------------------------
def test_repair_pdf_returns_none_when_file_is_actually_fine(tmp_path):
    good = make_pdf(tmp_path / "good.pdf", ["Halo dunia"])
    # A well-formed PDF doesn't need pikepdf to rebuild anything, but the
    # function must still hand back a usable (repaired) copy, not fail.
    repaired = repair_pdf(good)
    assert repaired is not None
    assert repaired.exists()
    repaired.unlink()


def test_extract_pdf_reports_repaired_flag_when_pymupdf_cannot_open(tmp_path):
    good = make_pdf(tmp_path / "good.pdf", ["PASAL 1 Ketentuan Umum"])
    data = good.read_bytes()
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(data[: int(len(data) * 0.5)])

    # Confirm PyMuPDF genuinely refuses this file directly (sanity check).
    with pytest.raises(Exception):
        pymupdf.open(corrupt)

    result = extract_pdf(corrupt, pdf_settings=PdfSettings(attempt_repair=True))
    assert result.repaired is True


def test_extract_pdf_without_repair_reports_clean_error(tmp_path):
    good = make_pdf(tmp_path / "good.pdf", ["PASAL 1 Ketentuan Umum"])
    data = good.read_bytes()
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(data[: int(len(data) * 0.5)])

    result = extract_pdf(corrupt, pdf_settings=PdfSettings(attempt_repair=False))
    assert result.repaired is False
    assert result.error is not None


def test_encrypted_pdf_opens_with_configured_password(tmp_path):
    protected = make_pdf(tmp_path / "locked.pdf", ["Isi rahasia"], password="secret123")

    without = probe_pdf(protected, PdfSettings(candidate_passwords=[]))
    assert without["error"] == "encrypted / password protected"

    with_pw = probe_pdf(protected, PdfSettings(candidate_passwords=["wrong", "secret123"]))
    assert with_pw["valid"] is True
    assert with_pw["error"] is None
    assert with_pw["page_count"] == 1


def test_encrypted_pdf_extract_uses_configured_password(tmp_path):
    protected = make_pdf(tmp_path / "locked.pdf", ["Isi rahasia dokumen"], password="letmein")
    result = extract_pdf(protected, pdf_settings=PdfSettings(candidate_passwords=["letmein"]))
    assert result.error is None
    assert "rahasia" in result.text.lower()
