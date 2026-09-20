"""Metadata extraction for Indonesian regulations (URD 3.2 & 3.3).

Deterministic, rule-based: regexes tuned to the fixed opening formula of
Indonesian legal drafting ("PERATURAN ... NOMOR ... TAHUN ... TENTANG ...").
No AI involved, so this is the mode that must always work.
"""
from __future__ import annotations

import re
from datetime import date

from hero.models import RegulationMetadata

# Recognised document types, longest label first so "PERATURAN PEMERINTAH
# PENGGANTI UNDANG-UNDANG" wins over "PERATURAN PEMERINTAH".
DOC_TYPES: list[tuple[str, str, str]] = [
    ("PERPPU", "PERATURAN PEMERINTAH PENGGANTI UNDANG-UNDANG",
     r"PERATURAN\s+PEMERINTAH\s+PENGGANTI\s+UNDANG[-\s]?UNDANG"),
    ("SEOJK", "Surat Edaran Otoritas Jasa Keuangan",
     r"SURAT\s+EDARAN\s+(?:OTORITAS\s+JASA\s+KEUANGAN|OJK)"),
    ("PADK", "Peraturan Anggota Dewan Komisioner OJK",
     r"PERATURAN\s+ANGGOTA\s+DEWAN\s+KOMISIONER"),
    ("PDK", "Peraturan Dewan Komisioner OJK",
     r"PERATURAN\s+DEWAN\s+KOMISIONER"),
    ("KEPDK", "Keputusan Dewan Komisioner OJK",
     r"KEPUTUSAN\s+(?:ANGGOTA\s+)?DEWAN\s+KOMISIONER"),
    ("POJK", "Peraturan Otoritas Jasa Keuangan",
     r"PERATURAN\s+(?:OTORITAS\s+JASA\s+KEUANGAN|OJK)"),
    ("PBI", "Peraturan Bank Indonesia",
     r"PERATURAN\s+BANK\s+INDONESIA"),
    ("PADG", "Peraturan Anggota Dewan Gubernur",
     r"PERATURAN\s+ANGGOTA\s+DEWAN\s+GUBERNUR"),
    ("SEBI", "Surat Edaran Bank Indonesia",
     r"SURAT\s+EDARAN\s+BANK\s+INDONESIA"),
    ("PERPRES", "Peraturan Presiden", r"PERATURAN\s+PRESIDEN"),
    ("KEPPRES", "Keputusan Presiden", r"KEPUTUSAN\s+PRESIDEN"),
    ("PERMEN", "Peraturan Menteri", r"PERATURAN\s+MENTERI"),
    ("KEPMEN", "Keputusan Menteri", r"KEPUTUSAN\s+MENTERI"),
    ("PP", "Peraturan Pemerintah", r"PERATURAN\s+PEMERINTAH"),
    ("UU", "Undang-Undang", r"UNDANG[-\s]?UNDANG\s+REPUBLIK\s+INDONESIA"),
    ("KEPDIR", "Keputusan Direksi", r"KEPUTUSAN\s+(?:DEWAN\s+)?DIREKSI"),
    ("SE", "Surat Edaran", r"SURAT\s+EDARAN"),
    ("UU", "Undang-Undang", r"\bUNDANG[-\s]?UNDANG\b"),
    ("PERDA", "Peraturan Daerah", r"PERATURAN\s+DAERAH"),
    # English translations published alongside the Indonesian originals.
    ("POJK", "Financial Services Authority Regulation",
     r"FINANCIAL\s+SERVICES\s+AUTHORITY\s+REGULATION"),
    ("SEOJK", "Circular Letter of the Financial Services Authority",
     r"CIRCULAR\s+LETTER\s+OF\s+THE\s+FINANCIAL\s+SERVICES\s+AUTHORITY"),
    ("PBI", "Bank Indonesia Regulation", r"BANK\s+INDONESIA\s+REGULATION"),
    ("SEBI", "Circular Letter of Bank Indonesia",
     r"CIRCULAR\s+LETTER\s+OF\s+BANK\s+INDONESIA"),
    ("UU", "Law of the Republic of Indonesia",
     r"LAW\s+OF\s+THE\s+REPUBLIC\s+OF\s+INDONESIA"),
    ("PP", "Government Regulation", r"GOVERNMENT\s+REGULATION"),
    ("PERPRES", "Presidential Regulation", r"PRESIDENTIAL\s+REGULATION"),
]

