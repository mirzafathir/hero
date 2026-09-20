"""Parse the BAB / Bagian / Pasal / ayat hierarchy of a regulation.

URD 3.3: "Ekstraksi struktur dokumen (bab, pasal, ayat) berdasarkan pola
penomoran baku." The resulting Article list is the unit of comparison for
harmonisation (URD 3.4) and for the PoV response checklist (URD 3.5).
"""
from __future__ import annotations

import re

from hero.models import Article, DocumentStructure, PageText

ROMAN = r"[IVXLCDM]+"

RE_BAB = re.compile(rf"^\s*(?:BAB|CHAPTER)\s+({ROMAN})\b\s*(.*)$",
                    re.IGNORECASE)
RE_BAGIAN = re.compile(r"^\s*BAGIAN\s+(\w+)\b\s*(.*)$", re.IGNORECASE)
RE_PARAGRAF = re.compile(r"^\s*PARAGRAF\s+(\w+)\b\s*(.*)$", re.IGNORECASE)
# "Pasal 12" / "Pasal 12A" / "Pasal 12 ayat (2)" — the heading form stands alone.
# "Article N" is the heading used in the English translations.
RE_PASAL = re.compile(r"^\s*(?:PASAL|ARTICLE)\s+(\d+[A-Za-z]?)\s*$",
                      re.IGNORECASE)
RE_PASAL_INLINE = re.compile(r"^\s*(?:PASAL|ARTICLE)\s+(\d+[A-Za-z]?)\b\s*(.*)$",
                             re.IGNORECASE)
# The marker may stand alone on its line, so the text part is optional.
RE_AYAT = re.compile(r"^\s*\((\d{1,2})\)\s*(.*)$")
# The closing formula ends the normative body. Matched case-insensitively
# because the signing block is set in mixed case.
# "I. KETENTUAN UMUM" — the section heading form used by Surat Edaran.
# Only I/V/X numerals: a lone "C." or "L." is an outline letter in a lampiran
# form, not section 100 or 50.
RE_SECTION = re.compile(
    r"^\s*(X{0,3}(?:IX|IV|V?I{0,3}))\.\s+([A-Z][A-Z0-9\s,./()\-]{3,80})\s*$")

RE_CLOSING = re.compile(
    r"^\s*(?:Ditetapkan\s+di|Diundangkan\s+di|Enacted\s+in|Promulgated\s+in|"
    r"Agar\s+setiap\s+orang\s+mengetahuinya)\b", re.IGNORECASE)
# The promulgation stamp is always all-caps. Case-SENSITIVE on purpose: the
# mixed-case "Lembaran Negara Republik Indonesia Nomor 5253)" that appears in
# every Mengingat citation must not be mistaken for the end of the body.
RE_PROMULGATION = re.compile(
    r"^\s*(?:TAMBAHAN\s+)?LEMBARAN\s+NEGARA\s+REPUBLIK\s+INDONESIA\b")
# Lampiran and Penjelasan both restate "Pasal N" as citations.
RE_LAMPIRAN = re.compile(r"^\s*(?:LAMPIRAN|PENJELASAN)\b")

_MAX_HEADING_LEN = 120


_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10}


def roman_to_int(numeral: str) -> int:
    """Convert I..XXXIX. Returns 0 for anything unparseable."""
    total, prev = 0, 0
    for char in reversed(numeral.upper()):
        value = _ROMAN_VALUES.get(char, 0)
        if not value:
            return 0
        total = total - value if value < prev else total + value
        prev = max(prev, value)
    return total


def _is_heading_like(line: str) -> bool:
    """Headings are short and mostly uppercase in Indonesian legal drafting."""
    stripped = line.strip()
    if not stripped or len(stripped) > _MAX_HEADING_LEN:
        return False
    letters = [c for c in stripped if c.isalpha()]
    if not letters:
        return False
    return sum(c.isupper() for c in letters) / len(letters) > 0.7


def _split_ayat(body: str) -> list[tuple[str, str]]:
    """Split a pasal into its ayat.

    Ayat are always numbered sequentially from (1), so only a marker matching
    the next expected number opens a new ayat. That is what separates a real
    "(2)" heading from the "(2)" of a cross-reference such as
    "sebagaimana dimaksud pada ayat (1) dan ayat (2)".
    """
    ayat: list[tuple[str, str]] = []
    current_num: str | None = None
    buf: list[str] = []
    expected = 1
    for line in body.split("\n"):
        m = RE_AYAT.match(line)
        if m and int(m.group(1)) == expected:
            if current_num is not None:
                ayat.append((current_num, "\n".join(buf).strip()))
            current_num, buf = m.group(1), [m.group(2)]
            expected += 1
        elif current_num is not None:
            buf.append(line)
    if current_num is not None:
        ayat.append((current_num, "\n".join(buf).strip()))
    return [(n, t) for n, t in ayat if t]


