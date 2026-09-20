"""Adapter for JDIH OJK (jdih.ojk.go.id) — the official legal-documentation
portal (URD 3.2, jalur 1).

Why this needs its own adapter rather than the generic HTML crawler:

  * The listing page renders **no document links at all**. The table is a
    DataTables grid filled by AJAX, so a generic link scraper sees zero PDFs.
    The rows come from ``/Web/ViewPeraturan/ListDataPeraturan`` as JSON.
  * The PDF itself sits behind ``/Web/ViewPeraturan/DownloadDokumen/{guid}``
    — no ``.pdf`` extension, served as ``application/octet-stream`` with the
    real filename only in the ``Content-Disposition`` header.
  * The listing JSON hands over metadata that is *more* authoritative than
    anything parsed out of the PDF: the regulation's legal status
    (Berlaku / Dicabut / Diubah), its sector, and its type. That directly
    feeds URD 3.3 ("identifikasi metadata ... status berlaku/dicabut") and
    URD 3.4 ("pengecekan status peraturan yang dirujuk").

Flow: listing JSON → detail page → DownloadDokumen → PDF.

``sektor`` and ``jenisPeraturan`` are the two dynamic axes of the portal;
both are enumerable from the sidebar of any Index page, so a site entry can
ask for specific pairs or for the whole matrix.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from hero.config import SiteSource
    from hero.ingest.web import SiteReport, WebScraper

log = logging.getLogger(__name__)

JDIH_HOST = "jdih.ojk.go.id"
BASE_URL = "https://jdih.ojk.go.id"
LIST_ENDPOINT = "https://jdih.ojk.go.id/Web/ViewPeraturan/ListDataPeraturan"
INDEX_PATH = "/Web/ViewPeraturan/Index"

# Labels confirmed against the live portal (2026-09-15). Used for logging and
# for the knowledge-base category hint; unknown codes still work, they just
# show up unlabelled.
SEKTOR_LABELS = {
    "01": "Perbankan",
    "02": "Pasar Modal, Keuangan Derivatif, dan Bursa Karbon",
    "03": "IKNB (Sebelum UU Nomor 4 Tahun 2023)",
    "04": "Perilaku PUJK, Edukasi dan Pelindungan Konsumen",
    "05": "Manajemen Strategis",
    "06": "Lainnya",
    "07": "Kebijakan Strategis",
    "08": "Perasuransian, Penjaminan, dan Dana Pensiun",
    "09": "Lembaga Pembiayaan, Modal Ventura, LKM, dan LJK Lainnya",
    "10": "Inovasi Teknologi Sektor Keuangan, Aset Keuangan Digital dan Aset Kripto",
}
JENIS_LABELS = {
    "01": "Undang-Undang",
    "02": "PERPPU",
    "03": "Peraturan Presiden",
    "04": "Peraturan Pemerintah",
    "05": "Peraturan Menteri",
    "06": "Peraturan OJK",
    "07": "Peraturan BI",
    "08": "Peraturan Bapepam dan LK",
    "09": "Surat Edaran OJK / PADK OJK",
    "10": "Surat Edaran BI",
    "11": "SK Dir",
    "12": "Keputusan Ketua Bapepam",
    "13": "SE Ketua Bapepam",
    "14": "Peraturan Lain",
}

# Map the portal's own sector naming onto HERO's knowledge-base categories.
SEKTOR_CATEGORY = {
    "01": "perbankan",
    "02": "pasar-modal",
    "03": "iknb",
    "04": "perlindungan-konsumen",
    "05": "tata-kelola",
    "06": "lain-lain",
    "07": "kelembagaan",
    "08": "iknb",
    "09": "iknb",
    # ITSK / digital assets — squarely the MVP pilot PoV (Unit Bisnis IT).
    "10": "teknologi-informasi",
}

_TAG_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r"""href=['"]([^'"]+)['"]""", re.IGNORECASE)


@dataclass
class JdihEntry:
    """One row of the JDIH listing grid."""

    detail_url: str
    title: str
    number: str | None = None
    sektor: str | None = None
    sektor_label: str | None = None
    jenis: str | None = None
    jenis_label: str | None = None
    status: str | None = None          # Berlaku | Tidak Berlaku | Dicabut ...
    document_url: str | None = None    # filled in after visiting the detail page

    @property
    def category(self) -> str | None:
        return SEKTOR_CATEGORY.get(self.sektor or "")

    def normalised_status(self) -> str:
        """Map the portal's wording onto HERO's status vocabulary.

        The leading word decides. JDIH writes compound labels like
        "Berlaku (Dicabut Sebagian)" — a regulation that IS still in force
        with some articles revoked. A naive substring check for "cabut"
        would wrongly file it as revoked and make harmonisation (URD 3.4)
        skip a regulation that still binds. Observed labels: "Berlaku",
        "Tidak Berlaku", "Berlaku (Dicabut Sebagian)",
        "Berlaku (Perubahan) (Diubah)", "Berlaku (Perubahan) (Mengubah)".
        """
        low = " ".join((self.status or "").strip().lower().split())
        if not low:
            return "unknown"
        if low.startswith("tidak berlaku") or low.startswith("dicabut"):
            return "dicabut"
        if low.startswith("berlaku"):
            # Still in force; note the amendment nuance when present.
            if "diubah" in low:
                return "diubah"
            return "berlaku"
        if "tidak berlaku" in low:
            return "dicabut"
        return "unknown"


def is_jdih_url(url: str) -> bool:
    return JDIH_HOST in (urlparse(url).netloc or "").lower()


def parse_index_url(url: str) -> tuple[str | None, str | None]:
    """Pull (sektor, jenisPeraturan) out of an Index URL, if present."""
    qs = parse_qs(urlparse(url).query)
    sektor = (qs.get("sektor") or [None])[0]
    jenis = (qs.get("jenisPeraturan") or [None])[0]
    return sektor, jenis


def discover_matrix(html: str) -> list[tuple[str, str]]:
    """Enumerate every (sektor, jenis) pair linked from an Index page sidebar.

    This is how "sektor dan jenisPeraturan dinamis" is satisfied without
    hard-coding: the portal itself publishes the valid combinations.
    """
    soup = BeautifulSoup(html, "lxml")
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for a in soup.find_all("a", href=True):
        if INDEX_PATH not in a["href"]:
            continue
        sektor, jenis = parse_index_url(a["href"])
        if sektor and jenis and (sektor, jenis) not in seen:
            seen.add((sektor, jenis))
            pairs.append((sektor, jenis))
    return pairs


def parse_listing_rows(payload: dict, sektor: str | None = None,
                       jenis: str | None = None) -> list[JdihEntry]:
    """Turn the DataTables JSON payload into typed entries.

    Row layout (confirmed live): [0] anchor HTML with the detail URL and
    title, [1] number, [2] sector name, [5] regulation type, [7] status.
    Indices are read defensively — the portal has changed column counts
    before, and a shifted column must not take the whole ingest down.
    """
    rows = payload.get("aaData") or []
    entries: list[JdihEntry] = []
    for row in rows:
        if not row:
            continue
        anchor_html = str(row[0]) if len(row) > 0 else ""
        m = _HREF_RE.search(anchor_html)
        if not m:
            continue
        detail_url = m.group(1).strip()
        # The portal emits http:// links even when served over https.
        if detail_url.startswith("http://"):
            detail_url = "https://" + detail_url[len("http://"):]
        title = _TAG_RE.sub("", anchor_html).strip()

        def cell(i: int) -> str | None:
            if len(row) > i and row[i] not in (None, ""):
                return str(row[i]).strip()
            return None

        row_sektor = sektor
        row_jenis = jenis
        if not row_sektor or not row_jenis:
            # Detail URLs end in /{sektor}/{jenis}.
            parts = [p for p in urlparse(detail_url).path.split("/") if p]
            if len(parts) >= 2:
                row_sektor = row_sektor or parts[-2]
                row_jenis = row_jenis or parts[-1]

        entries.append(JdihEntry(
            detail_url=detail_url,
            title=title,
            number=cell(1),
            sektor=row_sektor,
            sektor_label=cell(2) or SEKTOR_LABELS.get(row_sektor or ""),
            jenis=row_jenis,
            jenis_label=cell(5) or JENIS_LABELS.get(row_jenis or ""),
            status=cell(7),
        ))
    return entries


def fetch_listing(
    scraper: "WebScraper", sektor: str, jenis: str, language: str = "",
) -> tuple[list[JdihEntry], str | None]:
    """Fetch one (sektor, jenis) slice of the register. Returns (entries, error)."""
    url = (f"{LIST_ENDPOINT}?sektor={sektor}&jenisPeraturan={jenis}"
           f"&sLanguage={language}")
    try:
        resp = scraper.request_json(url)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return [], f"listing request failed (sektor={sektor}, jenis={jenis}): {exc}"
    if resp is None:
        return [], f"listing unavailable (sektor={sektor}, jenis={jenis})"
    try:
        entries = parse_listing_rows(resp, sektor, jenis)
    except Exception as exc:  # noqa: BLE001 - schema drift must not be fatal
        return [], (f"could not parse listing JSON (sektor={sektor}, "
                    f"jenis={jenis}): {exc}")
    return entries, None


def find_document_link(html: str, base_url: str = BASE_URL) -> str | None:
    """Locate the DownloadDokumen link on a regulation's detail page."""
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        if "DownloadDokumen" in a["href"]:
            return urljoin(base_url, a["href"])
    return None