ISSUING_BODIES: list[tuple[str, str]] = [
    (r"OTORITAS\s+JASA\s+KEUANGAN", "Otoritas Jasa Keuangan"),
    (r"BANK\s+INDONESIA", "Bank Indonesia"),
    (r"PRESIDEN\s+REPUBLIK\s+INDONESIA", "Presiden Republik Indonesia"),
    (r"MENTERI\s+KEUANGAN", "Menteri Keuangan"),
    (r"KEMENTERIAN\s+KEUANGAN", "Kementerian Keuangan"),
    (r"LEMBAGA\s+PENJAMIN\s+SIMPANAN", "Lembaga Penjamin Simpanan"),
    (r"KOMISI\s+PENGAWAS\s+PERSAINGAN\s+USAHA", "KPPU"),
    (r"FINANCIAL\s+SERVICES\s+AUTHORITY", "Otoritas Jasa Keuangan"),
    (r"(?:THE\s+GOVERNOR\s+OF\s+)?BANK\s+INDONESIA", "Bank Indonesia"),
]

MONTHS = {
    "januari": 1, "februari": 2, "pebruari": 2, "maret": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "agustus": 8, "september": 9,
    "oktober": 10, "november": 11, "nopember": 11, "desember": 12,
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6,
    "july": 7, "august": 8, "october": 10, "december": 12,
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))

# 11/POJK.03/2022 · 21 /SEOJK.03/2021 · 12 TAHUN 2020 · 4/PBI/2023
# Handles "45/PADK.06/2025", "2/11/PBI/2000" and the spaced English form
# "NUMBER14/15/ PBI / 2012" that appears in BI's translations.
RE_NUMBER_SLASH = re.compile(
    r"(?:NOMOR|NUMBER)\s*[:.]?\s*"
    r"([0-9]{1,4}(?:\s*/\s*[A-Za-z0-9.\-]{1,20}){1,4})",
    re.IGNORECASE)
RE_NUMBER_TAHUN = re.compile(
    r"(?:NOMOR|NUMBER)\s*[:.]?\s*([0-9]{1,4})\s+(?:TAHUN|YEAR|OF)\s+"
    r"((?:19|20)\d{2})", re.IGNORECASE)
RE_BARE_SLASH = re.compile(
    r"\b([0-9]{1,3}\s*/\s*(?:[0-9]{1,3}\s*/\s*)?"
    r"(?:POJK|SEOJK|PBI|PADK|PADG|SEBI|PMK|PER)[A-Za-z0-9.\-]*"
    r"\s*/\s*(?:19|20)\d{2})\b", re.IGNORECASE)
RE_TENTANG = re.compile(r"\b(?:TENTANG|CONCERNING)\b\s*[:\-]?\s*",
                        re.IGNORECASE)
RE_DATE_LONG = re.compile(
    rf"\b(\d{{1,2}})\s+({_MONTH_ALT})\s+((?:19|20)\d{{2}})\b", re.IGNORECASE)
RE_DATE_NUM = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-]((?:19|20)\d{2})\b")
RE_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
RE_DITETAPKAN = re.compile(
    r"(?:Ditetapkan|Ditandatangani|Disahkan|Diundangkan)\s+(?:di|pada)"
    r"[^\n]{0,80}?(?:pada\s+tanggal\s*)?", re.IGNORECASE)

# Status markers
RE_DICABUT = re.compile(
    r"\b(?:dicabut\s+dan\s+dinyatakan\s+tidak\s+berlaku|"
    r"dinyatakan\s+(?:tidak\s+berlaku|dicabut)|status\s*:?\s*dicabut)\b",
    re.IGNORECASE)
RE_DIUBAH = re.compile(
    r"\bPERUBAHAN\s+(?:KE[A-Z]*\s+)?ATAS\b|\bstatus\s*:?\s*diubah\b",
    re.IGNORECASE)

RE_LEGAL_BASIS = re.compile(
    r"(Undang[-\s]?Undang(?:\s+Republik\s+Indonesia)?\s+Nomor\s+[0-9]{1,4}\s+"
    r"Tahun\s+(?:19|20)\d{2}"
    r"|Peraturan\s+(?:Pemerintah|Presiden|Otoritas\s+Jasa\s+Keuangan|"
    r"Bank\s+Indonesia|Menteri[^,;\n]{0,40})\s+Nomor\s+[0-9A-Z./\-]{1,30}"
    r"(?:\s+Tahun\s+(?:19|20)\d{2})?)",
    re.IGNORECASE)

_NOISE_LINE = re.compile(
    r"^(?:salinan|lampiran|halaman|hal\.?|page|www\.|http|otoritas jasa keuangan"
    r"|republik indonesia|republic of indonesia|financial services authority"
    r"|bank indonesia|-\s*\d+\s*-|\d+\s*$)", re.IGNORECASE)


def _head(text: str, chars: int = 4000) -> str:
    return text[:chars]


