"""The ingest pipeline: fetch → validate → extract → classify → store.

One entry point (`IngestPipeline.ingest_file`) is shared by all three intake
routes required by URD 4.1, so a document behaves identically whether it was
scraped, uploaded, or read from a folder.
"""
from __future__ import annotations

import logging
import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hero.config import Settings
from hero.extract.pdf import extract_pdf, is_pdf, probe_pdf
from hero.extract.metadata import extract_metadata
from hero.extract.structure import parse_structure
from hero.extract.summary import build_summary
from hero.extract.quality import assess_quality
from hero.ingest.folders import scan_folder
from hero.ingest.onedrive import download_onedrive_share
from hero import inventory as inv
from hero.ingest import sharepoint
from hero.ingest.archive import extract_primary_pdf, is_zip
from hero.ingest.jdih import harvest_jdih
from hero.ingest.jdih import parse_detail as jdih_parse_detail
from hero.ingest.web import PdfLink, WebScraper
from hero.kb.catalog import Catalog
from hero.kb.classify import classify, kb_relative_path
from hero.models import IngestRecord, RegulationMetadata, sha256_file

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, str], None]


@dataclass
class RunSummary:
    run_id: str
    records: list[IngestRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for r in self.records if r.status == status)

    @property
    def ingested(self) -> int:
        return self.count("ingested")

    @property
    def duplicates(self) -> int:
        return self.count("duplicate")

    @property
    def rejected(self) -> int:
        return self.count("rejected") + self.count("failed")

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ingested": self.ingested,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "errors": self.errors,
            "documents": [r.to_dict() for r in self.records],
        }


