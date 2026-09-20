"""Inventory — the complete register of what each source publishes.

Scraping used to mean "download N PDFs". But "what does OJK publish at all,
and what is the legal status of each item?" needs *every* record, whether or
not its document has been fetched. So discovery and download are separate:

  discover   listing pass  → one row per regulation (JSON / 10-row pages; cheap)
             detail pass   → every field the source publishes (1 page per row;
                             resumable — an interrupted run continues where it
                             stopped instead of starting over)
             reconcile     → match ojk.go.id rows to JDIH by type|number|year so
                             they inherit JDIH's authoritative legal status
  harvest    download      → documents for chosen inventory rows, into the KB
                             (see ``IngestPipeline.harvest_inventory``)

Each source runs in its own thread with its own HTTP session. Sources sharing
a host share a thread, so the per-host politeness delay still holds.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from hero.config import Settings, SiteSource
from hero.ingest import jdih, sharepoint
from hero.ingest.web import WebScraper, discover_pdf_links, normalize_url
from hero.kb.catalog import Catalog

log = logging.getLogger(__name__)

SOURCE_JDIH = "jdih-ojk"
SOURCE_REGULASI = "ojk-regulasi"
SOURCE_RANCANGAN = "ojk-rancangan"
SOURCE_LABELS = {
    SOURCE_JDIH: "JDIH OJK",
    SOURCE_REGULASI: "OJK · Regulasi",
    SOURCE_RANCANGAN: "OJK · Rancangan Regulasi",
    "unggah-manual": "Unggah manual",
    "folder-lokal": "Folder lokal",
    "onedrive": "OneDrive",
}

JDIH_JENIS_ABBREV = {
    "01": "UU", "02": "PERPPU", "03": "PERPRES", "04": "PP", "05": "PERMEN",
    "06": "POJK", "07": "PBI", "08": "BAPEPAM-LK", "09": "SEOJK", "10": "SEBI",
    "11": "SK-DIR", "12": "KEP-BAPEPAM", "13": "SE-BAPEPAM", "14": "LAINNYA",
}
JENIS_LABEL_ABBREV = {
    "undang-undang": "UU", "peraturan ojk": "POJK", "surat edaran ojk": "SEOJK",
    "peraturan adk": "PADK", "peraturan pemerintah": "PP",
    "peraturan/keputusan mentri": "PERMEN", "peraturan/keputusan menteri": "PERMEN",
    "klasifikasi bapepam": "BAPEPAM-LK", "peraturan presiden": "PERPRES",
    "perppu": "PERPPU", "peraturan bi": "PBI", "surat edaran bi": "SEBI",
}

_NUMBER_IN_TEXT = re.compile(
    r"Nomor\s+([0-9]+[A-Za-z]?(?:\s*/\s*[A-Za-z0-9.\-]+)*\s*/\s*(?:19|20)\d{2}"
    r"|[0-9]+[A-Za-z]?\s+Tahun\s+(?:19|20)\d{2})", re.IGNORECASE)
_ABBREV_IN_NUMBER = re.compile(r"/\s*([A-Za-z]{2,6})\s*[./]")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_LEAD_INT = re.compile(r"\s*(\d+)")
_DMY = re.compile(r"^\s*(\d{1,2})-(\d{1,2})-(\d{4})\s*$")
_DRAFT_PREFIX = re.compile(r"^(R(?:POJK|SEOJK|PADK|PP|UU))\b", re.IGNORECASE)
_DRAFT_TEXT = [
    (re.compile(r"rancangan\s+peraturan\s+anggota\s+dewan\s+komisioner", re.I), "RPADK"),
    (re.compile(r"rancangan\s+surat\s+edaran", re.I), "RSEOJK"),
    (re.compile(r"rancangan\s+peraturan\s+(?:otoritas\s+jasa\s+keuangan|ojk)", re.I), "RPOJK"),
]

ProgressFn = Callable[[str, str], None]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------
def number_from_text(text: str | None) -> str | None:
    """"... Nomor 9/POJK.04/2015 tentang ..." → "9/POJK.04/2015"."""
    m = _NUMBER_IN_TEXT.search(text or "")
    return re.sub(r"\s*/\s*", "/", m.group(1)).strip() if m else None


def abbrev_from_number(number: str | None) -> str | None:
    """"9/POJK.04/2015" → "POJK"; "2/11/PBI/2000" → "PBI"."""
    if not number or "/" not in number:
        return None
    m = _ABBREV_IN_NUMBER.search(number)
    return m.group(1).upper() if m else None


def year_from_number(number: str | None) -> int | None:
    years = _YEAR.findall(number or "")
    return int(years[-1]) if years else None


def dmy_to_iso(value: str | None) -> str | None:
    """JDIH prints dates dd-mm-yyyy: "25-06-2015" → "2015-06-25"."""
    m = _DMY.match(value or "")
    if not m:
        return None
    day, month, year = (int(g) for g in m.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def reg_key(doc_type: str | None, number: str | None, year: int | None) -> str | None:
    """Cross-source identity: "POJK|9|2015".

    The same regulation is written "9/POJK.04/2015" on one site and
    "Nomor 9" + "Peraturan OJK" + 2015 on another; type, leading number and
    year are the parts both agree on.
    """
    if not (doc_type and number and year):
        return None
    m = _LEAD_INT.match(number)
    return f"{doc_type.upper()}|{int(m.group(1))}|{year}" if m else None


def draft_type(url: str, *texts: str | None) -> str | None:
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    m = _DRAFT_PREFIX.match(slug)
    if m:
        return m.group(1).upper()
    blob = " ".join(t for t in texts if t)
    for pattern, code in _DRAFT_TEXT:
        if pattern.search(blob):
            return code
    return None


def source_key_for_site(site: SiteSource) -> str:
    adapter = site.resolved_adapter()
    if adapter == "jdih_ojk":
        return SOURCE_JDIH
    if adapter == "ojk_sharepoint":
        return (SOURCE_RANCANGAN if sharepoint.detect_listing_kind(site.url) == "rancangan"
                else SOURCE_REGULASI)
    host = (urlparse(site.url).netloc or "").lower().removeprefix("www.")
    return _slug(host) or "lainnya"


def source_key_for_ref(source_type: str | None, source_ref: str | None) -> str:
    """Derive the source folder for a document from where it came from."""
    ref = (source_ref or "").lower()
    if "jdih.ojk.go.id" in ref:
        return SOURCE_JDIH
    if "rancangan-regulasi" in ref:
        return SOURCE_RANCANGAN
    if "ojk.go.id" in ref:
        return SOURCE_REGULASI
    if source_type == "upload":
        return "unggah-manual"
    if source_type == "local_folder":
        return "folder-lokal"
    if source_type == "onedrive":
        return "onedrive"
    host = (urlparse(source_ref or "").netloc or "").lower().removeprefix("www.")
    return _slug(host) or "lainnya"


# --------------------------------------------------------------------------
# Record builders — listing rows and detail pages → inventory records
# --------------------------------------------------------------------------
def record_from_jdih(entry: jdih.JdihEntry) -> dict[str, Any]:
    number = number_from_text(entry.title) or entry.number
    doc_type = abbrev_from_number(number) or JDIH_JENIS_ABBREV.get(entry.jenis or "")
    year = year_from_number(number) if number and not number.isdigit() else None
    return {
        "record_key": entry.detail_url, "source": SOURCE_JDIH,
        "title": entry.title, "number": number, "doc_type": doc_type,
        "jenis": entry.jenis_label, "sektor": entry.sektor_label,
        "category": entry.category, "year": year,
        "status": entry.normalised_status(), "status_label": entry.status,
        "status_source": "jdih" if entry.status else None,
        "reg_key": reg_key(doc_type, number, year),
        "detail_url": entry.detail_url, "listed_at": _now(),
        "fields": {
            "Judul": entry.title, "Nomor Peraturan": entry.number,
            "Jenis/Bentuk Peraturan": entry.jenis_label, "Sektor": entry.sektor_label,
            "Status Peraturan": entry.status,
            "Kode Sektor JDIH": entry.sektor, "Kode Jenis JDIH": entry.jenis,
        },
    }


def apply_jdih_detail(existing: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    f = dict(detail.get("fields") or {})
    if detail.get("riwayat"):
        f["Riwayat Peraturan"] = detail["riwayat"]
    iso = dmy_to_iso(f.get("Tanggal Penetapan"))
    if iso:
        f["Tanggal Penetapan (ISO)"] = iso
    iso_u = dmy_to_iso(f.get("Tanggal Pengundangan"))
    if iso_u:
        f["Tanggal Pengundangan (ISO)"] = iso_u

    number = existing.get("number")
    doc_type = (f.get("Singkatan Jenis/Bentuk Peraturan") or "").strip().upper() \
        or existing.get("doc_type")
    year = year_from_number(number) if number and not str(number).isdigit() else None
    year = year or (int(iso[:4]) if iso else existing.get("year"))

    status_label = f.get("Status Peraturan") or existing.get("status_label")
    status = jdih.JdihEntry(detail_url="", title="", status=status_label).normalised_status()

    atts: list[dict] = []
    for doc in detail.get("documents") or []:
        name = doc.get("name") or doc.get("label") or "Dokumen"
        kind = "utama" if doc.get("url") == detail.get("document_url") \
            else sharepoint.attachment_kind(f"{doc.get('label') or ''} {name}")
        atts.append({"name": name, "url": doc["url"], "kind": kind,
                     "label": doc.get("label") or None,
                     "ext": name.rsplit(".", 1)[-1].lower() if "." in name else "pdf"})
    if not atts and detail.get("document_url"):
        name = detail.get("document_name") or "Dokumen"
        atts.append({"name": name, "url": detail["document_url"], "kind": "utama",
                     "ext": name.rsplit(".", 1)[-1].lower() if "." in name else "pdf"})
    atts += [{"name": x["title"], "url": x["url"], "kind": "landasan", "ext": ""}
             for x in detail.get("landasan") or []]
    atts += [{"name": x["title"], "url": x["url"], "kind": "riwayat", "ext": ""}
             for x in detail.get("riwayat_links") or []]

    return {
        "record_key": existing["record_key"], "fields": f, "attachments": atts,
        "doc_type": doc_type or None, "year": year,
        "status": status if status != "unknown" else existing.get("status"),
        "status_label": status_label, "status_source": "jdih" if status_label else None,
        "reg_key": reg_key(doc_type, number, year),
        "document_url": detail.get("document_url"),
        "document_name": detail.get("document_name"),
        "enriched_at": _now(), "enrich_error": None,
    }


def record_from_sharepoint_row(row: sharepoint.ListingRow, source: str) -> dict[str, Any]:
    if source == SOURCE_RANCANGAN:
        doc_type = draft_type(row.url, row.title, row.description)
        return {
            "record_key": row.url, "source": source, "title": row.title,
            "doc_type": doc_type, "jenis": "Rancangan Regulasi",
            "status": "rancangan", "status_label": "Rancangan (dibuka untuk tanggapan)",
            "status_source": "sumber", "detail_url": row.url, "listed_at": _now(),
            "fields": {"Deskripsi": row.description, "Halaman Daftar": row.page},
        }
    number = row.number
    doc_type = abbrev_from_number(number) or JENIS_LABEL_ABBREV.get((row.jenis or "").lower())
    year = year_from_number(number)
    if year is None and row.tahun and row.tahun.isdigit():
        year = int(row.tahun)
    return {
        "record_key": row.url, "source": source, "title": row.title,
        "number": number, "doc_type": doc_type, "jenis": row.jenis,
        "sektor": row.sektor, "category": sharepoint.sektor_category(row.sektor),
        "year": year, "status": "unknown", "reg_key": reg_key(doc_type, number, year),
        "detail_url": row.url, "listed_at": _now(),
        "fields": {"Nomor Regulasi": number, "Jenis Regulasi": row.jenis,
                   "Sektor": row.sektor, "Tahun (daftar)": row.tahun,
                   "Halaman Daftar": row.page},
    }


def apply_sharepoint_detail(existing: dict[str, Any], detail: dict[str, Any],
                            source: str) -> dict[str, Any]:
    f = dict(detail.get("fields") or {})
    if detail.get("description"):
        f["Uraian"] = detail["description"]
    atts = detail.get("attachments") or []
    primary = sharepoint.primary_attachment(atts)
    upd: dict[str, Any] = {
        "record_key": existing["record_key"], "fields": f, "attachments": atts,
        "document_url": primary["url"] if primary else None,
        "document_name": primary["name"] if primary else None,
        "enriched_at": _now(), "enrich_error": None,
    }
    if detail.get("title"):
        upd["title"] = detail["title"]

    if source == SOURCE_RANCANGAN:
        iso = sharepoint.us_date_to_iso(f.get("Tanggal"))
        if iso:
            f["Tanggal (ISO)"] = iso
            upd["year"] = int(iso[:4])
        upd["category"] = sharepoint.sektor_category(f.get("Kategori")) or existing.get("category")
        upd["sektor"] = f.get("Kategori") or existing.get("sektor")
        upd["doc_type"] = existing.get("doc_type") or draft_type(
            existing["record_key"], detail.get("title"), detail.get("description"),
            primary["name"] if primary else None)
        return upd

    number = f.get("Nomor Regulasi") or existing.get("number")
    doc_type = abbrev_from_number(number) \
        or JENIS_LABEL_ABBREV.get((f.get("Jenis Regulasi") or "").lower()) \
        or existing.get("doc_type")
    year = year_from_number(number) or existing.get("year")
    iso = sharepoint.us_date_to_iso(f.get("Tanggal Berlaku"))
    if iso:
        f["Tanggal Berlaku (ISO)"] = iso
    upd.update({
        "number": number, "doc_type": doc_type, "year": year,
        "jenis": f.get("Jenis Regulasi") or existing.get("jenis"),
        "sektor": f.get("Sektor") or existing.get("sektor"),
        "category": sharepoint.sektor_category(f.get("Sektor")) or existing.get("category"),
        "reg_key": reg_key(doc_type, number, year),
    })
    return upd


def record_from_link(url: str, anchor: str, found_on: str, source: str) -> dict[str, Any]:
    return {
        "record_key": url, "source": source, "title": anchor or url.rsplit("/", 1)[-1],
        "status": "unknown", "detail_url": found_on, "document_url": url,
        "document_name": url.rsplit("/", 1)[-1], "listed_at": _now(),
        "enriched_at": _now(), "fields": {"Teks Tautan": anchor, "Ditemukan di": found_on},
    }


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
@dataclass
class DiscoverReport:
    site: str
    source: str
    listed: int = 0
    new: int = 0
    pages: int = 0
    enriched: int = 0
    enrich_failed: int = 0
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0


def _list_jdih(site, scraper, cat, rep, say) -> None:
    pairs, errors = jdih.resolve_pairs(site, scraper)
    rep.errors.extend(errors)
    for sektor, jenis_code in pairs:
        entries, error = jdih.fetch_listing(scraper, sektor, jenis_code)
        rep.pages += 1
        if error:
            rep.errors.append(error)
            continue
        for entry in entries:
            rep.listed += 1
            rep.new += cat.upsert_inventory(record_from_jdih(entry))
        if entries:
            say("list", f"{rep.source}: sektor {sektor} × jenis {jenis_code} → "
                        f"{len(entries)} (total {rep.listed})")


def _list_sharepoint(site, scraper, cat, rep, say, full, max_pages) -> None:
    source = rep.source
    kind = "rancangan" if source == SOURCE_RANCANGAN else "regulasi"
    quiet_pages = 0
    for page, rows in sharepoint.iter_listing(scraper, site.url, kind, max_pages, rep.errors):
        rep.pages = page
        new_here = 0
        for row in rows:
            rep.listed += 1
            new_here += cat.upsert_inventory(record_from_sharepoint_row(row, source))
        rep.new += new_here
        if page == 1 or page % 10 == 0:
            say("list", f"{source}: halaman {page} (total {rep.listed}, baru {rep.new})")
        # Incremental mode: listings are newest-first, so two pages in a row
        # with nothing new means everything older is already known.
        quiet_pages = quiet_pages + 1 if new_here == 0 else 0
        if not full and quiet_pages >= 2:
            say("list", f"{source}: tidak ada entri baru — berhenti di halaman {page}")
            return


def _list_generic(site, scraper, cat, rep, say) -> None:
    queue, seen_pages = [normalize_url(site.url)], set()
    while queue and rep.pages < max(site.max_pages, 1):
        url = queue.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        html, _ = scraper.get_html(url)
        rep.pages += 1
        if html is None:
            rep.errors.append(f"could not read page: {url}")
            continue
        pdfs, follow, _ambiguous = discover_pdf_links(html, url, site)
        for link in pdfs:
            rep.listed += 1
            rep.new += cat.upsert_inventory(
                record_from_link(link.url, link.anchor_text, url, rep.source))
        queue.extend(u for u in follow if u not in seen_pages)


def _enrich(site, scraper, cat, rep, say, refresh, max_details) -> None:
    rows = cat.list_inventory(rep.source, pending_enrich=not refresh, limit=max_details)
    total = len(rows)
    if total:
        say("detail", f"{rep.source}: {total} halaman detail akan dibaca")
    for i, row in enumerate(rows, start=1):
        existing = dict(row)
        html, _ = scraper.get_html(row["detail_url"])
        if html is None:
            rep.enrich_failed += 1
            cat.upsert_inventory({"record_key": row["record_key"],
                                  "enrich_error": "detail page unreadable"})
            continue
        try:
            if rep.source == SOURCE_JDIH:
                upd = apply_jdih_detail(existing, jdih.parse_detail(html))
            else:
                upd = apply_sharepoint_detail(
                    existing, sharepoint.parse_detail(html, row["detail_url"]), rep.source)
        except Exception as exc:  # noqa: BLE001 - one odd page must not stop the run
            rep.enrich_failed += 1
            cat.upsert_inventory({"record_key": row["record_key"],
                                  "enrich_error": f"{type(exc).__name__}: {exc}"})
            continue
        cat.upsert_inventory(upd)
        rep.enriched += 1
        if i % 50 == 0 or i == total:
            say("detail", f"{rep.source}: {i}/{total} detail")


def discover_site(
    site: SiteSource, settings: Settings, *, enrich: bool = True,
    refresh: bool = False, full: bool | None = None,
    max_pages: int | None = None, max_details: int | None = None,
    progress: ProgressFn | None = None,
) -> DiscoverReport:
    """Build (or update) the inventory for one site."""
    started = time.perf_counter()
    say = progress or (lambda kind, msg: None)
    source = source_key_for_site(site)
    rep = DiscoverReport(site=site.name, source=source)
    adapter = site.resolved_adapter()
    with WebScraper(settings.scraper) as scraper, Catalog(settings.catalog_db) as cat:
        already = len(cat.list_inventory(source, limit=1))
        do_full = refresh or (full if full is not None else already == 0)
        try:
            if adapter == "jdih_ojk":
                _list_jdih(site, scraper, cat, rep, say)
            elif adapter == "ojk_sharepoint":
                _list_sharepoint(site, scraper, cat, rep, say, do_full, max_pages)
            else:
                _list_generic(site, scraper, cat, rep, say)
            if enrich and adapter in ("jdih_ojk", "ojk_sharepoint"):
                _enrich(site, scraper, cat, rep, say, refresh, max_details)
        except Exception as exc:  # noqa: BLE001 - report per site, never crash all
            log.exception("discovery failed for %s", site.name)
            rep.errors.append(f"{type(exc).__name__}: {exc}")
    rep.seconds = round(time.perf_counter() - started, 1)
    return rep


def reconcile_status(settings: Settings) -> dict[str, int]:
    """Give ojk.go.id rows the legal status JDIH publishes for the same regulation.

    ojk.go.id never prints whether a regulation is still in force; JDIH does.
    Matching on TYPE|number|year lets one source's facts fill the other's gap.
    """
    matched = unmatched = 0
    with Catalog(settings.catalog_db) as cat:
        for row in cat.list_inventory(SOURCE_REGULASI):
            if row["status_source"] == "jdih":
                matched += 1
                continue
            hit = cat.find_inventory_by_regkey(row["reg_key"], SOURCE_JDIH) \
                if row["reg_key"] else None
            if hit is None or not hit["status"] or hit["status"] == "unknown":
                unmatched += 1
                continue
            cat.upsert_inventory({
                "record_key": row["record_key"], "status": hit["status"],
                "status_label": hit["status_label"], "status_source": "jdih",
                "fields": {"Status menurut JDIH": hit["status_label"],
                           "Tautan JDIH": hit["detail_url"]},
            })
            matched += 1
    return {"matched": matched, "unmatched": unmatched}


def discover_all(
    settings: Settings, sites: Iterable[SiteSource] | None = None, *,
    parallel: bool = True, progress: ProgressFn | None = None, **kwargs: Any,
) -> list[DiscoverReport]:
    """Discover every site; sites on different hosts run concurrently."""
    sites = list(sites if sites is not None else settings.enabled_sites)
    groups: dict[str, list[SiteSource]] = {}
    for site in sites:
        groups.setdefault((urlparse(site.url).netloc or "").lower(), []).append(site)

    def run_group(group: list[SiteSource]) -> list[DiscoverReport]:
        return [discover_site(s, settings, progress=progress, **kwargs) for s in group]

    reports: list[DiscoverReport] = []
    if parallel and len(groups) > 1:
        with ThreadPoolExecutor(max_workers=len(groups)) as pool:
            for result in pool.map(run_group, groups.values()):
                reports.extend(result)
    else:
        for group in groups.values():
            reports.extend(run_group(group))
    return reports
