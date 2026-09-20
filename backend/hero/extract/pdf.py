"""PDF text extraction with automatic OCR fallback.

Strategy per document:
  1. Read the embedded text layer with PyMuPDF (fast, exact).
  2. Any page whose text layer is thinner than ``min_chars_per_page`` is
     treated as image-only, rendered at ``dpi`` and sent through Tesseract.
This keeps born-digital regulations cheap while still handling scanned ones.
"""
from __future__ import annotations

import logging
import re
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf

from hero.config import OcrSettings, PdfSettings
from hero.extract.ocr import ocr_pixmap_bytes, tesseract_available
from hero.models import ExtractionResult, PageText

log = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"


def repair_pdf(path: Path) -> Path | None:
    """Rebuild a malformed PDF's cross-reference table with pikepdf/qpdf.

    Government-produced PDFs are sometimes technically broken (truncated
    xref, incorrect object offsets) in ways that PyMuPDF refuses to open but
    that a full re-serialisation fixes. Returns a path to a repaired temp
    copy, or ``None`` if pikepdf is unavailable or repair also fails.
    """
    try:
        import pikepdf
    except ImportError:
        log.debug("pikepdf not installed; skipping repair attempt")
        return None
    try:
        tmp = Path(tempfile.mkstemp(suffix=".pdf", prefix="hero_repair_")[1])
        with pikepdf.open(path) as pdf:
            pdf.save(tmp)
        return tmp
    except Exception as exc:  # noqa: BLE001 - repair is best-effort
        log.warning("pikepdf could not repair %s: %s", path.name, exc)
        return None


def _authenticate(doc: "pymupdf.Document", passwords: list[str]) -> str | None:
    """Try the blank password, then each configured candidate. Returns the
    password that worked, or ``None`` if the document stayed locked."""
    for pw in ("", *passwords):
        if doc.authenticate(pw):
            return pw
    return None