class IngestPipeline:
    def __init__(
        self,
        settings: Settings,
        catalog: Catalog | None = None,
        analyze: bool = True,
        force_ocr: bool = False,
    ):
        self.settings = settings
        self.catalog = catalog or Catalog(settings.catalog_db)
        self.analyze = analyze
        self.force_ocr = force_ocr
        settings.knowledge_base.mkdir(parents=True, exist_ok=True)
        settings.staging_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Single document
    # ------------------------------------------------------------------
    def ingest_file(
        self,
        path: Path,
        source_type: str,
        source_name: str,
        source_ref: str | None = None,
        category_hint: str | None = None,
        run_id: str | None = None,
        move: bool = False,
        metadata_hint: dict | None = None,
        source_key: str | None = None,
    ) -> IngestRecord:
        """Validate, extract, classify and file one PDF into the KB.

        ``metadata_hint`` carries facts the *source* already knows and that
        are more reliable than anything parsed out of the PDF — notably the
        legal status published by JDIH OJK alongside each regulation.
        """
        path = Path(path)
        rec = IngestRecord(
            doc_id=uuid.uuid4().hex,
            sha256="",
            source_type=source_type,
            source_name=source_name,
            source_ref=source_ref or str(path),
            original_filename=path.name,
        )

        if not path.exists():
            rec.status, rec.reason = "rejected", "file not found"
            return self._finish(rec, run_id)

        rec.size_bytes = path.stat().st_size
        if rec.size_bytes == 0:
            rec.status, rec.reason = "rejected", "empty file"
            return self._finish(rec, run_id)

        # URD 3.2: "Validasi format file" — magic bytes, not the extension.
        if not is_pdf(path):
            rec.status, rec.reason = "rejected", "not a valid PDF"
            return self._finish(rec, run_id)

        rec.sha256 = sha256_file(path)
        if (existing := self.catalog.exists(rec.sha256)) is not None:
            rec.doc_id = existing["doc_id"]
            rec.status = "duplicate"
            rec.reason = f"identical content already stored at {existing['stored_path']}"
            rec.stored_path = existing["stored_path"]
            rec.category = existing["category"]
            return self._finish(rec, run_id, log_only=True)

        probe = probe_pdf(path, self.settings.pdf)
        if probe.get("error") and not probe.get("page_count"):
            rec.status, rec.reason = "rejected", probe["error"]
            return self._finish(rec, run_id)

        extraction = extract_pdf(
            path, self.settings.ocr, self.settings.pdf, force_ocr=self.force_ocr)
        rec.page_count = extraction.page_count
        rec.ocr_pages = extraction.ocr_pages
        rec.is_scanned = extraction.is_scanned

        if extraction.error and extraction.char_count == 0:
            # Still archive it — a human can escalate it manually (URD 7.1).
            rec.status, rec.reason = "failed", extraction.error
            rec.metadata = RegulationMetadata(
                title=path.stem.replace("_", " "),
                warnings=[extraction.error],
            )
        else:
            rec.metadata = extract_metadata(
                extraction.text, probe.get("pdf_metadata"), path.name)
            rec.status = "ingested"

        status_source = "teks-dokumen"
        if metadata_hint:
            self._apply_metadata_hint(rec.metadata, metadata_hint)
            if metadata_hint.get("status") not in (None, "", "unknown"):
                status_source = metadata_hint.get("status_source") or "sumber"
        if status_source == "teks-dokumen":
            # No source told us the status: borrow JDIH's, if it lists the
            # same regulation (TYPE|number|year), before trusting the text.
            md = rec.metadata
            key = inv.reg_key(md.doc_type, md.number, md.year)
            hit = self.catalog.find_inventory_by_regkey(key, inv.SOURCE_JDIH) if key else None
            if hit is not None and hit["status"] not in (None, "", "unknown"):
                md.status = hit["status"]
                if hit["status_label"]:
                    md.warnings.append(f"status menurut JDIH: {hit['status_label']}")
                status_source = "jdih"

        source_key = source_key or inv.source_key_for_ref(source_type, source_ref)
        category, hits = classify(rec.metadata, extraction.text, category_hint)
        rec.category = category

        target = self.settings.knowledge_base / kb_relative_path(
            rec.metadata, category, path.name, source_key)
        target = self._unique_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(path), target)
        else:
            shutil.copy2(path, target)
        rec.stored_path = str(target)

        self.catalog.upsert_document(rec)
        self.catalog.set_document_source(rec.doc_id, source_key, status_source)
        if extraction.char_count:
            struct = parse_structure(extraction.pages)
            analysis: dict[str, Any] | None = None
            if self.analyze:
                analysis = build_summary(extraction.text, struct, rec.metadata)
                analysis["classification"] = {
                    "category": category, "matched_keywords": hits,
                }
                analysis["extraction"] = {
                    "page_count": extraction.page_count,
                    "ocr_pages": extraction.ocr_pages,
                    "is_scanned": extraction.is_scanned,
                    "seconds": extraction.duration_seconds,
                    "repaired": extraction.repaired,
                }
                analysis["quality"] = assess_quality(extraction)
            self.catalog.save_text(
                rec.doc_id, extraction.text, struct.to_dict(), analysis)
            if struct.articles:
                self.catalog.save_articles(
                    rec.doc_id, [a.to_dict() for a in struct.articles])

        return self._finish(rec, run_id, log_only=True)

    def _already_have_url(self, url: str) -> bool:
        if self.catalog.seen_source_ref(url) is not None:
            return True
        # A detail-page URL is an inventory key; it counts as "have" once
        # the inventory row has been linked to a knowledge-base document.
        row = self.catalog.get_inventory(url)
        return bool(row is not None and row["doc_id"])

    # ------------------------------------------------------------------
    # Harvest — download documents for inventory rows
    # ------------------------------------------------------------------
    def harvest_inventory(
        self, *, sources: list[str] | None = None, status: str | None = None,
        category: str | None = None, limit: int | None = None,
        record_keys: list[str] | None = None, scraper: WebScraper | None = None,
        run: RunSummary | None = None, progress: ProgressFn | None = None,
        status_override: str | None = None,
    ) -> RunSummary:
        """Download and ingest the documents behind inventory rows.

        Rows not yet enriched are enriched on the fly (the document URL lives
        on the detail page). ZIP archives are unpacked to their main PDF.
        Each ingested document is linked back to its inventory row, so a
        second harvest never downloads the same regulation twice.
        """
        run = run or RunSummary(run_id=uuid.uuid4().hex[:12])
        if record_keys is not None:
            rows = [r for r in (self.catalog.get_inventory(k) for k in record_keys) if r]
        else:
            rows = self.catalog.list_inventory(
                sources, pending_download=True, status=status,
                category=category, limit=limit)
        own_scraper = scraper is None
        scraper = scraper or WebScraper(self.settings.scraper)
        staging = self.settings.staging_dir / f"harvest-{run.run_id}"
        try:
            for i, raw in enumerate(rows, start=1):
                row = dict(raw)
                title = (row.get("title") or row["record_key"])[:90]
                if progress:
                    progress("doc", f"{i}/{len(rows)} {title}")
                if not row.get("document_url") and row["source"] in (
                        inv.SOURCE_JDIH, inv.SOURCE_REGULASI, inv.SOURCE_RANCANGAN):
                    row = self._enrich_row(scraper, row) or row
                doc_url = row.get("document_url")
                if not doc_url:
                    atts = json.loads(row.get("attachments_json") or "[]")
                    exts = sorted({a.get("ext") for a in atts if a.get("ext")})
                    if exts and not {"pdf", "zip"} & set(exts):
                        run.errors.append(
                            f"[{row['source']}] hanya tersedia sebagai {'/'.join(exts)} "
                            f"— di luar cakupan PDF (URD 4.2), tidak diunduh: {title}")
                    else:
                        run.errors.append(f"[{row['source']}] tidak ada dokumen untuk: {title}")
                    continue
                known = self.catalog.seen_source_ref(doc_url)
                if known is not None:
                    self.catalog.link_inventory_document(row["record_key"], known["doc_id"])
                    run.records.append(self._stub_record(
                        row, doc_url, "duplicate",
                        f"sudah ada di knowledge base ({known['stored_path']})", known["doc_id"]))
                    continue

                fetch = scraper.download_pdf(
                    PdfLink(doc_url, row.get("title") or "", row.get("detail_url") or ""),
                    staging, accept_zip=True)
                if not fetch.ok:
                    run.records.append(self._finish(self._stub_record(
                        row, doc_url, "rejected", fetch.reason), run.run_id))
                    continue
                path = fetch.path
                if is_zip(path):
                    pdf, members = extract_primary_pdf(path, staging)
                    self.catalog.upsert_inventory({
                        "record_key": row["record_key"],
                        "fields": {"Isi Arsip": "; ".join(m["name"] for m in members)},
                    })
                    path.unlink(missing_ok=True)
                    if pdf is None:
                        run.records.append(self._finish(self._stub_record(
                            row, doc_url, "rejected", "arsip ZIP tanpa PDF"), run.run_id))
                        continue
                    path = pdf

                hint = {
                    "status": status_override or row.get("status"),
                    "status_label": row.get("status_label"),
                    "status_source": row.get("status_source") or "sumber",
                    "doc_type": row.get("doc_type"), "number": row.get("number"),
                    "year": row.get("year"), "source_title": row.get("title"),
                    "doc_type_label": row.get("jenis"),
                    "override": ("year",) if row["source"] == inv.SOURCE_RANCANGAN else (),
                }
                rec = self.ingest_file(
                    path, "web", inv.SOURCE_LABELS.get(row["source"], row["source"]),
                    doc_url, row.get("category"), run.run_id, move=True,
                    metadata_hint=hint, source_key=row["source"])
                if rec.doc_id and rec.status in ("ingested", "failed", "duplicate"):
                    self.catalog.link_inventory_document(row["record_key"], rec.doc_id)
                run.records.append(rec)
        finally:
            if own_scraper:
                scraper.close()
            self._clean_staging(staging)
        return run

    def _enrich_row(self, scraper: WebScraper, row: dict) -> dict | None:
        html, _ = scraper.get_html(row["detail_url"])
        if html is None:
            return None
        if row["source"] == inv.SOURCE_JDIH:
            upd = inv.apply_jdih_detail(row, jdih_parse_detail(html))
        else:
            upd = inv.apply_sharepoint_detail(
                row, sharepoint.parse_detail(html, row["detail_url"]), row["source"])
        self.catalog.upsert_inventory(upd)
        got = self.catalog.get_inventory(row["record_key"])
        return dict(got) if got else None

    @staticmethod
    def _stub_record(row: dict, doc_url: str, status: str, reason: str | None,
                     doc_id: str | None = None) -> IngestRecord:
        return IngestRecord(
            doc_id=doc_id or uuid.uuid4().hex, sha256="", source_type="web",
            source_name=inv.SOURCE_LABELS.get(row["source"], row["source"]),
            source_ref=doc_url, original_filename=Path(doc_url).name,
            status=status, reason=reason,
            metadata=RegulationMetadata(
                title=row.get("title"), number=row.get("number"),
                doc_type=row.get("doc_type"), year=row.get("year"),
                status=row.get("status") or "unknown"),
        )

    def _pending_sharepoint_rows(
        self, scraper: WebScraper, site, want: int,
    ) -> tuple[list[str], int, int, list[str]]:
        """Walk an ojk.go.id listing until ``want`` rows lack a document.

        Returns (record keys to harvest, rows skipped as already held, pages
        read, errors). Every row seen is recorded in the inventory on the way.
        """
        source = inv.source_key_for_site(site)
        kind = "rancangan" if source == inv.SOURCE_RANCANGAN else "regulasi"
        keys: list[str] = []
        skipped = pages = 0
        errors: list[str] = []
        for page, rows in sharepoint.iter_listing(scraper, site.url, kind, None, errors):
            pages = page
            for row in rows:
                self.catalog.upsert_inventory(inv.record_from_sharepoint_row(row, source))
                got = self.catalog.get_inventory(row.url)
                if got is not None and got["doc_id"]:
                    skipped += 1
                    continue
                if got is not None and got["enriched_at"] and not got["document_url"]:
                    continue   # read before, nothing downloadable (e.g. .docx only)
                keys.append(row.url)
                if len(keys) >= want:
                    return keys, skipped, pages, errors
        return keys, skipped, pages, errors

    @staticmethod
    def _apply_metadata_hint(md: RegulationMetadata, hint: dict) -> None:
        """Overlay source-supplied metadata onto what the parser inferred.

        The source wins for ``status``: a portal that publishes
        "Tidak Berlaku" next to a regulation is authoritative in a way the
        PDF text never is — the document itself rarely says it was revoked
        (URD 3.3 "status berlaku/dicabut", URD 3.4 status checking).
        Everything else only fills gaps, so a good parse is never overwritten.
        """
        status = hint.get("status")
        if status and status != "unknown":
            md.status = status
        label = hint.get("status_label")
        if label and label.strip().lower() != (status or ""):
            md.warnings.append(f"status menurut sumber: {label}")
        # "override" names keys where the source beats the parser outright —
        # e.g. a draft's year is when OJK published it for comment; the
        # draft's own text often mentions a later effective year instead.
        overrides = set(hint.get("override") or ())
        for key in ("title", "subject", "number", "doc_type", "doc_type_label",
                    "issuing_body", "year"):
            value = hint.get(key)
            if value and (key in overrides or not getattr(md, key, None)):
                setattr(md, key, value)
        if hint.get("source_title") and not md.subject:
            md.subject = hint["source_title"]

    def _finish(self, rec: IngestRecord, run_id: str | None,
                log_only: bool = False) -> IngestRecord:
        if not log_only and rec.status in ("rejected", "failed"):
            log.warning("%s: %s (%s)", rec.original_filename, rec.status, rec.reason)
        if run_id:
            self.catalog.log_ingest(run_id, rec)
        return rec

    @staticmethod
    def _clean_staging(staging: Path) -> None:
        """Remove the run's staging directory once it is empty.

        Successful documents are moved into the knowledge base; anything left
        behind belongs to a document that failed and is kept for inspection.
        """
        try:
            if staging.is_dir() and not any(staging.iterdir()):
                staging.rmdir()
        except OSError:      # never let cleanup fail a run
            log.debug("could not remove staging dir %s", staging)

    @staticmethod
    def _unique_path(path: Path) -> Path:
        """Never overwrite: different content with the same derived name coexists."""
        if not path.exists():
            return path
        for n in range(2, 1000):
            candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
            if not candidate.exists():
                return candidate
        return path.with_name(f"{path.stem}-{uuid.uuid4().hex[:6]}{path.suffix}")

    # ------------------------------------------------------------------
    # Route 1 — scraping registered sites
    # ------------------------------------------------------------------
    def run_sites(
        self, sites=None, limit: int | None = None,
        progress: ProgressFn | None = None,
    ) -> RunSummary:
        run = RunSummary(run_id=uuid.uuid4().hex[:12])
        sites = sites if sites is not None else self.settings.enabled_sites
        if not sites:
            run.errors.append("no sites configured (see config/sources.yaml)")
            return run

        staging = self.settings.staging_dir / f"web-{run.run_id}"
        with WebScraper(self.settings.scraper) as scraper:
            for site in sites:
                if progress:
                    progress("site", f"{site.name} — {site.url}")
                adapter = site.resolved_adapter()
                jdih_entries: dict = {}
                if adapter == "ojk_sharepoint":
                    want = limit if limit is not None else site.max_documents
                    keys, skipped, pages, errors = self._pending_sharepoint_rows(
                        scraper, site, want)
                    run.errors.extend(f"[{site.name}] {e}" for e in errors)
                    if skipped:
                        run.errors.append(
                            f"[{site.name}] {skipped} dokumen sudah ada di "
                            f"knowledge base — tidak diunduh ulang")
                    if not keys:
                        run.errors.append(
                            f"[{site.name}] tidak ada dokumen baru "
                            f"({pages} halaman daftar dibaca)")
                    self.harvest_inventory(
                        record_keys=keys, scraper=scraper, run=run,
                        progress=progress, status_override=site.status_hint)
                    continue
                if adapter == "jdih_ojk":
                    report, jdih_entries = harvest_jdih(
                        scraper, site, staging, limit,
                        skip_url=self._already_have_url)
                else:
                    report = scraper.harvest_site(
                        site, staging, limit, skip_url=self._already_have_url,
                        cache=self.catalog if self.settings.scraper.use_page_cache
                        else None)
                run.errors.extend(f"[{site.name}] {e}" for e in report.errors)
                if report.skipped_known:
                    run.errors.append(
                        f"[{site.name}] {report.skipped_known} dokumen sudah ada "
                        f"di knowledge base — tidak diunduh ulang")
                if report.pages_cached:
                    run.errors.append(
                        f"[{site.name}] {report.pages_cached}/{report.pages_visited} "
                        f"halaman tidak berubah sejak run sebelumnya (304, dari cache)")
                if not report.fetched and not report.skipped_known:
                    run.errors.append(
                        f"[{site.name}] no PDFs downloaded "
                        f"({report.links_found} link(s) seen on "
                        f"{report.pages_visited} page(s))")
                for fetch in report.fetched:
                    if not fetch.ok:
                        rec = IngestRecord(
                            doc_id=uuid.uuid4().hex, sha256="", source_type="web",
                            source_name=site.name, source_ref=fetch.link.url,
                            original_filename=Path(fetch.link.url).name,
                            status="rejected", reason=fetch.reason,
                        )
                        run.records.append(self._finish(rec, run.run_id))
                        continue
                    if progress:
                        progress("doc", fetch.path.name)
                    entry = jdih_entries.get(fetch.link.url)
                    hint = None
                    category_hint = site.category_hint
                    record_key = None
                    if entry is not None:
                        hint = {
                            "status": entry.normalised_status(),
                            # Keep JDIH's exact wording: "Berlaku (Dicabut
                            # Sebagian)" carries nuance the 4-value status
                            # vocabulary cannot, and a reviewer needs it.
                            "status_label": entry.status,
                            "status_source": "jdih",
                            "doc_type_label": entry.jenis_label,
                            "source_title": entry.title,
                        }
                        category_hint = category_hint or entry.category
                        inv_rec = inv.record_from_jdih(entry)
                        inv_rec["document_url"] = fetch.link.url
                        self.catalog.upsert_inventory(inv_rec)
                        record_key = inv_rec["record_key"]
                    if site.status_hint:
                        hint = {**(hint or {}), "status": site.status_hint,
                                "status_source": "sumber"}
                    rec = self.ingest_file(
                        fetch.path, "web", site.name, fetch.link.url,
                        category_hint, run.run_id, move=True,
                        metadata_hint=hint)
                    if record_key and rec.doc_id and rec.status != "rejected":
                        self.catalog.link_inventory_document(record_key, rec.doc_id)
                    run.records.append(rec)
        self._clean_staging(staging)
        return run

    # ------------------------------------------------------------------
    # Route 2 — manual upload
    # ------------------------------------------------------------------
    def run_upload(
        self, paths: list[Path], source_name: str = "manual-upload",
        category_hint: str | None = None, progress: ProgressFn | None = None,
    ) -> RunSummary:
        run = RunSummary(run_id=uuid.uuid4().hex[:12])
        expanded: list[Path] = []
        for p in paths:
            p = Path(p)
            if p.is_dir():
                expanded.extend(sorted(p.rglob("*.pdf")))
            else:
                expanded.append(p)
        for path in expanded:
            if progress:
                progress("doc", path.name)
            run.records.append(self.ingest_file(
                path, "upload", source_name, str(path),
                category_hint, run.run_id))
        if not expanded:
            run.errors.append("no PDF files found in the given paths")
        return run

    # ------------------------------------------------------------------
    # Route 3 — local / OneDrive folders
    # ------------------------------------------------------------------
    def run_folders(
        self, folders=None, progress: ProgressFn | None = None
    ) -> RunSummary:
        run = RunSummary(run_id=uuid.uuid4().hex[:12])
        folders = folders if folders is not None else self.settings.enabled_folders
        if not folders:
            run.errors.append("no folders configured (see config/sources.yaml)")
        for folder in folders:
            if progress:
                progress("folder", f"{folder.name} — {folder.path}")
            report = scan_folder(folder.name, folder.path, folder.recursive)
            if not report.accessible:
                # URD 7.1 mitigation: surface access failures, do not crash.
                run.errors.append(f"[{folder.name}] {report.error}: {report.path}")
                continue
            run.errors.extend(
                f"[{folder.name}] skipped {Path(p).name}: {why}"
                for p, why in report.skipped)
            if not report.pdf_files:
                run.errors.append(f"[{folder.name}] no readable PDFs in {report.path}")
            for pdf in report.pdf_files:
                if progress:
                    progress("doc", pdf.name)
                run.records.append(self.ingest_file(
                    pdf, "local_folder", folder.name, str(pdf),
                    folder.category_hint, run.run_id))
        return run

    def run_onedrive_shares(
        self, shares=None, progress: ProgressFn | None = None
    ) -> RunSummary:
        run = RunSummary(run_id=uuid.uuid4().hex[:12])
        shares = shares if shares is not None else self.settings.enabled_shares
        if not shares:
            run.errors.append("no OneDrive shares configured")
        for share in shares:
            if progress:
                progress("share", share.name)
            staging = self.settings.staging_dir / f"onedrive-{run.run_id}"
            paths, errors = download_onedrive_share(
                share.share_url, staging, max_files=share.max_files,
                subfolder=share.subfolder, exclude=share.exclude_patterns)
            run.errors.extend(f"[{share.name}] {e}" for e in errors)
            for path in paths:
                if progress:
                    progress("doc", path.name)
                run.records.append(self.ingest_file(
                    path, "onedrive", share.name, share.share_url,
                    share.category_hint, run.run_id, move=True))
            self._clean_staging(staging)
        return run

    def run_all(self, progress: ProgressFn | None = None) -> RunSummary:
        combined = RunSummary(run_id=uuid.uuid4().hex[:12])
        runners = [self.run_sites, self.run_folders]
        # Route 3b stays available but is only run when shares are actually
        # registered — with none configured it is a no-op, not a warning.
        if self.settings.enabled_shares:
            runners.append(self.run_onedrive_shares)
        for runner in runners:
            part = runner(progress=progress)
            combined.records.extend(part.records)
            combined.errors.extend(part.errors)
        return combined

    def close(self) -> None:
        self.catalog.close()
