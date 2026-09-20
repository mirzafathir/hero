"""Adapter for OJK's SharePoint listings on ojk.go.id (URD 3.2, jalur 1).

Covers both registered listings:
  * Regulasi           — https://ojk.go.id/id/regulasi/default.aspx
  * Rancangan Regulasi — https://www.ojk.go.id/id/regulasi/otoritas-jasa-keuangan/
                         rancangan-regulasi/Default.aspx

Why a dedicated adapter: each listing shows 10 rows per page and the pager is
an ASP.NET WebForms control. "Page 2" is not a URL — it is a POST back to the
same page carrying the form's hidden state (``__VIEWSTATE`` alone is ~170 KB)
plus ``__EVENTTARGET`` naming the pager button. A link-following crawler can
therefore only ever see page 1: 10 of the 1,570 regulations and 10 of the 650
drafts published (counted live on 2026-09-15). The SharePoint REST endpoints
that would sidestep this (``/_api``, ``listdata.svc``) are closed to anonymous
clients, so replaying the postback is the only route to the full register.

Detail pages carry the structured fields ("Sektor : PVML", "Nomor Regulasi :
45/PADK.06/2025", ...) and the attachments. A regulation usually ships with
companion files (Abstrak, FAQ, Lampiran); a draft usually ships as one ZIP.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Iterator
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from hero.ingest.web import WebScraper

log = logging.getLogger(__name__)

_POSTBACK_RE = re.compile(r"__doPostBack\('([^']+)'")
_TITLE_LINK_RE = re.compile(r"HyperlinkTitle")
_PAGER_RE = re.compile(r"DataPagerArticles")
_US_DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")

DOCUMENT_EXTENSIONS = (".pdf", ".zip", ".docx", ".doc", ".xlsx", ".xls",
                       ".pptx", ".rar")

# Companion files published beside the regulation itself. The first match on
# the filename (or link text) decides the attachment's role.
_ATTACHMENT_KINDS: list[tuple[str, tuple[str, ...]]] = [
    ("abstrak", ("abstrak", "abstract", "ringkasan")),
    ("faq", ("faq", "tanya jawab", "tanya-jawab", "qna", "q&a")),
    ("matriks", ("matriks", "matrix", "tanggapan")),
    ("lampiran", ("lampiran", "annex", "appendix", "juknis", "petunjuk teknis")),
    ("infografis", ("infografis", "infographic", "siaran pers", "press release")),
    ("terjemahan", ("english", "translation", "terjemahan")),
]

# ojk.go.id's own sector naming -> HERO knowledge-base category.
SEKTOR_CATEGORY = {
    "perbankan": "perbankan",
    "pasar modal": "pasar-modal",
    "iknb": "iknb",
    "pvml": "iknb",
    "ppdp": "iknb",
    "syariah": "syariah",
    "epk": "perlindungan-konsumen",
    "pepk": "perlindungan-konsumen",
    "ojk": "kelembagaan",
    "itsk": "teknologi-informasi",
    "iakd": "teknologi-informasi",
}


@dataclass
class ListingRow:
    """One row of a listing page, before its detail page is visited."""

    url: str
    title: str
    page: int
    number: str | None = None
    jenis: str | None = None
    sektor: str | None = None
    tahun: str | None = None
    description: str | None = None


@dataclass
class ListingResult:
    rows: list[ListingRow] = field(default_factory=list)
    pages: int = 0
    errors: list[str] = field(default_factory=list)


def _text(node) -> str:
    # SharePoint's rich-text editor leaves zero-width spaces (U+200B) around
    # edited words; they survive .split() and poison matching and exports.
    if not node:
        return ""
    raw = node.get_text(" ", strip=True).replace("​", "").replace("﻿", "")
    return " ".join(raw.split())


def detect_listing_kind(url: str) -> str:
    return "rancangan" if "rancangan-regulasi" in url.lower() else "regulasi"


def is_ojk_sharepoint_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return host in ("ojk.go.id", "www.ojk.go.id") and "/regulasi/" in url.lower()


def us_date_to_iso(value: str | None) -> str | None:
    """SharePoint renders dates en-US (M/D/YYYY): "7/1/2027" is 1 July.

    Confirmed by values such as "9/14/2026", where 14 cannot be a month.
    """
    if not value:
        return None
    m = _US_DATE_RE.match(value)
    if not m:
        return None
    month, day, year = (int(g) for g in m.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def sektor_category(label: str | None) -> str | None:
    if not label:
        return None
    return SEKTOR_CATEGORY.get(label.strip().lower())


# --------------------------------------------------------------------------
# Listing pages
# --------------------------------------------------------------------------
def form_fields(soup: BeautifulSoup) -> dict[str, str]:
    """Every hidden/text input — the state a WebForms postback must echo."""
    data: dict[str, str] = {}
    for inp in soup.find_all("input", attrs={"name": True}):
        if (inp.get("type") or "text").lower() in ("hidden", "text"):
            data[inp["name"]] = inp.get("value", "")
    return data


def next_page_target(soup: BeautifulSoup) -> tuple[int | None, str | None]:
    """Return (current page number, postback target for the next page).

    The pager shows ten numbered buttons at a time, then "..." to open the
    next block. So: take the button numbered current+1 if it is on screen,
    otherwise the "..." that follows the current page. ``None`` as the target
    means this is the last page.
    """
    pager = soup.find(id=_PAGER_RE)
    if pager is None:
        return None, None
    current_el = pager.find("span", class_="currentPagingButton")
    current = None
    if current_el and current_el.get_text(strip=True).isdigit():
        current = int(current_el.get_text(strip=True))

    if current is not None:
        for a in pager.find_all("a", href=True):
            if a.get_text(strip=True) == str(current + 1):
                m = _POSTBACK_RE.search(a["href"])
                if m:
                    return current, m.group(1)

    past_current = False
    for el in pager.children:
        name = getattr(el, "name", None)
        if name == "span" and "currentPagingButton" in (el.get("class") or []):
            past_current = True
        elif past_current and name == "a" and el.get_text(strip=True) == "...":
            m = _POSTBACK_RE.search(el.get("href", ""))
            if m:
                return current, m.group(1)
    return current, None


def parse_regulasi_rows(soup: BeautifulSoup, base_url: str, page: int) -> list[ListingRow]:
    """Rows look like: [number] [title link + caption "Jenis • Sektor • Tahun"]."""
    rows: list[ListingRow] = []
    for a in soup.find_all("a", id=_TITLE_LINK_RE, href=True):
        tr = a.find_parent("tr")
        cells = tr.find_all("td") if tr else []
        number = _text(cells[0]) if len(cells) >= 2 else None
        caption = tr.find(class_="caption") if tr else None
        parts = [p.strip() for p in _text(caption).split("•")] if caption else []
        parts += [None] * (3 - len(parts))
        rows.append(ListingRow(
            url=urljoin(base_url, a["href"]), title=_text(a), page=page,
            number=number or None, jenis=parts[0] or None,
            sektor=parts[1] or None, tahun=parts[2] or None,
        ))
    return rows


def parse_rancangan_rows(soup: BeautifulSoup, base_url: str, page: int) -> list[ListingRow]:
    """Rows look like: [title link] [description asking for public comment]."""
    rows: list[ListingRow] = []
    for a in soup.find_all("a", id=_TITLE_LINK_RE, href=True):
        tr = a.find_parent("tr")
        cells = tr.find_all("td") if tr else []
        description = _text(cells[1]) if len(cells) >= 2 else None
        rows.append(ListingRow(
            url=urljoin(base_url, a["href"]), title=_text(a), page=page,
            description=description or None,
        ))
    return rows


def iter_listing(
    scraper: "WebScraper", url: str, kind: str | None = None,
    max_pages: int | None = None, errors: list[str] | None = None,
) -> Iterator[tuple[int, list[ListingRow]]]:
    """Walk every page of a listing by replaying the pager postback.

    Yields (page number, rows new on that page). Stops at the last page, at
    ``max_pages``, or when a page returns nothing new (a pager that loops
    back on itself must not spin forever).
    """
    kind = kind or detect_listing_kind(url)
    parse = parse_rancangan_rows if kind == "rancangan" else parse_regulasi_rows
    errors = errors if errors is not None else []

    html, _ = scraper.get_html(url)
    if html is None:
        errors.append(f"could not read listing page: {url}")
        return
    page = 1
    seen: set[str] = set()
    while True:
        soup = BeautifulSoup(html, "lxml")
        rows = [r for r in parse(soup, url, page) if r.url not in seen]
        seen.update(r.url for r in rows)
        yield page, rows
        if not rows or (max_pages and page >= max_pages):
            return
        current, target = next_page_target(soup)
        if not target:
            return
        data = form_fields(soup)
        data.update({"__EVENTTARGET": target, "__EVENTARGUMENT": ""})
        html = scraper.post_html(url, data)
        if html is None:
            errors.append(f"pager postback failed after page {page}: {url}")
            return
        page = (current or page) + 1


def fetch_listing(
    scraper: "WebScraper", url: str, kind: str | None = None,
    max_pages: int | None = None,
) -> ListingResult:
    result = ListingResult()
    for page, rows in iter_listing(scraper, url, kind, max_pages, result.errors):
        result.pages = page
        result.rows.extend(rows)
    return result


# --------------------------------------------------------------------------
# Detail pages
# --------------------------------------------------------------------------
def attachment_kind(name: str) -> str:
    low = name.lower()
    for kind, needles in _ATTACHMENT_KINDS:
        if any(n in low for n in needles):
            return kind
    return "utama"


def parse_detail(html: str, url: str) -> dict:
    """Extract every labelled field and every attachment from a detail page.

    Returns {"title", "fields": {label: value}, "attachments": [...],
    "description"}. Field labels are kept exactly as the site prints them,
    so the export shows the source's own vocabulary as its columns.
    """
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.find("h1", class_="title-in") or soup.find("h1")
    fields: dict[str, str] = {}

    for block in soup.select(".list-regulasi-display"):
        label, sep, value = _text(block).partition(":")
        if sep and label.strip():
            fields[label.strip()] = value.strip()

    kategori = soup.select_one(".kategori-rancangan-regulasi")
    if kategori and _text(kategori):
        fields.setdefault("Kategori", _text(kategori))
    date_el = soup.select_one(".display-date-text")
    if date_el and _text(date_el) and "Tanggal Berlaku" not in fields:
        fields.setdefault("Tanggal", _text(date_el))

    attachments: list[dict] = []
    seen: set[str] = set()
    containers = soup.select(".ms-rtestate-field") or [soup]
    for container in containers:
        for a in container.find_all("a", href=True):
            href = a["href"].strip()
            path = urlparse(href).path.lower()
            if not path.endswith(DOCUMENT_EXTENSIONS):
                continue
            full = urljoin(url, href)
            if full in seen:
                continue
            seen.add(full)
            name = _text(a) or unquote(PurePosixPath(urlparse(full).path).name)
            attachments.append({
                "name": name,
                "url": full,
                "kind": attachment_kind(name),
                "ext": PurePosixPath(path).suffix.lstrip("."),
            })

    description = None
    for body in soup.select(".ms-rtestate-field"):
        text = _text(body)
        # Skip rich fields that only wrap the attachment links.
        if text and not all(att["name"] in text for att in attachments[:1]) \
                and len(text) > 40:
            description = text[:2000]
            break

    return {
        "title": _text(title_el) or None,
        "fields": fields,
        "attachments": attachments,
        "description": description,
    }


def primary_attachment(attachments: list[dict]) -> dict | None:
    """The document itself, not its abstract/FAQ/annex.

    Preference: a PDF classed "utama", then a ZIP (drafts ship as one
    archive), then any PDF at all.
    """
    for att in attachments:
        if att["kind"] == "utama" and att["ext"] == "pdf":
            return att
    for att in attachments:
        if att["ext"] == "zip":
            return att
    for att in attachments:
        if att["ext"] == "pdf":
            return att
    return None