def parse_detail(html: str, base_url: str = BASE_URL) -> dict:
    """Extract every field a JDIH detail page publishes.

    The page is a label / ":" / value table (Tipe Dokumen, Judul, T.E.U
    Badan, Nomor Peraturan, Tanggal Penetapan, Tanggal Pengundangan, Sumber
    LN/TLN, Status Peraturan, Subjek, Bidang Hukum, Landasan Hukum, ...),
    followed by a "Riwayat Peraturan" block (what this regulation revokes or
    amends, and what revoked or amended it) and the document itself.
    Labels are kept verbatim so they can serve directly as export columns.
    """
    soup = BeautifulSoup(html, "lxml")
    fields: dict[str, str] = {}
    documents: list[dict] = []
    for tr in soup.find_all("tr"):
        # Labels are <th>, values <td> — read both, in document order.
        cells = [" ".join(td.get_text(" ", strip=True).split())
                 for td in tr.find_all(["th", "td"])]
        download = tr.find("a", href=re.compile("downloaddokumen", re.I))
        if download is not None:
            # A document row — "Peraturan : 2026padk001.pdf [Unduh]", with
            # siblings "Abstrak : …" and "FAQ : …". Keep label and file name;
            # drop the page's "klik pada nama file…" instructions.
            preview = tr.find("a", href=re.compile("previewdokumen", re.I))
            label = cells[0] if cells and cells[0] != ":" else ""
            name = " ".join(preview.get_text(" ", strip=True).split()) if preview else ""
            documents.append({
                "label": label, "name": name or None,
                "url": urljoin(base_url, download["href"]),
                "preview_url": urljoin(base_url, preview["href"]) if preview else None,
            })
            if label:
                fields[label] = name
            continue
        if len(cells) >= 3 and cells[1] == ":" and cells[0]:
            fields[cells[0]] = cells[2]

    riwayat = None
    heading = soup.find("h2", string=lambda s: bool(s) and "Riwayat" in s)
    if heading is not None:
        box = heading.find_next_sibling("div", class_="box-holder")
        if box is not None:
            text = " ".join(box.get_text(" ", strip=True).split())
            riwayat = text or None

    landasan, riwayat_links = [], []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = " ".join(a.get_text(" ", strip=True).split())
        low = href.lower()
        if "downloadfilelandasan" in low:
            landasan.append({"title": label, "url": urljoin(base_url, href)})
        elif "downloadfileriwayat" in low:
            riwayat_links.append({"title": label, "url": urljoin(base_url, href)})

    if not documents:
        # A layout without the document table: fall back to the first links.
        dl = soup.find("a", href=re.compile("downloaddokumen", re.I))
        pv = soup.find("a", href=re.compile("previewdokumen", re.I))
        if dl is not None:
            documents.append({
                "label": "", "url": urljoin(base_url, dl["href"]),
                "name": " ".join(pv.get_text(" ", strip=True).split()) or None if pv else None,
                "preview_url": urljoin(base_url, pv["href"]) if pv else None,
            })

    primary = primary_document(documents)
    return {
        "fields": fields,
        "riwayat": riwayat,
        "landasan": landasan,
        "riwayat_links": riwayat_links,
        "documents": documents,
        "document_url": primary["url"] if primary else None,
        "preview_url": primary.get("preview_url") if primary else None,
        "document_name": primary.get("name") if primary else None,
    }


