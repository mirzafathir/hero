"""SQLite catalog for the knowledge base.

Gives the KB the "dapat ditelusuri dan diperbarui secara konsisten" property
the URD asks for: content-hash deduplication, full-text search over the
extracted text, and a per-document audit trail of where it came from.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from hero.models import IngestRecord, RegulationMetadata

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id            TEXT PRIMARY KEY,
    sha256            TEXT NOT NULL UNIQUE,
    source_type       TEXT NOT NULL,
    source_name       TEXT,
    source_ref        TEXT,
    original_filename TEXT,
    stored_path       TEXT,
    category          TEXT,
    size_bytes        INTEGER DEFAULT 0,
    page_count        INTEGER DEFAULT 0,
    ocr_pages         INTEGER DEFAULT 0,
    is_scanned        INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'pending',
    reason            TEXT,
    title             TEXT,
    subject           TEXT,
    doc_type          TEXT,
    number            TEXT,
    year              INTEGER,
    issued_date       TEXT,
    issuing_body      TEXT,
    reg_status        TEXT,
    confidence        REAL DEFAULT 0,
    metadata_json     TEXT,
    ingested_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_type   ON documents(doc_type);
CREATE INDEX IF NOT EXISTS idx_documents_year   ON documents(year);
CREATE INDEX IF NOT EXISTS idx_documents_cat    ON documents(category);
CREATE INDEX IF NOT EXISTS idx_documents_number ON documents(number);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_ref    ON documents(source_ref);

CREATE TABLE IF NOT EXISTS document_text (
    doc_id      TEXT PRIMARY KEY REFERENCES documents(doc_id) ON DELETE CASCADE,
    full_text   TEXT,
    structure   TEXT,
    analysis    TEXT,
    updated_at  TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS document_fts
USING fts5(doc_id UNINDEXED, title, subject, body);

CREATE TABLE IF NOT EXISTS ingest_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    doc_id      TEXT,
    source_type TEXT,
    source_ref  TEXT,
    status      TEXT,
    reason      TEXT,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id    TEXT REFERENCES documents(doc_id) ON DELETE CASCADE,
    number    TEXT,
    bab       TEXT,
    page      INTEGER,
    text      TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_doc ON articles(doc_id);

-- Conditional-GET cache for scraped listing pages: lets a repeat `hero
-- scrape` send If-None-Match / If-Modified-Since and skip re-downloading a
-- page the site reports as unchanged (HTTP 304).
CREATE TABLE IF NOT EXISTS page_cache (
    url            TEXT PRIMARY KEY,
    etag           TEXT,
    last_modified  TEXT,
    content_hash   TEXT,
    body           TEXT,
    status_code    INTEGER,
    fetched_at     TEXT
);

-- The inventory: one row per regulation a source PUBLISHES, whether or not
-- its document has been downloaded yet. `hero discover` fills it (listing
-- first, then each detail page); `hero harvest` downloads from it and links
-- the resulting knowledge-base document back through doc_id.
CREATE TABLE IF NOT EXISTS inventory (
    record_key       TEXT PRIMARY KEY,   -- the detail-page URL
    source           TEXT NOT NULL,      -- jdih-ojk | ojk-regulasi | ojk-rancangan
    title            TEXT,
    number           TEXT,
    doc_type         TEXT,               -- POJK, SEOJK, PADK, UU, RPOJK ...
    jenis            TEXT,               -- the source's own type label
    sektor           TEXT,
    category         TEXT,               -- HERO knowledge-base category
    year             INTEGER,
    status           TEXT,               -- berlaku | dicabut | diubah | rancangan | unknown
    status_label     TEXT,               -- the source's exact wording
    status_source    TEXT,               -- where the status came from
    reg_key          TEXT,               -- TYPE|number|year, for cross-source matching
    detail_url       TEXT,
    document_url     TEXT,
    document_name    TEXT,
    fields_json      TEXT,               -- every published field, labels verbatim
    attachments_json TEXT,
    listed_at        TEXT,
    enriched_at      TEXT,
    enrich_error     TEXT,
    doc_id           TEXT
);
CREATE INDEX IF NOT EXISTS idx_inventory_source ON inventory(source);
CREATE INDEX IF NOT EXISTS idx_inventory_regkey ON inventory(reg_key);
CREATE INDEX IF NOT EXISTS idx_inventory_docurl ON inventory(document_url);
"""


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