def parse_structure(pages: list[PageText]) -> DocumentStructure:
    """Walk the document line by line, tracking the current heading context."""
    struct = DocumentStructure()
    if not pages:
        return struct

    cur_bab: str | None = None
    cur_bagian: str | None = None
    cur_paragraf: str | None = None
    pending_bab: str | None = None   # BAB number awaiting its title line
    in_attachment = False
    sections_done = False   # body sections finished, lampiran forms follow

    current: Article | None = None
    buf: list[str] = []

    def close_current() -> None:
        nonlocal current, buf
        if current is not None:
            body = "\n".join(buf).strip()
            current.text = body
            current.ayat = _split_ayat(body)
            if body:
                if current.in_attachment:
                    struct.attachment_articles.append(current)
                else:
                    struct.articles.append(current)
        current, buf = None, []

    def start_article(number: str, page: int) -> None:
        nonlocal current
        close_current()
        current = Article(
            number=number.upper(), text="", bab=cur_bab,
            bagian=cur_bagian, paragraf=cur_paragraf, page=page,
            in_attachment=in_attachment,
        )

    for page in pages:
        for raw in page.text.split("\n"):
            line = raw.rstrip()
            stripped = line.strip()

            if pending_bab is not None:
                # The line right after "BAB II" is normally its title.
                if stripped and _is_heading_like(stripped) \
                        and not RE_PASAL_INLINE.match(stripped):
                    struct.babs[-1]["title"] = stripped.title()
                    cur_bab = f"BAB {pending_bab} — {stripped.title()}"
                pending_bab = None
                if struct.babs and cur_bab is None:
                    cur_bab = f"BAB {struct.babs[-1]['number']}"
                if stripped and struct.babs and \
                        struct.babs[-1].get("title") == stripped.title():
                    continue

            m = RE_BAB.match(stripped)
            if m and len(stripped) < _MAX_HEADING_LEN:
                close_current()
                number, inline_title = m.group(1).upper(), m.group(2).strip()
                struct.babs.append({
                    "number": number,
                    "title": inline_title.title() or None,
                    "page": page.number,
                })
                cur_bab = f"BAB {number}"
                if inline_title:
                    cur_bab += f" — {inline_title.title()}"
                else:
                    pending_bab = number
                cur_bagian = cur_paragraf = None
                continue

            m = RE_BAGIAN.match(stripped)
            if m and _is_heading_like(stripped):
                cur_bagian = stripped.title()
                cur_paragraf = None
                continue

            m = RE_PARAGRAF.match(stripped)
            if m and _is_heading_like(stripped):
                cur_paragraf = stripped.title()
                continue

            m = RE_PASAL.match(stripped)
            if m:
                start_article(m.group(1), page.number)
                continue

            # Section headings are recorded but do not interrupt article
            # collection: a regulation with pasal never uses them in its body.
            m = RE_SECTION.match(stripped)
            if m and m.group(1) and not in_attachment and not sections_done:
                value = roman_to_int(m.group(1))
                # Body sections run I, II, III… without gaps. A numeral that
                # breaks the run means the lampiran forms have started, and
                # those restart their own numbering on every table.
                if value != len(struct.sections) + 1:
                    sections_done = True
                    continue
                struct.sections.append({
                    "number": m.group(1),
                    "title": m.group(2).strip().title(),
                    "page": page.number,
                })
                if current is None:
                    cur_bagian = f"{m.group(1)}. {m.group(2).strip().title()}"
                continue

            # Everything past the closing formula is lampiran/penjelasan
            # material, where "Pasal N" is a citation rather than a heading.
            if (RE_CLOSING.match(stripped) or RE_PROMULGATION.match(stripped)
                    ) and (struct.articles or struct.sections
                           or current is not None):
                close_current()
                in_attachment = True
                cur_bab = cur_bagian = cur_paragraf = None
                continue

            if RE_LAMPIRAN.match(stripped) and _is_heading_like(stripped) and (
                    struct.articles or struct.sections or current is not None):
                close_current()
                in_attachment = True
                cur_bab = cur_bagian = cur_paragraf = None
                continue

            if current is not None:
                buf.append(line)

    close_current()
    struct.has_structure = bool(struct.articles or struct.sections)
    return struct


def article_index(struct: DocumentStructure) -> dict[str, Article]:
    """Map 'Pasal 5' -> Article, keeping the first occurrence of each."""
    idx: dict[str, Article] = {}
    for art in struct.articles:
        idx.setdefault(art.number, art)
    return idx


def structure_quality(struct: DocumentStructure) -> dict[str, object]:
    """Sanity signals for the parse, so a bad extraction is visible."""
    numbers = [a.number for a in struct.articles]
    plain = [int(re.sub(r"\D", "", n) or 0) for n in numbers]
    ascending = all(a <= b for a, b in zip(plain, plain[1:]))
    return {
        "article_count": len(struct.articles),
        "section_count": len(struct.sections),
        "attachment_article_count": len(struct.attachment_articles),
        "numbering_ascending": ascending,
        "duplicate_numbers": sorted(
            {n for n in numbers if numbers.count(n) > 1}),
        "starts_at_pasal_1": bool(plain) and plain[0] == 1,
    }
