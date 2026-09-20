"""Document-quality scoring for the ingest pipeline.

Turns the low-level signals produced during extraction (OCR ratio, mean
confidence, blank/rotated pages) into one human-readable grade, so an
operator triaging the "escalasi manual untuk dokumen bermasalah" queue (URD
7.1) can sort by risk instead of opening every file.
"""
from __future__ import annotations

from typing import Any

from hero.models import ExtractionResult

# Below this mean OCR confidence, a page's text is flagged as needing review
# rather than trusted outright.
LOW_CONFIDENCE_THRESHOLD = 65.0


def assess_quality(result: ExtractionResult) -> dict[str, Any]:
    """Summarise how trustworthy an extraction is, with a plain-language grade."""
    total = result.page_count
    low_conf_pages = [
        p.number for p in result.pages
        if p.ocr_confidence is not None and p.ocr_confidence < LOW_CONFIDENCE_THRESHOLD
    ]
    retried_pages = [p.number for p in result.pages if p.ocr_attempts > 1]

    issues: list[str] = []
    if result.error:
        issues.append(result.error)
    if result.repaired:
        issues.append("berkas diperbaiki otomatis sebelum diproses (struktur PDF rusak)")
    if result.blank_pages:
        issues.append(f"{result.blank_pages} halaman kosong/tidak terbaca")
    if low_conf_pages:
        issues.append(
            f"{len(low_conf_pages)} halaman OCR dengan keyakinan rendah "
            f"(<{LOW_CONFIDENCE_THRESHOLD:.0f}%)")
    if result.rotated_pages:
        issues.append(f"{result.rotated_pages} halaman diputar otomatis (orientasi awal salah)")

    if result.error and result.char_count == 0:
        grade = "gagal"
    elif result.is_scanned and (result.mean_ocr_confidence or 0) < LOW_CONFIDENCE_THRESHOLD:
        grade = "perlu-review"
    elif issues:
        grade = "cukup"
    else:
        grade = "baik"

    return {
        "grade": grade,
        "page_count": total,
        "ocr_pages": result.ocr_pages,
        "ocr_ratio": round(result.ocr_ratio, 3),
        "mean_ocr_confidence": result.mean_ocr_confidence,
        "blank_pages": result.blank_pages,
        "rotated_pages": result.rotated_pages,
        "low_confidence_pages": low_conf_pages,
        "adaptive_retry_pages": retried_pages,
        "repaired": result.repaired,
        "issues": issues,
    }