_COMPANION_DOCS = ("abstrak", "abstract", "faq", "lampiran", "terjemahan",
                   "english", "ringkasan", "matriks")


def primary_document(documents: list[dict]) -> dict | None:
    """The regulation itself, not its Abstrak / FAQ / annex.

    Judged on the row label first ("Peraturan", "Abstrak", "FAQ"), then the
    file name — "2026abspadk001.pdf" is the abstract of "2026padk001.pdf".
    """
    if not documents:
        return None
    for doc in documents:
        label = (doc.get("label") or "").lower()
        if label and not any(c in label for c in _COMPANION_DOCS):
            return doc
    for doc in documents:
        blob = f"{doc.get('label') or ''} {doc.get('name') or ''}".lower()
        if not any(c in blob for c in _COMPANION_DOCS):
            return doc
    return documents[0]


def resolve_pairs(site: "SiteSource", scraper: "WebScraper") -> tuple[
        list[tuple[str, str]], list[str]]:
    """Work out which (sektor, jenis) pairs this site entry should harvest.

    Precedence: explicit lists in the config → the pair embedded in the
    configured URL → the whole matrix advertised by the portal sidebar.
    """
    errors: list[str] = []
    wildcard = {"*", "all", "semua"}
    raw_sektor = [str(s).strip() for s in (site.sektor or [])]
    raw_jenis = [str(j).strip() for j in (site.jenis_peraturan or [])]
    # "*" means "every value on this axis" — and, unlike an empty list, it
    # stops the pair embedded in the URL from narrowing the harvest.
    all_sektor = any(s.lower() in wildcard for s in raw_sektor)
    all_jenis = any(j.lower() in wildcard for j in raw_jenis)
    sektors = [] if all_sektor else [s.zfill(2) for s in raw_sektor if s]
    jenis_list = [] if all_jenis else [j.zfill(2) for j in raw_jenis if j]

    url_sektor, url_jenis = parse_index_url(site.url)
    if not sektors and url_sektor and not all_sektor:
        sektors = [url_sektor]
    if not jenis_list and url_jenis and not all_jenis:
        jenis_list = [url_jenis]

    if sektors and jenis_list:
        return [(s, j) for s in sektors for j in jenis_list], errors

    # Fall back to whatever the portal itself lists.
    html, _ = scraper.get_html(site.url)
    if html is None:
        errors.append(f"could not read JDIH index page: {site.url}")
        return [], errors
    matrix = discover_matrix(html)
    if not matrix:
        errors.append("no sektor/jenisPeraturan pairs found on the index page")
        return [], errors
    if sektors:
        matrix = [(s, j) for s, j in matrix if s in sektors]
    if jenis_list:
        matrix = [(s, j) for s, j in matrix if j in jenis_list]
    return matrix, errors


