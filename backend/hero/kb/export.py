"""Export the knowledge base to analysis-friendly files.

The catalog is the system of record, but downstream work (spreadsheets,
notebooks, the harmonisation and PoV modules) wants flat tables. Three
datasets are produced, joinable on ``doc_id``:

    documents.csv     one row per regulation, with its metadata
    articles.csv      one row per pasal, the unit of comparison for URD 3.4
    takeaways.csv     one row per normative clause found (URD 3.3)

``jsonl`` writes the same three datasets with nested fields preserved.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterator

from hero.kb.catalog import Catalog

DOCUMENT_FIELDS = [
    "doc_id", "sha256", "doc_type", "number", "year", "issued_date",
    "title", "subject", "issuing_body", "reg_status", "category",
    "source_type", "source_name", "source_ref", "stored_path",
    "page_count", "ocr_pages", "is_scanned", "size_bytes",
    "confidence", "status", "ingested_at",
]
ARTICLE_FIELDS = [
    "doc_id", "doc_type", "number", "pasal", "bab", "page", "ayat_count", "text",
]
TAKEAWAY_FIELDS = [
    "doc_id", "doc_type", "number", "category", "pasal", "page",
    "it_relevant", "score", "text",
]


def _documents(cat: Catalog) -> Iterator[dict[str, Any]]:
    for row in cat.list_documents(limit=1_000_000):
        yield {k: row[k] for k in DOCUMENT_FIELDS}


def _articles(cat: Catalog) -> Iterator[dict[str, Any]]:
    for row in cat.list_documents(limit=1_000_000):
        text_row = cat.get_text(row["doc_id"])
        if not text_row or not text_row["structure"]:
            continue
        for art in json.loads(text_row["structure"]).get("articles", []):
            yield {
                "doc_id": row["doc_id"],
                "doc_type": row["doc_type"],
                "number": row["number"],
                "pasal": art["number"],
                "bab": art.get("bab"),
                "page": art.get("page"),
                "ayat_count": len(art.get("ayat") or []),
                "text": art.get("text", ""),
            }


def _takeaways(cat: Catalog) -> Iterator[dict[str, Any]]:
    for row in cat.list_documents(limit=1_000_000):
        text_row = cat.get_text(row["doc_id"])
        if not text_row or not text_row["analysis"]:
            continue
        for item in json.loads(text_row["analysis"]).get("key_takeaways", []):
            yield {
                "doc_id": row["doc_id"],
                "doc_type": row["doc_type"],
                "number": row["number"],
                "category": item["category"],
                "pasal": item.get("pasal"),
                "page": item.get("page"),
                "it_relevant": item.get("it_relevant"),
                "score": item.get("score"),
                "text": item["text"],
            }


DATASETS = {
    "documents": (DOCUMENT_FIELDS, _documents),
    "articles": (ARTICLE_FIELDS, _articles),
    "takeaways": (TAKEAWAY_FIELDS, _takeaways),
}


def export(catalog: Catalog, out_dir: Path, fmt: str = "csv") -> dict[str, int]:
    """Write every dataset into ``out_dir``. Returns rows written per dataset."""
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    for name, (fields, producer) in DATASETS.items():
        rows = list(producer(catalog))
        counts[name] = len(rows)
        if fmt == "jsonl":
            path = out_dir / f"{name}.jsonl"
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        else:
            path = out_dir / f"{name}.csv"
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                # utf-8-sig so Excel opens Indonesian text correctly.
                writer = csv.DictWriter(fh, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)
    return counts