def is_pdf(path: Path) -> bool:
    """Validate by magic bytes, not by extension (URD 3.2: validasi format)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(1024)
    except OSError:
        return False
    return PDF_MAGIC in head[:1024]


def probe_pdf(path: Path, pdf_settings: PdfSettings | None = None) -> dict:
    """Cheap inspection: page count, encryption, whether a text layer exists."""
    pdf_cfg = pdf_settings or PdfSettings()
    info = {
        "valid": False, "page_count": 0, "encrypted": False,
        "has_text_layer": False, "error": None, "pdf_metadata": {},
        "repaired": False, "password_required": False,
    }
    if not is_pdf(path):
        info["error"] = "not a PDF (magic bytes missing)"
        return info

    open_path = path
    try:
        doc = pymupdf.open(open_path)
    except Exception:  # noqa: BLE001
        repaired = repair_pdf(path) if pdf_cfg.attempt_repair else None
        if repaired is None:
            info["error"] = "cannot open PDF (corrupt or unsupported structure)"
            return info
        try:
            doc = pymupdf.open(repaired)
            info["repaired"] = True
            open_path = repaired
        except Exception as exc:  # noqa: BLE001
            info["error"] = f"{type(exc).__name__}: {exc}"
            return info

    try:
        with doc:
            info["encrypted"] = bool(doc.is_encrypted)
            if doc.is_encrypted:
                if _authenticate(doc, pdf_cfg.candidate_passwords) is None:
                    info["error"] = "encrypted / password protected"
                    info["password_required"] = True
                    info["valid"] = True
                    return info
            info["page_count"] = doc.page_count
            info["pdf_metadata"] = {
                k: v for k, v in (doc.metadata or {}).items() if v
            }
            # Sample the first few pages: a scan has essentially no text layer,
            # a born-digital regulation has hundreds of characters per page.
            probe_pages = min(doc.page_count, 5)
            chars = sum(
                len(doc[i].get_text("text").strip()) for i in range(probe_pages)
            )
            info["has_text_layer"] = chars > 20 * probe_pages
            info["valid"] = True
    except Exception as exc:  # noqa: BLE001 - report, never crash the pipeline
        info["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if open_path != path:
            open_path.unlink(missing_ok=True)
    return info


# --------------------------------------------------------------------------
# Text cleanup
# --------------------------------------------------------------------------
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "‘": "'", "’": "'", "“": '"',
    "”": '"', "–": "-", "—": "-", " ": " ",
}
_HYPHEN_BREAK = re.compile(r"(\w)-\s*\n\s*(\w)")
_MULTI_BLANK = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+\n")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def clean_text(text: str) -> str:
    """Normalise unicode, join hyphenated line breaks, squeeze whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    for src, dst in _LIGATURES.items():
        text = text.replace(src, dst)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = _TRAILING_WS.sub("\n", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()


# A line that opens a new structural unit must never be merged into the one
# above it, or the Pasal/ayat parser loses its anchors.
_STRUCTURAL_START = re.compile(
    r"^\s*(?:"
    r"\(\d{1,2}\)"                    # ayat marker: (1)
    r"|\d{1,2}\.(?:\s|$)"             # numbered definition: 1.
    r"|[a-z]\.(?:\s|$)"                # lettered point: a.
    r"|BAB\s|BAGIAN\s|PARAGRAF\s|PASAL\s|Pasal\s|CHAPTER\s|Article\s|ARTICLE\s"
    r"|LAMPIRAN|PENJELASAN|MEMUTUSKAN|MENETAPKAN"
    r"|Menimbang|Mengingat|Ditetapkan|Diundangkan"
    r"|Yth\.|-\s*\d+\s*-"
    r")")
# A paragraph continues only if the previous line stopped mid-sentence.
_SENTENCE_END = re.compile(r"[.;:!?]['\")\]]?$")
# A standalone structural heading must not absorb the line that follows it.
_HEADING_LINE = re.compile(
    r"^(?:BAB\s+[IVXLCDM]+|CHAPTER\s+[IVXLCDM]+|BAGIAN\s+\w+|PARAGRAF\s+\w+"
    r"|PASAL\s+\d+[A-Za-z]?|ARTICLE\s+\d+[A-Za-z]?)\s*$",
    re.IGNORECASE)
# Bare page numbers: "- 4 -", "4", "- 12".
_PAGE_NUMBER_LINE = re.compile(r"^-{0,2}\s*\d{1,4}\s*-{0,2}$")
# Some layouts place the "-2-" footer in the text stream immediately before
# the first word of the next page, giving "-2Indonesia Nomor 106/OJK);".
_LEADING_PAGE_NUMBER = re.compile(r"^-\s?\d{1,4}\s?-?\s*(?=[A-Za-z(])")
# A list marker alone on its line ("a.", "1.", "(2)") always owns the text
# on the next line, whatever punctuation it ends with.
_BARE_MARKER = re.compile(r"^(?:\(\d{1,2}\)|\d{1,2}\.|[a-z]\.|[IVXLC]{1,6}\.)$")


def reflow_paragraphs(text: str) -> str:
    """Rejoin words that justified-text extraction split across lines.

    OJK "SALINAN" PDFs position each word independently, so PyMuPDF emits
    "Pelapor\nmenyusun\ndan\nmenyampaikan". Left alone, that wrecks sentence
    splitting, summaries and any diff between two documents.
    """
    out: list[str] = []
    for raw in (text or "").split("\n"):
        line = raw.strip()
        if not line:
            out.append("")
            continue
        if _PAGE_NUMBER_LINE.match(line):
            out.append(line)   # kept as its own line so the footer strip sees it
            continue
        if out and out[-1] and _BARE_MARKER.match(out[-1]):
            out[-1] = f"{out[-1]} {line}"
            continue
        if (out and out[-1] and not _STRUCTURAL_START.match(line)
                and not _SENTENCE_END.search(out[-1])
                and not _HEADING_LINE.match(out[-1])
                and not _PAGE_NUMBER_LINE.match(out[-1])
                and not _is_all_caps_heading(out[-1])
                and not _is_all_caps_heading(line)):
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
    return "\n".join(out)


def _is_all_caps_heading(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    if not letters or len(line) > 90:
        return False
    return sum(c.isupper() for c in letters) / len(letters) > 0.85


def strip_repeated_lines(pages: list[PageText], threshold: float = 0.6) -> None:
    """Remove running headers/footers and page numbers, in place.

    Page numbers and "Otoritas Jasa Keuangan" banners otherwise pollute every
    summary and every harmonisation diff. Structural headings are never
    candidates: digits are normalised to '#' when lines are compared, so
    "Pasal 2", "Pasal 3" and "Pasal 5" would otherwise look like one repeated
    line and a short regulation would lose its headings entirely.
    """
    boilerplate: set[str] = set()
    # Repetition only means "running header" when there are enough pages.
    if len(pages) >= 6:
        counts: dict[str, int] = {}
        for page in pages:
            lines = page.text.split("\n")
            for line in lines[:3] + lines[-3:]:
                stripped = line.strip()
                if _HEADING_LINE.match(stripped) or _STRUCTURAL_START.match(stripped):
                    continue
                key = re.sub(r"\d+", "#", stripped)
                if 3 < len(key) < 90:
                    counts[key] = counts.get(key, 0) + 1
        cutoff = max(4, int(len(pages) * threshold))
        boilerplate = {k for k, v in counts.items() if v >= cutoff}

    for page in pages:
        kept = []
        for line in page.text.split("\n"):
            stripped = line.strip()
            # Page numbers migrate into the body when a footer sits mid-column.
            if _PAGE_NUMBER_LINE.match(stripped):
                continue
            if boilerplate and re.sub(r"\d+", "#", stripped) in boilerplate:
                continue
            kept.append(line)
        page.text = _MULTI_BLANK.sub("\n\n", "\n".join(kept)).strip()
        page.text = _LEADING_PAGE_NUMBER.sub("", page.text)
        page.char_count = len(page.text)


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------
def extract_pdf(
    path: Path,
    ocr_settings: OcrSettings | None = None,
    pdf_settings: PdfSettings | None = None,
    force_ocr: bool = False,
    max_workers: int = 4,
) -> ExtractionResult:
    """Extract text from ``path``, OCR-ing any page that lacks a text layer."""
    ocr_cfg = ocr_settings or OcrSettings()
    pdf_cfg = pdf_settings or PdfSettings()
    started = time.perf_counter()
    result = ExtractionResult(path=path)

    if not is_pdf(path):
        result.error = "not a PDF (magic bytes missing)"
        return result

    open_path = path
    try:
        doc = pymupdf.open(open_path)
    except Exception:  # noqa: BLE001
        repaired = repair_pdf(path) if pdf_cfg.attempt_repair else None
        if repaired is None:
            result.error = "cannot open PDF (corrupt or unsupported structure)"
            return result
        try:
            doc = pymupdf.open(repaired)
            open_path, result.repaired = repaired, True
        except Exception as exc:  # noqa: BLE001
            result.error = f"cannot open PDF even after repair: {exc}"
            repaired.unlink(missing_ok=True)
            return result

    try:
        with doc:
            if doc.is_encrypted:
                if _authenticate(doc, pdf_cfg.candidate_passwords) is None:
                    result.encrypted = True
                    result.error = "encrypted / password protected"
                    return result

            result.page_count = doc.page_count
            pages: list[PageText] = []
            ocr_todo: list[int] = []

            for i in range(doc.page_count):
                try:
                    raw = "" if force_ocr else doc[i].get_text("text")
                except Exception as exc:  # noqa: BLE001 - one bad page ≠ a bad doc
                    log.warning("%s: page %d text layer unreadable (%s); "
                               "falling back to OCR", path.name, i + 1, exc)
                    raw = ""
                text = reflow_paragraphs(clean_text(raw))
                page = PageText(number=i + 1, text=text, source="text-layer")
                if force_ocr or page.char_count < ocr_cfg.min_chars_per_page:
                    ocr_todo.append(i)
                pages.append(page)

            if ocr_todo and ocr_cfg.enabled:
                if not tesseract_available():
                    log.warning(
                        "%d page(s) in %s need OCR but Tesseract is unavailable",
                        len(ocr_todo), path.name,
                    )
                else:
                    budget = ocr_todo[: ocr_cfg.max_pages]
                    if len(ocr_todo) > len(budget):
                        log.warning(
                            "%s: OCR budget %d pages, %d needed — truncating",
                            path.name, ocr_cfg.max_pages, len(ocr_todo),
                        )
                    rendered = [
                        (i, doc[i].get_pixmap(dpi=ocr_cfg.dpi).tobytes("png"))
                        for i in budget
                    ]
                    with ThreadPoolExecutor(max_workers=max_workers) as pool:
                        outputs = list(pool.map(
                            lambda item: ocr_pixmap_bytes(
                                item[1], ocr_cfg.languages,
                                preprocess=ocr_cfg.preprocess,
                                deskew=ocr_cfg.deskew,
                                adaptive_retry=ocr_cfg.adaptive_retry,
                            ),
                            rendered,
                        ))
                    for (i, _), out in zip(rendered, outputs):
                        text = reflow_paragraphs(clean_text(out["text"]))
                        # Keep whichever layer actually produced content.
                        if len(text) > pages[i].char_count:
                            pages[i] = PageText(
                                number=i + 1, text=text, source="ocr",
                                ocr_confidence=out["confidence"],
                                rotation_applied=out["rotation_applied"],
                                preprocessed=out["preprocessed"],
                                ocr_attempts=out["attempts"],
                            )
                            result.ocr_pages += 1
    finally:
        if open_path != path:
            open_path.unlink(missing_ok=True)

    strip_repeated_lines(pages)
    result.pages = pages
    result.is_scanned = bool(
        result.page_count and result.ocr_pages / result.page_count > 0.5
    )
    result.duration_seconds = round(time.perf_counter() - started, 2)
    if result.char_count == 0 and not result.error:
        result.error = "no extractable text (empty or unreadable document)"
    return result


def extract_tables(path: Path, max_pages: int = 20) -> list[dict]:
    """Pull tabular data out with pdfplumber (attachment/lampiran tables)."""
    import pdfplumber

    tables: list[dict] = []
    try:
        with pdfplumber.open(path) as pdf:
            for pno, page in enumerate(pdf.pages[:max_pages], start=1):
                for tno, tbl in enumerate(page.extract_tables() or [], start=1):
                    rows = [
                        [(c or "").strip() for c in row]
                        for row in tbl if any(c for c in row)
                    ]
                    if len(rows) >= 2:
                        tables.append(
                            {"page": pno, "index": tno,
                             "rows": rows, "n_rows": len(rows),
                             "n_cols": max(len(r) for r in rows)}
                        )
    except Exception as exc:  # noqa: BLE001
        log.warning("table extraction failed for %s: %s", path.name, exc)
    return tables
