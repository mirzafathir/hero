"""Core data structures shared by the ingest and extraction layers."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any


@dataclass
class RegulationMetadata:
    """Metadata extracted from a regulation PDF (URD 3.2 & 3.3)."""

    title: str | None = None
    # "Tentang ..." clause — the subject line of an Indonesian regulation.
    subject: str | None = None
    doc_type: str | None = None          # POJK, SEOJK, UU, PP, PERPRES, ...
    doc_type_label: str | None = None    # Human readable expansion
    number: str | None = None            # "11/POJK.03/2022"
    number_raw: str | None = None
    year: int | None = None
    issued_date: date | None = None
    issuing_body: str | None = None      # Otoritas Jasa Keuangan, Presiden RI...
    status: str = "unknown"              # berlaku | dicabut | diubah | unknown
    legal_basis: list[str] = field(default_factory=list)
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if isinstance(self.issued_date, date):
            d["issued_date"] = self.issued_date.isoformat()
        return d


@dataclass
class PageText:
    """Text of a single PDF page plus how it was obtained."""

    number: int
    text: str
    source: str          # "text-layer" | "ocr"
    char_count: int = 0
    ocr_confidence: float | None = None
    rotation_applied: int = 0     # degrees corrected via OSD before OCR
    preprocessed: bool = False    # ran through the image-cleanup pipeline
    ocr_attempts: int = 1         # >1 means an adaptive re-OCR pass won

    def __post_init__(self) -> None:
        self.char_count = len(self.text.strip())


@dataclass
class ExtractionResult:
    """Full text-extraction outcome for one PDF."""

    path: Path
    pages: list[PageText] = field(default_factory=list)
    page_count: int = 0
    ocr_pages: int = 0
    is_scanned: bool = False
    encrypted: bool = False
    error: str | None = None
    duration_seconds: float = 0.0
    repaired: bool = False   # opened only after a pikepdf repair pass

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    @property
    def char_count(self) -> int:
        return sum(p.char_count for p in self.pages)

    @property
    def ocr_ratio(self) -> float:
        return self.ocr_pages / self.page_count if self.page_count else 0.0

    @property
    def mean_ocr_confidence(self) -> float | None:
        confs = [p.ocr_confidence for p in self.pages if p.ocr_confidence is not None]
        return round(sum(confs) / len(confs), 2) if confs else None

    @property
    def blank_pages(self) -> int:
        return sum(1 for p in self.pages if p.char_count < 5)

    @property
    def rotated_pages(self) -> int:
        return sum(1 for p in self.pages if p.rotation_applied)


@dataclass
class Article:
    """One 'Pasal' with its ayat, located inside a bab/bagian."""

    number: str
    text: str
    bab: str | None = None
    bagian: str | None = None
    paragraf: str | None = None
    page: int | None = None
    ayat: list[tuple[str, str]] = field(default_factory=list)
    # True for a "Pasal N" heading found after the closing formula, i.e. inside
    # a lampiran. Those are cross-references, not normative body text.
    in_attachment: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "in_attachment": self.in_attachment,
            "bab": self.bab,
            "bagian": self.bagian,
            "paragraf": self.paragraf,
            "page": self.page,
            "text": self.text,
            "ayat": [{"number": n, "text": t} for n, t in self.ayat],
        }


@dataclass
class DocumentStructure:
    """Parsed hierarchy of a regulation (URD 3.3: bab, pasal, ayat)."""

    babs: list[dict[str, Any]] = field(default_factory=list)
    # Normative body articles only — the unit of comparison for harmonisation.
    articles: list[Article] = field(default_factory=list)
    # "Pasal N" headings inside lampiran; kept for traceability, not compared.
    attachment_articles: list[Article] = field(default_factory=list)
    # Roman-numeral sections. Surat Edaran (SEOJK/SEBI) have no pasal at all;
    # they are organised as "I. KETENTUAN UMUM", "II. ...".
    sections: list[dict[str, Any]] = field(default_factory=list)
    has_structure: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_structure": self.has_structure,
            "bab_count": len(self.babs),
            "article_count": len(self.articles),
            "attachment_article_count": len(self.attachment_articles),
            "section_count": len(self.sections),
            "babs": self.babs,
            "sections": self.sections,
            "articles": [a.to_dict() for a in self.articles],
            "attachment_articles": [
                a.to_dict() for a in self.attachment_articles
            ],
        }


@dataclass
class IngestRecord:
    """One document as it lands in the knowledge base."""

    doc_id: str
    sha256: str
    source_type: str          # web | upload | local_folder | onedrive
    source_name: str
    source_ref: str           # URL or original path
    original_filename: str
    stored_path: str | None = None
    category: str | None = None
    size_bytes: int = 0
    page_count: int = 0
    ocr_pages: int = 0
    is_scanned: bool = False
    status: str = "pending"   # pending | ingested | duplicate | rejected | failed
    reason: str | None = None
    metadata: RegulationMetadata = field(default_factory=RegulationMetadata)
    ingested_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["metadata"] = self.metadata.to_dict()
        d["ingested_at"] = self.ingested_at.isoformat()
        return d


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
