"""Move knowledge-base files into the source › category › type › year › status layout.

Idempotent: a file already where it belongs is left alone, so this can be
re-run after every discovery (a regulation JDIH later marks "Tidak Berlaku"
moves from .../berlaku/ to .../dicabut/ on the next run).
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from hero import inventory as inv
from hero.config import Settings
from hero.kb.catalog import Catalog
from hero.kb.classify import classify, kb_relative_path
from hero.models import RegulationMetadata

log = logging.getLogger(__name__)

# Categories that described *where a file came from* under the old layout.
# Source is now its own folder level, so these get a real sector instead.
_LEGACY_SOURCE_CATEGORIES = {"rancangan-regulasi", "bank-indonesia"}


@dataclass
class RestructureReport:
    moved: int = 0
    unchanged: int = 0
    missing: int = 0
    status_from_jdih: int = 0
    recategorised: int = 0
    moves: list[tuple[str, str]] = field(default_factory=list)


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 1000):
        cand = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not cand.exists():
            return cand
    return path


def restructure(settings: Settings, dry_run: bool = False) -> RestructureReport:
    rep = RestructureReport()
    kb = settings.knowledge_base.resolve()
    with Catalog(settings.catalog_db) as cat:
        docs = cat.conn.execute("SELECT * FROM documents").fetchall()
        for row in docs:
            stored = Path(row["stored_path"]) if row["stored_path"] else None
            if stored is None or not stored.exists():
                rep.missing += 1
                continue

            source_key = row["source_key"] or inv.source_key_for_ref(
                row["source_type"], row["source_ref"])
            md = RegulationMetadata(
                title=row["title"], subject=row["subject"], doc_type=row["doc_type"],
                number=row["number"], year=row["year"],
                status=row["reg_status"] or "unknown")
            status_source = row["status_source"]

            if source_key == inv.SOURCE_RANCANGAN:
                md.status, status_source = "rancangan", "sumber"
            elif status_source != "jdih":
                key = inv.reg_key(md.doc_type, md.number, md.year)
                hit = cat.find_inventory_by_regkey(key, inv.SOURCE_JDIH) if key else None
                if hit is not None and hit["status"] not in (None, "", "unknown"):
                    if hit["status"] != md.status:
                        rep.status_from_jdih += 1
                    md.status, status_source = hit["status"], "jdih"
                    cat.link_inventory_document(hit["record_key"], row["doc_id"])

            category = row["category"]
            if not category or category in _LEGACY_SOURCE_CATEGORIES:
                text_row = cat.get_text(row["doc_id"])
                text = (text_row["full_text"] or "") if text_row else ""
                category, _ = classify(md, text)
                rep.recategorised += 1

            target = kb / kb_relative_path(md, category, stored.name, source_key)
            if stored.resolve() == target:
                rep.unchanged += 1
                if not dry_run:
                    cat.set_document_source(row["doc_id"], source_key, status_source)
                continue
            target = _unique(target)
            rep.moves.append((str(stored), str(target)))
            rep.moved += 1
            if dry_run:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stored), target)
            rel = target.relative_to(Path.cwd()) if target.is_relative_to(Path.cwd()) else target
            cat.update_stored_path(row["doc_id"], str(rel), category, md.status)
            cat.set_document_source(row["doc_id"], source_key, status_source)

    if not dry_run:
        # Remove folders the old layout leaves empty (deepest first).
        for d in sorted((p for p in kb.rglob("*") if p.is_dir()),
                        key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
    return rep
