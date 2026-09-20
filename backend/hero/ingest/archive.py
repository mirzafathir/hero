"""ZIP handling for sources that publish a document as an archive.

OJK's draft-regulation listing ships each draft as one ZIP (confirmed live
2026-09-15): "00 Batang Tubuh.pdf" (the draft's body text), several
"Lampiran" PDFs, and a "matriks tanggapan" .docx for public comments. The
body text is the regulation; everything else is supporting material.
"""
from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

ZIP_MAGIC = b"PK\x03\x04"
_MAX_MEMBER_BYTES = 150 * 1024 * 1024
_BODY_HINTS = ("batang tubuh", "batang_tubuh", "batangtubuh", "body")
_COMPANION_HINTS = ("lampiran", "matriks", "abstrak", "faq", "tanggapan",
                    "penjelasan", "naskah akademik")


def is_zip(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == ZIP_MAGIC
    except OSError:
        return False


def list_members(path: Path) -> list[dict]:
    """Name, size and type of every file inside the archive."""
    with zipfile.ZipFile(path) as zf:
        return [
            {"name": PurePosixPath(i.filename).name, "path": i.filename,
             "size": i.file_size, "ext": PurePosixPath(i.filename).suffix.lstrip(".").lower()}
            for i in zf.infolist() if not i.is_dir()
        ]


def _rank(member: dict) -> tuple[int, int]:
    """Lower is better: body text first, companions last, larger breaks ties
    among unlabelled PDFs (the regulation is rarely the smallest file)."""
    low = member["name"].lower()
    if any(h in low for h in _BODY_HINTS):
        tier = 0
    elif any(h in low for h in _COMPANION_HINTS):
        tier = 2
    else:
        tier = 1
    return tier, -member["size"]


def extract_primary_pdf(path: Path, dest_dir: Path) -> tuple[Path | None, list[dict]]:
    """Extract the archive's main PDF. Returns (extracted path, all members).

    Only the chosen member is written to disk, and its name is sanitised, so
    a hostile archive cannot write outside ``dest_dir`` (zip-slip).
    """
    try:
        members = list_members(path)
    except zipfile.BadZipFile as exc:
        log.warning("%s: not a readable ZIP (%s)", path.name, exc)
        return None, []
    pdfs = [m for m in members if m["ext"] == "pdf" and 0 < m["size"] <= _MAX_MEMBER_BYTES]
    if not pdfs:
        return None, members
    best = sorted(pdfs, key=_rank)[0]
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._\- ]+", "_", best["name"]).strip(" ._") or "dokumen.pdf"
    target = dest_dir / f"{path.stem[:60]}__{safe}"
    with zipfile.ZipFile(path) as zf, zf.open(best["path"]) as src, open(target, "wb") as out:
        out.write(src.read())
    return target, members
