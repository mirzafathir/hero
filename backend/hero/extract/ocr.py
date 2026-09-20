"""OCR layer, with the image-quality pipeline that makes it usable on scans.

Mitigation for the URD risk "Format dokumen sumber bervariasi (PDF hasil scan,
tidak terstruktur)": any page without a usable text layer is rendered to an
image and passed through Tesseract with the Indonesian language model.

Three techniques sit in front of the raw Tesseract call, in order:
  1. Orientation detection (OSD) — fixes pages scanned sideways/upside-down.
  2. Deskew + binarize — fixes the few-degree tilt and poor contrast typical
     of a flatbed/feeder scan, which otherwise fools Tesseract's line finder.
  3. Adaptive re-OCR — if the first pass comes back with low mean confidence,
     retry once with a different page-segmentation mode and keep the better
     of the two results, since no single PSM suits every layout.
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image

log = logging.getLogger(__name__)

_MISSING_MSG = (
    "Tesseract is not installed. Install it with `brew install tesseract "
    "tesseract-lang` (macOS) or `apt-get install tesseract-ocr "
    "tesseract-ocr-ind` (Debian/Ubuntu)."
)

# A second attempt only helps when the first one was genuinely weak.
LOW_CONFIDENCE_THRESHOLD = 65.0
# PSM 3 = fully automatic layout; PSM 6 = one uniform block of text. Trying
# the second when the first scores low covers most single-column regulation
# scans that PSM 3 occasionally over-segments into spurious blocks.
_PRIMARY_CONFIG = "--oem 3 --psm 3"
_RETRY_CONFIG = "--oem 3 --psm 6"


@lru_cache(maxsize=1)
def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


@lru_cache(maxsize=1)
def available_languages() -> tuple[str, ...]:
    if not tesseract_available():
        return ()
    try:
        out = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    return tuple(line.strip() for line in out.splitlines()[1:] if line.strip())


def resolve_languages(requested: str) -> str:
    """Drop language codes that are not installed, keeping the request order.

    Falls back to ``eng`` so a missing ``ind`` traineddata degrades the OCR
    quality instead of failing the whole ingest.
    """
    have = set(available_languages())
    if not have:
        return requested
    kept = [code for code in requested.split("+") if code in have]
    if not kept:
        kept = ["eng"] if "eng" in have else [sorted(have)[0]]
        log.warning("None of the requested OCR languages (%s) are installed; "
                    "falling back to %s", requested, "+".join(kept))
    return "+".join(kept)


# --------------------------------------------------------------------------
# Step 1 — orientation (0/90/180/270)
# --------------------------------------------------------------------------
def detect_and_fix_orientation(image: "Image") -> tuple["Image", int]:
    """Rotate a page image upright using Tesseract's orientation detector.

    Returns (possibly-rotated image, degrees rotated). OSD needs a page with
    enough text to score confidently, so failure here is normal for near-
    blank pages and is treated as "leave it alone", never as an error.
    """
    if not tesseract_available():
        return image, 0
    import pytesseract

    try:
        osd = pytesseract.image_to_osd(image, config="--psm 0",
                                       output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractError:
        return image, 0
    except Exception:  # noqa: BLE001 - OSD is a best-effort helper
        return image, 0

    rotation = int(osd.get("rotate", 0)) % 360
    confidence = float(osd.get("orientation_conf", 0) or 0)
    # Low-confidence OSD calls are noise as often as signal; require some
    # margin before trusting a rotation that a human would have to review.
    if rotation == 0 or confidence < 1.0:
        return image, 0
    # PIL rotates counter-clockwise; Tesseract reports the clockwise
    # correction needed, hence the negation.
    return image.rotate(-rotation, expand=True), rotation


# --------------------------------------------------------------------------
# Step 2 — deskew + binarize
# --------------------------------------------------------------------------
def _otsu_threshold(gray) -> int:
    """Classic Otsu global threshold, computed from the image histogram.

    Picks the grey level that best separates ink from paper by maximising
    the between-class variance — the standard, dependency-light alternative
    to adaptive/local thresholding.
    """
    import numpy as np

    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    total = gray.size
    sum_all = np.dot(np.arange(256), hist)
    sum_bg, weight_bg, best_thresh, best_var = 0.0, 0, 0, -1.0
    for t in range(256):
        weight_bg += hist[t]
        if weight_bg == 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg == 0:
            break
        sum_bg += t * hist[t]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_all - sum_bg) / weight_fg
        variance = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if variance > best_var:
            best_var, best_thresh = variance, t
    return best_thresh


def _skew_angle(gray, search_range: float = 5.0, step: float = 0.5) -> float:
    """Estimate page tilt via the projection-profile method.

    Rotates a downsampled copy through a small range of candidate angles and
    keeps the one whose row-sum profile has the highest variance — text
    lines align into sharp peaks/troughs only when the page is level.
    """
    import numpy as np
    from PIL import Image as PILImage

    small = gray.copy()
    small.thumbnail((800, 800))
    arr = np.asarray(small, dtype=np.float64)

    best_angle, best_score = 0.0, -1.0
    angle = -search_range
    while angle <= search_range + 1e-9:
        if angle == 0:
            rotated = arr
        else:
            rotated = np.asarray(
                PILImage.fromarray(arr.astype("uint8")).rotate(
                    angle, resample=PILImage.BILINEAR, fillcolor=255),
                dtype=np.float64,
            )
        profile = rotated.sum(axis=1)
        score = float(np.var(profile))
        if score > best_score:
            best_score, best_angle = score, angle
        angle += step
    return best_angle


def preprocess_for_ocr(image: "Image", deskew: bool = True) -> "Image":
    """Grayscale, upscale, deskew and binarize a page image for Tesseract.

    Each step targets a specific scan defect: low DPI (upscale), page tilt
    (deskew), and washed-out or shadowed contrast (Otsu binarize). Any step
    that fails (missing numpy, degenerate image) is skipped rather than
    aborting the OCR pass — a partially-cleaned image still OCRs better than
    raising an exception would help anyone.
    """
    from PIL import Image as PILImage, ImageOps

    gray = ImageOps.grayscale(image)

    # Tesseract's accuracy drops sharply under ~200 DPI equivalent; a cheap
    # upscale of small renders recovers most of that.
    if min(gray.size) < 1200:
        scale = 1200 / max(min(gray.size), 1)
        scale = min(scale, 2.5)
        gray = gray.resize(
            (int(gray.width * scale), int(gray.height * scale)),
            PILImage.LANCZOS,
        )

    if deskew:
        try:
            angle = _skew_angle(gray)
            if abs(angle) >= 0.3:
                gray = gray.rotate(
                    angle, resample=PILImage.BICUBIC,
                    expand=True, fillcolor=255,
                )
        except Exception:  # noqa: BLE001 - numpy missing or degenerate page
            log.debug("deskew skipped", exc_info=True)

    gray = ImageOps.autocontrast(gray, cutoff=1)
    try:
        threshold = _otsu_threshold(__import__("numpy").asarray(gray))
        gray = gray.point(lambda p, t=threshold: 255 if p > t else 0)
    except Exception:  # noqa: BLE001 - numpy missing
        log.debug("binarization skipped", exc_info=True)

    return gray


# --------------------------------------------------------------------------
# Step 3 — OCR + adaptive retry
# --------------------------------------------------------------------------
def _run_tesseract(image: "Image", lang: str, config: str) -> tuple[str, float | None]:
    import pytesseract
    from pytesseract import Output

    try:
        data = pytesseract.image_to_data(
            image, lang=lang, config=config, output_type=Output.DICT
        )
    except pytesseract.TesseractError as exc:  # pragma: no cover - env specific
        log.warning("OCR failed: %s", exc)
        return "", None

    words, confs = [], []
    for word, conf in zip(data["text"], data["conf"]):
        word = (word or "").strip()
        if not word:
            continue
        words.append(word)
        try:
            c = float(conf)
        except (TypeError, ValueError):
            continue
        if c >= 0:
            confs.append(c)

    text = _reflow(data)
    mean_conf = round(sum(confs) / len(confs), 2) if confs else None
    return text, mean_conf


def ocr_image(
    image: "Image",
    languages: str = "ind+eng",
    preprocess: bool = True,
    deskew: bool = True,
    adaptive_retry: bool = True,
) -> dict:
    """Run the full pipeline on one page image.

    Returns a dict: text, confidence, rotation_applied, preprocessed,
    attempts — everything ``extract_pdf`` needs to populate a ``PageText``.
    """
    if not tesseract_available():
        raise RuntimeError(_MISSING_MSG)

    lang = resolve_languages(languages)
    rotation = 0
    working = image
    if preprocess:
        working, rotation = detect_and_fix_orientation(working)
        working = preprocess_for_ocr(working, deskew=deskew)

    text, conf = _run_tesseract(working, lang, _PRIMARY_CONFIG)
    attempts = 1

    if adaptive_retry and (conf is None or conf < LOW_CONFIDENCE_THRESHOLD):
        retry_text, retry_conf = _run_tesseract(working, lang, _RETRY_CONFIG)
        attempts = 2
        # A retry only wins if it is both non-empty and actually more
        # confident — an empty low-confidence result must not replace text.
        if retry_text and (conf is None or (retry_conf or 0) > conf):
            text, conf = retry_text, retry_conf

    return {
        "text": text, "confidence": conf, "rotation_applied": rotation,
        "preprocessed": preprocess, "attempts": attempts,
    }


def _reflow(data: dict) -> str:
    """Rebuild line breaks from Tesseract's block/paragraph/line indices."""
    lines: dict[tuple[int, int, int, int], list[str]] = {}
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        if not word:
            continue
        key = (
            data["page_num"][i], data["block_num"][i],
            data["par_num"][i], data["line_num"][i],
        )
        lines.setdefault(key, []).append(word)
    return "\n".join(" ".join(w) for _, w in sorted(lines.items()))


def ocr_pixmap_bytes(
    png_bytes: bytes,
    languages: str = "ind+eng",
    preprocess: bool = True,
    deskew: bool = True,
    adaptive_retry: bool = True,
) -> dict:
    from PIL import Image

    with Image.open(io.BytesIO(png_bytes)) as img:
        img.load()
        return ocr_image(img, languages, preprocess, deskew, adaptive_retry)
