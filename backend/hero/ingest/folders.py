"""Read PDFs from local directories and synced OneDrive folders (URD 3.2, jalur 3)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

# OneDrive/Finder droppings that should never be ingested.
SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
SKIP_DIR_PREFIXES = (".", "~$", "__pycache__")


@dataclass
class FolderReport:
    name: str
    path: str
    accessible: bool = True
    files_seen: int = 0
    pdf_files: list[Path] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None


def iter_pdf_files(root: Path, recursive: bool = True) -> Iterator[Path]:
    """Yield candidate PDF paths, skipping hidden and placeholder files."""
    if not root.exists():
        return
    walker = os.walk(root) if recursive else [
        (str(root), [], [p.name for p in root.iterdir() if p.is_file()])
    ]
    for dirpath, dirnames, filenames in walker:
        dirnames[:] = [
            d for d in dirnames if not d.startswith(SKIP_DIR_PREFIXES)
        ]
        for fname in sorted(filenames):
            if fname in SKIP_NAMES or fname.startswith(SKIP_DIR_PREFIXES):
                continue
            if fname.lower().endswith(".pdf"):
                yield Path(dirpath) / fname


def scan_folder(
    name: str, path: str | Path, recursive: bool = True
) -> FolderReport:
    """Inspect a source folder and report what can be ingested.

    Mitigates the URD risk "Akses ke folder lokal/OneDrive public terkendala
    hak akses": permission problems are reported per folder, never raised.
    """
    root = Path(os.path.expandvars(os.path.expanduser(str(path))))
    report = FolderReport(name=name, path=str(root))

    if not root.exists():
        report.accessible = False
        report.error = "folder does not exist"
        return report
    if not root.is_dir():
        report.accessible = False
        report.error = "path is not a directory"
        return report
    if not os.access(root, os.R_OK | os.X_OK):
        report.accessible = False
        report.error = "no read permission"
        return report

    try:
        for pdf in iter_pdf_files(root, recursive):
            report.files_seen += 1
            try:
                size = pdf.stat().st_size
            except OSError as exc:
                report.skipped.append((str(pdf), f"stat failed: {exc}"))
                continue
            if size == 0:
                # OneDrive "online-only" files materialise as 0 bytes.
                report.skipped.append(
                    (str(pdf), "empty file (possibly a cloud placeholder)"))
                continue
            if not os.access(pdf, os.R_OK):
                report.skipped.append((str(pdf), "no read permission"))
                continue
            report.pdf_files.append(pdf)
    except OSError as exc:
        report.accessible = False
        report.error = f"scan failed: {exc}"
    return report