def harvest_jdih(
    scraper: "WebScraper",
    site: "SiteSource",
    dest_dir: Path,
    limit: int | None = None,
    skip_url: Callable[[str], bool] | None = None,
) -> tuple["SiteReport", dict[str, JdihEntry]]:
    """Harvest PDFs from JDIH OJK. Returns (report, {document_url: entry}).

    The entry map lets the pipeline carry the portal's authoritative metadata
    (status, sector, type) onto the ingested document.
    """
    from hero.ingest.web import PdfLink, SiteReport

    report = SiteReport(site=site.name)
    budget = limit if limit is not None else site.max_documents
    entry_by_url: dict[str, JdihEntry] = {}

    pairs, errors = resolve_pairs(site, scraper)
    report.errors.extend(errors)
    if not pairs:
        return report, entry_by_url

    collected: list[JdihEntry] = []
    for sektor, jenis in pairs:
        if len(collected) >= budget:
            break
        entries, error = fetch_listing(scraper, sektor, jenis)
        report.pages_visited += 1
        if error:
            report.errors.append(error)
            continue
        label = (f"{SEKTOR_LABELS.get(sektor, sektor)} / "
                 f"{JENIS_LABELS.get(jenis, jenis)}")
        if not entries:
            log.debug("JDIH %s: no records", label)
            continue
        log.info("JDIH %s: %d record(s)", label, len(entries))
        collected.extend(entries)

    report.links_found = len(collected)

    fetched = 0
    for entry in collected:
        if fetched >= budget:
            break
        # Cheap check first: the detail URL is stable, so a document already
        # ingested from this portal can be skipped without opening its page.
        if skip_url is not None and skip_url(entry.detail_url):
            report.skipped_known += 1
            continue

        html, _ = scraper.get_html(entry.detail_url)
        report.pages_visited += 1
        if html is None:
            report.errors.append(f"could not read detail page: {entry.detail_url}")
            continue
        doc_url = find_document_link(html, BASE_URL)
        if not doc_url:
            report.errors.append(
                f"no downloadable document on detail page: {entry.title[:80]}")
            continue
        if skip_url is not None and skip_url(doc_url):
            report.skipped_known += 1
            continue

        link = PdfLink(url=doc_url, anchor_text=entry.title,
                       found_on=entry.detail_url)
        result = scraper.download_pdf(link, dest_dir)
        report.fetched.append(result)
        fetched += 1
        if result.ok:
            entry.document_url = doc_url
            entry_by_url[doc_url] = entry

    return report, entry_by_url