class Catalog:
    """Thin SQLite wrapper. Safe to open repeatedly; schema is idempotent."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # timeout: discovery runs one thread per source, each with its own
        # connection; WAL lets them interleave writes if they wait briefly.
        self.conn = sqlite3.connect(self.db_path, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive schema changes for catalogs created by older versions."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(documents)")}
        if "source_key" not in cols:
            self.conn.execute("ALTER TABLE documents ADD COLUMN source_key TEXT")
        if "status_source" not in cols:
            self.conn.execute("ALTER TABLE documents ADD COLUMN status_source TEXT")

    # -- inventory ---------------------------------------------------------
    _INVENTORY_SCALARS = (
        "source", "title", "number", "doc_type", "jenis", "sektor", "category",
        "year", "status", "status_label", "status_source", "reg_key",
        "detail_url", "document_url", "document_name", "listed_at",
        "enriched_at", "enrich_error", "doc_id",
    )

    def get_inventory(self, record_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM inventory WHERE record_key = ?", (record_key,)).fetchone()

    def upsert_inventory(self, rec: dict[str, Any]) -> bool:
        """Insert or merge one inventory record. Returns True if it was new.

        Merge rules: a ``None`` never overwrites a known value, and the
        ``fields`` / ``attachments`` payloads are merged key by key, so a
        listing pass re-run later cannot erase what a detail pass found.
        """
        key = rec["record_key"]
        old = self.get_inventory(key)
        merged: dict[str, Any] = dict(old) if old is not None else {}
        for col in self._INVENTORY_SCALARS:
            if col in rec and (rec[col] is not None or col == "enrich_error"):
                merged[col] = rec[col]
        fields = json.loads(merged.get("fields_json") or "{}")
        fields.update({k: v for k, v in (rec.get("fields") or {}).items()
                       if v not in (None, "")})
        merged["fields_json"] = json.dumps(fields, ensure_ascii=False)
        if rec.get("attachments") is not None:
            merged["attachments_json"] = json.dumps(rec["attachments"], ensure_ascii=False)
        merged["record_key"] = key
        merged.setdefault("source", rec.get("source", "unknown"))

        cols = ["record_key", *self._INVENTORY_SCALARS, "fields_json", "attachments_json"]
        # doc_id is COALESCEd in SQL rather than carried in `merged`: another
        # connection (a harvest, a restructure) may link the document between
        # our read above and this write, and must not be silently undone.
        updates = ", ".join(
            "doc_id = COALESCE(excluded.doc_id, inventory.doc_id)" if c == "doc_id"
            else f"{c} = excluded.{c}" for c in cols if c != "record_key")
        self.conn.execute(
            f"INSERT INTO inventory ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT(record_key) DO UPDATE SET {updates}",
            [merged.get(c) for c in cols],
        )
        self.conn.commit()
        return old is None

    def list_inventory(
        self, source: str | Iterable[str] | None = None, *,
        pending_enrich: bool = False, pending_download: bool = False,
        status: str | None = None, category: str | None = None,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
        if source:
            sources = [source] if isinstance(source, str) else list(source)
            clauses.append(f"source IN ({', '.join('?' for _ in sources)})")
            params.extend(sources)
        if pending_enrich:
            clauses.append("enriched_at IS NULL")
        if pending_download:
            # Exclude rows already known to have nothing downloadable (e.g. a
            # draft published only as .docx) so they don't eat every --limit.
            clauses.append("doc_id IS NULL AND (document_url IS NOT NULL "
                           "OR enriched_at IS NULL)")
        if status:
            clauses.append("status = ?")
            params.append(status)
        if category:
            clauses.append("category = ?")
            params.append(category)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (f"SELECT * FROM inventory {where} "
               f"ORDER BY source, COALESCE(year, 0) DESC, title")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql, params).fetchall()

    def find_inventory_by_regkey(self, reg_key: str,
                                 source: str | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM inventory WHERE reg_key = ?"
        params: list[Any] = [reg_key]
        if source:
            sql += " AND source = ?"
            params.append(source)
        return self.conn.execute(sql + " LIMIT 1", params).fetchone()

    def find_inventory_by_document_url(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM inventory WHERE document_url = ? LIMIT 1", (url,)).fetchone()

    def link_inventory_document(self, record_key: str, doc_id: str) -> None:
        self.conn.execute("UPDATE inventory SET doc_id = ? WHERE record_key = ?",
                          (doc_id, record_key))
        self.conn.commit()

    def inventory_stats(self) -> dict[str, Any]:
        c = self.conn
        return {
            "total": c.execute("SELECT COUNT(*) FROM inventory").fetchone()[0],
            "enriched": c.execute(
                "SELECT COUNT(*) FROM inventory WHERE enriched_at IS NOT NULL").fetchone()[0],
            "downloaded": c.execute(
                "SELECT COUNT(*) FROM inventory WHERE doc_id IS NOT NULL").fetchone()[0],
            "by_source": {r[0]: r[1] for r in c.execute(
                "SELECT source, COUNT(*) FROM inventory GROUP BY source ORDER BY 2 DESC")},
            "by_status": {r[0] or "?": r[1] for r in c.execute(
                "SELECT status, COUNT(*) FROM inventory GROUP BY status ORDER BY 2 DESC")},
        }

    def set_document_source(self, doc_id: str, source_key: str | None,
                            status_source: str | None = None) -> None:
        self.conn.execute(
            "UPDATE documents SET source_key = COALESCE(?, source_key), "
            "status_source = COALESCE(?, status_source) WHERE doc_id = ?",
            (source_key, status_source, doc_id))
        self.conn.commit()

    def update_stored_path(self, doc_id: str, stored_path: str,
                           category: str | None = None,
                           reg_status: str | None = None) -> None:
        self.conn.execute(
            "UPDATE documents SET stored_path = ?, "
            "category = COALESCE(?, category), reg_status = COALESCE(?, reg_status) "
            "WHERE doc_id = ?", (stored_path, category, reg_status, doc_id))
        self.conn.commit()

    # -- lifecycle -------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes ----------------------------------------------------------
    def exists(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM documents WHERE sha256 = ?", (sha256,)
        ).fetchone()

    def seen_source_ref(self, source_ref: str) -> sqlite3.Row | None:
        """Has this exact URL/path already produced a stored document?

        Sector listing pages on ojk.go.id cross-link the same regulations, so
        without this check one run downloads the same PDF several times only to
        throw it away on the content hash.
        """
        return self.conn.execute(
            "SELECT * FROM documents WHERE source_ref = ? "
            "AND status IN ('ingested', 'failed') LIMIT 1", (source_ref,)
        ).fetchone()

    # -- page cache (conditional GET for the scraper) --------------------
    def get_page_cache(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM page_cache WHERE url = ?", (url,)
        ).fetchone()

    def put_page_cache(
        self, url: str, etag: str | None, last_modified: str | None,
        content_hash: str | None, body: str | None, status_code: int,
    ) -> None:
        self.conn.execute(
            """INSERT INTO page_cache
               (url, etag, last_modified, content_hash, body, status_code, fetched_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                 etag=excluded.etag, last_modified=excluded.last_modified,
                 content_hash=excluded.content_hash, body=excluded.body,
                 status_code=excluded.status_code, fetched_at=excluded.fetched_at""",
            (url, etag, last_modified, content_hash, body, status_code,
             datetime.now().isoformat()),
        )
        self.conn.commit()

    def upsert_document(self, rec: IngestRecord) -> None:
        md = rec.metadata
        self.conn.execute(
            """
            INSERT INTO documents (
                doc_id, sha256, source_type, source_name, source_ref,
                original_filename, stored_path, category, size_bytes,
                page_count, ocr_pages, is_scanned, status, reason,
                title, subject, doc_type, number, year, issued_date,
                issuing_body, reg_status, confidence, metadata_json, ingested_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(sha256) DO UPDATE SET
                stored_path = excluded.stored_path,
                category    = excluded.category,
                page_count  = excluded.page_count,
                ocr_pages   = excluded.ocr_pages,
                is_scanned  = excluded.is_scanned,
                status      = excluded.status,
                reason      = excluded.reason,
                title       = COALESCE(excluded.title, documents.title),
                subject     = COALESCE(excluded.subject, documents.subject),
                doc_type    = COALESCE(excluded.doc_type, documents.doc_type),
                number      = COALESCE(excluded.number, documents.number),
                year        = COALESCE(excluded.year, documents.year),
                issued_date = COALESCE(excluded.issued_date, documents.issued_date),
                confidence  = excluded.confidence,
                metadata_json = excluded.metadata_json
            """,
            (
                rec.doc_id, rec.sha256, rec.source_type, rec.source_name,
                rec.source_ref, rec.original_filename, rec.stored_path,
                rec.category, rec.size_bytes, rec.page_count, rec.ocr_pages,
                int(rec.is_scanned), rec.status, rec.reason,
                md.title, md.subject, md.doc_type, md.number, md.year,
                md.issued_date.isoformat() if md.issued_date else None,
                md.issuing_body, md.status, md.confidence,
                json.dumps(md.to_dict(), ensure_ascii=False, default=_json_default),
                rec.ingested_at.isoformat(),
            ),
        )
        self.conn.commit()

    def save_text(
        self, doc_id: str, full_text: str,
        structure: dict | None = None, analysis: dict | None = None,
    ) -> None:
        now = datetime.now().isoformat()
        self.conn.execute(
            """INSERT INTO document_text (doc_id, full_text, structure, analysis, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(doc_id) DO UPDATE SET
                 full_text=excluded.full_text, structure=excluded.structure,
                 analysis=excluded.analysis, updated_at=excluded.updated_at""",
            (
                doc_id, full_text,
                json.dumps(structure, ensure_ascii=False, default=_json_default)
                if structure else None,
                json.dumps(analysis, ensure_ascii=False, default=_json_default)
                if analysis else None,
                now,
            ),
        )
        row = self.conn.execute(
            "SELECT title, subject FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        self.conn.execute("DELETE FROM document_fts WHERE doc_id = ?", (doc_id,))
        self.conn.execute(
            "INSERT INTO document_fts (doc_id, title, subject, body) VALUES (?,?,?,?)",
            (doc_id, (row["title"] if row else "") or "",
             (row["subject"] if row else "") or "", full_text),
        )
        self.conn.commit()

    def save_articles(self, doc_id: str, articles: list[dict]) -> None:
        self.conn.execute("DELETE FROM articles WHERE doc_id = ?", (doc_id,))
        self.conn.executemany(
            "INSERT INTO articles (doc_id, number, bab, page, text) VALUES (?,?,?,?,?)",
            [(doc_id, a.get("number"), a.get("bab"), a.get("page"), a.get("text"))
             for a in articles],
        )
        self.conn.commit()

    def log_ingest(self, run_id: str, rec: IngestRecord) -> None:
        self.conn.execute(
            """INSERT INTO ingest_log
               (run_id, doc_id, source_type, source_ref, status, reason, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (run_id, rec.doc_id, rec.source_type, rec.source_ref,
             rec.status, rec.reason, datetime.now().isoformat()),
        )
        self.conn.commit()

    def delete_document(self, doc_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM document_fts WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))

    # -- reads -----------------------------------------------------------
    def get(self, doc_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM documents WHERE doc_id = ? OR sha256 = ?"
            " OR doc_id LIKE ? || '%'", (doc_id, doc_id, doc_id)
        ).fetchone()

    def get_text(self, doc_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM document_text WHERE doc_id = ?", (doc_id,)
        ).fetchone()

    def list_documents(
        self, category: str | None = None, doc_type: str | None = None,
        year: int | None = None, status: str | None = None, limit: int = 100,
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
        for column, value in (
            ("category", category), ("doc_type", doc_type),
            ("year", year), ("status", status),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self.conn.execute(
            f"SELECT * FROM documents {where} "
            f"ORDER BY COALESCE(year, 0) DESC, ingested_at DESC LIMIT ?",
            params,
        ).fetchall()

    def search(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        """Full-text search with a snippet of the matching passage."""
        try:
            return self.conn.execute(
                """SELECT d.*, snippet(document_fts, 3, char(2), char(3), ' … ', 18) AS snippet
                   FROM document_fts f JOIN documents d ON d.doc_id = f.doc_id
                   WHERE document_fts MATCH ? ORDER BY rank LIMIT ?""",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # Bare user input can be invalid FTS5 syntax; retry as a phrase.
            safe = '"' + query.replace('"', " ") + '"'
            return self.conn.execute(
                """SELECT d.*, snippet(document_fts, 3, char(2), char(3), ' … ', 18) AS snippet
                   FROM document_fts f JOIN documents d ON d.doc_id = f.doc_id
                   WHERE document_fts MATCH ? ORDER BY rank LIMIT ?""",
                (safe, limit),
            ).fetchall()

    def stats(self) -> dict[str, Any]:
        c = self.conn
        total = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        return {
            "total_documents": total,
            "ingested": c.execute(
                "SELECT COUNT(*) FROM documents WHERE status='ingested'"
            ).fetchone()[0],
            "scanned_documents": c.execute(
                "SELECT COUNT(*) FROM documents WHERE is_scanned=1"
            ).fetchone()[0],
            "with_text": c.execute("SELECT COUNT(*) FROM document_text").fetchone()[0],
            "total_pages": c.execute(
                "SELECT COALESCE(SUM(page_count),0) FROM documents"
            ).fetchone()[0],
            "ocr_pages": c.execute(
                "SELECT COALESCE(SUM(ocr_pages),0) FROM documents"
            ).fetchone()[0],
            "by_category": {
                r["category"] or "?": r["n"] for r in c.execute(
                    "SELECT category, COUNT(*) n FROM documents "
                    "GROUP BY category ORDER BY n DESC")
            },
            "by_type": {
                r["doc_type"] or "?": r["n"] for r in c.execute(
                    "SELECT doc_type, COUNT(*) n FROM documents "
                    "GROUP BY doc_type ORDER BY n DESC")
            },
            "by_source": {
                r["source_type"] or "?": r["n"] for r in c.execute(
                    "SELECT source_type, COUNT(*) n FROM documents "
                    "GROUP BY source_type ORDER BY n DESC")
            },
            "by_year": {
                str(r["year"] or "?"): r["n"] for r in c.execute(
                    "SELECT year, COUNT(*) n FROM documents "
                    "GROUP BY year ORDER BY year DESC LIMIT 12")
            },
        }