# The opening formula of an Indonesian regulation is fixed:
#     [SALINAN] <JENIS PERATURAN> NOMOR <nomor> TENTANG <perihal>
# Everything after the first TENTANG belongs to the subject and the preamble,
# where *other* regulations are cited. Restricting type/number detection to
# this block is what stops a "Mengingat" citation being read as the document's
# own identity.
def _identity_block(text: str, cap: int = 2500) -> str:
    window = text[:cap]
    m = RE_TENTANG.search(window)
    if m and m.start() > 20:
        return window[: m.start()]
    return window


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _parse_date(text: str) -> date | None:
    m = RE_DATE_LONG.search(text)
    if m:
        day, month_name, year = m.groups()
        month = MONTHS.get(month_name.lower())
        if month:
            try:
                return date(int(year), month, int(day))
            except ValueError:
                pass
    m = RE_DATE_NUM.search(text)
    if m:
        day, month, year = (int(g) for g in m.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _detect_type(head: str) -> tuple[str | None, str | None, int]:
    """Return (code, label, match position) for the earliest strong match."""
    best: tuple[int, int, str, str] | None = None
    for code, label, pattern in DOC_TYPES:
        m = re.search(pattern, head, re.IGNORECASE)
        if not m:
            continue
        # Earliest occurrence wins; on a tie the longer literal wins, so
        # "PERATURAN ANGGOTA DEWAN KOMISIONER" beats a bare "PERATURAN".
        cand = (m.start(), -(m.end() - m.start()), code, label)
        if best is None or cand[:2] < best[:2]:
            best = cand
    if best is None:
        return None, None, -1
    return best[2], best[3], best[0]


def _extract_subject(text: str) -> str | None:
    """Grab the 'TENTANG ...' clause, which is the real title of the doc."""
    m = RE_TENTANG.search(text)
    if not m:
        return None
    tail = text[m.end(): m.end() + 700]
    lines: list[str] = []
    for raw in tail.split("\n"):
        line = raw.strip()
        if not line:
            if lines:
                break
            continue
        # The subject block ends where the preamble begins, or where a
        # summary sheet starts its own section ("ABSTRAK :", "RINGKASAN") —
        # OJK publishes regulations with an abstract page whose text would
        # otherwise be swallowed into the title.
        if re.match(r"^(DENGAN|MENIMBANG|MENGINGAT|DEWAN|PRESIDEN|MEMUTUSKAN|"
                    r"BAB\b|PASAL\b|Yth\.|KEPADA"
                    r"|ABSTRAK\b|RINGKASAN\b|LATAR\s+BELAKANG|DAFTAR\s+ISI"
                    r"|STATUS\s*:|CATATAN\s*:|STATUS\s+PERATURAN"
                    r"|WITH\s+THE\s+BLESSING|CONSIDERING|IN\s+VIEW\s+OF"
                    r"|HAVING\s+REGARD|DECIDES|DECREES|THE\s+GOVERNOR"
                    r"|BOARD\s+OF|CHAPTER\b|ARTICLE\b|ABSTRACT\b)",
                    line, re.IGNORECASE):
            break
        if _NOISE_LINE.match(line):
            continue
        # A section marker can also sit mid-line once the PDF's columns are
        # flattened, e.g. "... RAKYAT SYARIAH ABSTRAK : - POJK mengenai ...".
        cut = re.search(r"\b(ABSTRAK|ABSTRACT|RINGKASAN)\b\s*:?", line,
                        re.IGNORECASE)
        if cut and cut.start() > 0:
            lines.append(line[: cut.start()].strip())
            break
        if cut:
            break
        lines.append(line)
        if len(" ".join(lines)) > 300:
            break
    subject = _norm_space(" ".join(lines)).strip(" ,.;:-")
    # Hard cap: the soft check above can overshoot on a single long line,
    # and a 700-character "title" poisons both the KB filename and search.
    if len(subject) > 300:
        subject = subject[:300].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return subject or None


def _extract_number(block: str) -> tuple[str | None, str | None, int | None]:
    """Return (normalised number, raw match, year) from the identity block.

    The last NOMOR in the block is the document's own: a header may name the
    issuing hierarchy first, but the regulation's number always sits directly
    above TENTANG.
    """
    slash = list(RE_NUMBER_SLASH.finditer(block))
    tahun = list(RE_NUMBER_TAHUN.finditer(block))

    best = None
    if slash:
        best = ("slash", slash[-1])
    if tahun and (best is None or tahun[-1].start() > best[1].start()):
        best = ("tahun", tahun[-1])

    if best is not None:
        kind, m = best
        if kind == "tahun":
            n, y = m.groups()
            return f"{n} Tahun {y}", m.group(0).strip(), int(y)
        raw = m.group(1)
        num = re.sub(r"\s+", "", raw).strip(" ./-")
        y = RE_YEAR.search(num)
        return num, raw.strip(), int(y.group(1)) if y else None

    m = RE_BARE_SLASH.search(block)
    if m:
        raw = m.group(1)
        num = re.sub(r"\s+", "", raw)
        y = RE_YEAR.search(num)
        return num, raw, int(y.group(1)) if y else None
    return None, None, None


def _extract_issuer(head: str) -> str | None:
    for pattern, label in ISSUING_BODIES:
        if re.search(pattern, head, re.IGNORECASE):
            return label
    return None


def _detect_status(text: str, subject: str | None = None) -> str:
    """Status of *this* document, not of the ones it refers to.

    A regulation is an amendment only when its own TENTANG clause reads
    "PERUBAHAN ATAS ...". The preamble cites amendments to *other* regulations
    constantly, so it must not be consulted here.
    """
    tail = text[-3000:]
    if re.search(r"peraturan\s+ini\s+dinyatakan\s+(?:tidak\s+berlaku|dicabut)",
                 tail, re.IGNORECASE):
        return "dicabut"
    if subject and RE_DIUBAH.search(subject):
        return "diubah"
    return "berlaku"


def _extract_legal_basis(text: str, limit: int = 25) -> list[str]:
    """Collect the 'Mengingat' citations — feeds harmonisation (URD 3.4)."""
    window = text
    m = re.search(r"\bMengingat\s*:", text, re.IGNORECASE)
    if m:
        window = text[m.start(): m.start() + 6000]
    # Keyed on the lowercase form to de-duplicate, but the original casing is
    # what gets displayed and cited.
    seen: dict[str, str] = {}
    for match in RE_LEGAL_BASIS.finditer(window):
        item = _norm_space(match.group(1))
        seen.setdefault(item.lower(), item)
        if len(seen) >= limit:
            break
    return list(seen.values())


def _clean_pdf_title(raw: str) -> str | None:
    """PDF /Title is usually the authoring filename, e.g.
    "Microsoft Word - Peraturan.docx". Strip that wrapper before using it."""
    title = (raw or "").strip()
    if not title:
        return None
    title = re.sub(r"^Microsoft\s+Word\s*-\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\.(docx?|pdf|rtf|odt)$", "", title, flags=re.IGNORECASE)
    title = _norm_space(title.replace("_", " "))
    return title or None


def _build_title(md: RegulationMetadata) -> str | None:
    parts = [md.doc_type_label or md.doc_type]
    if md.number:
        parts.append(f"Nomor {md.number}")
    if md.subject:
        parts.append(f"tentang {md.subject}")
    title = " ".join(p for p in parts if p).strip()
    return title or None


def extract_metadata(
    text: str,
    pdf_metadata: dict | None = None,
    filename: str | None = None,
) -> RegulationMetadata:
    """Derive regulation metadata from document text (+ PDF info + filename)."""
    md = RegulationMetadata()
    text = text or ""
    head = _head(text)
    identity = _identity_block(text)
    score = 0.0

    code, label, _ = _detect_type(identity)
    if not code:
        code, label, _ = _detect_type(head)
    if not code and filename:
        code, label, _ = _detect_type(filename.upper().replace("_", " "))
    if code:
        md.doc_type, md.doc_type_label = code, label
        score += 0.25

    md.number, md.number_raw, year = _extract_number(identity)
    if not md.number and filename:
        md.number, md.number_raw, year = _extract_number(
            "NOMOR " + filename.replace("_", "/"))
    if md.number:
        score += 0.3

    md.subject = _extract_subject(text)
    if md.subject:
        score += 0.2

    md.issuing_body = _extract_issuer(identity) or _extract_issuer(head)
    if md.issuing_body:
        score += 0.1

    # Signing date lives in the closing block; fall back to the header.
    md.issued_date = None
    m = RE_DITETAPKAN.search(text)
    if m:
        md.issued_date = _parse_date(text[m.end(): m.end() + 200])
    if md.issued_date is None:
        md.issued_date = _parse_date(text[-2500:]) or _parse_date(head)
    if md.issued_date:
        score += 0.15

    md.year = year or (md.issued_date.year if md.issued_date else None)
    if md.year is None:
        cands = [int(y) for y in RE_YEAR.findall(head)]
        md.year = max(cands) if cands else None

    md.status = _detect_status(text, md.subject) if text else "unknown"
    md.legal_basis = _extract_legal_basis(text)

    md.title = _build_title(md) or _clean_pdf_title(
        (pdf_metadata or {}).get("title", "")) or (
        filename.rsplit(".", 1)[0].replace("_", " ") if filename else None)

    if not text.strip():
        md.warnings.append("no text available for metadata extraction")
    if not md.doc_type:
        md.warnings.append("document type not recognised")
    if not md.number:
        md.warnings.append("regulation number not found")
    if not md.issued_date:
        md.warnings.append("issue date not found")

    md.confidence = round(min(score, 1.0), 2)
    return md
