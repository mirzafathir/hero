"""Orientation correction, deskew/binarize preprocessing, and adaptive retry.

These exercise the real Tesseract binary (skipped if unavailable) against
synthetic page images, since the whole point of this pipeline is measurable
OCR-accuracy behaviour that a mock cannot demonstrate.
"""
from __future__ import annotations

import pytest

from hero.extract.ocr import (
    detect_and_fix_orientation, ocr_image, preprocess_for_ocr,
    tesseract_available,
)

pytestmark = pytest.mark.skipif(
    not tesseract_available(), reason="tesseract not installed")


def make_text_image(rotate: int = 0):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (900, 500), color=255)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/System/Library/Fonts/Supplemental/Arial.ttf", 26)
    except OSError:
        font = ImageFont.load_default()
    lines = [
        "PASAL 5",
        "Setiap pelaku usaha wajib menyampaikan laporan",
        "paling lambat tanggal 10 setiap bulan.",
    ]
    y = 60
    for line in lines:
        draw.text((60, y), line, fill=0, font=font)
        y += 50
    img = img.convert("RGB")
    return img.rotate(rotate, expand=True, fillcolor=(255, 255, 255)) if rotate else img


def test_orientation_detection_recovers_upside_down_page():
    img = make_text_image(rotate=180)
    fixed, rotation = detect_and_fix_orientation(img)
    assert rotation == 180
    # The corrected image must OCR back to upright, readable text.
    out = ocr_image(fixed, preprocess=False)
    assert "PASAL 5" in out["text"]


def test_orientation_detection_leaves_upright_page_alone():
    img = make_text_image(rotate=0)
    fixed, rotation = detect_and_fix_orientation(img)
    assert rotation == 0
    assert fixed.size == img.size


def test_preprocess_for_ocr_returns_a_grayscale_image_of_same_content():
    img = make_text_image()
    cleaned = preprocess_for_ocr(img)
    assert cleaned.mode in ("L", "1")
    out = ocr_image(cleaned, preprocess=False)
    assert "PASAL 5" in out["text"]


def test_ocr_image_full_pipeline_on_tilted_scan():
    tilted = make_text_image(rotate=-3)
    out = ocr_image(tilted, preprocess=True, deskew=True)
    assert "PASAL 5" in out["text"]
    assert out["confidence"] is not None and out["confidence"] > 50


def test_ocr_image_reports_metadata_fields():
    img = make_text_image()
    out = ocr_image(img, preprocess=True, deskew=True, adaptive_retry=True)
    assert set(out) == {"text", "confidence", "rotation_applied",
                        "preprocessed", "attempts"}
    assert out["preprocessed"] is True
    assert out["attempts"] in (1, 2)


def test_ocr_image_without_preprocessing_skips_pipeline():
    img = make_text_image()
    out = ocr_image(img, preprocess=False)
    assert out["preprocessed"] is False
    assert out["rotation_applied"] == 0
