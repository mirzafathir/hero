"""Rule-based classification and knowledge-base placement (URD 3.2).

"Klasifikasi dan penempatan dokumen ke folder knowledge base yang sesuai
(folder eksisting atau folder baru dibuat otomatis)."

Layout: <kb>/<source>/<category>/<doc_type>/<year>/<status>/<file>.pdf
"""
from __future__ import annotations

import re
from pathlib import Path

from hero.models import RegulationMetadata

# Ordered: the first category whose keywords appear wins.
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("teknologi-informasi", (
        "teknologi informasi", "sistem elektronik", "keamanan siber",
        "data pribadi", "layanan digital", "inovasi teknologi sektor keuangan",
        "itsk", "penyelenggara sistem", "alih daya", "outsourcing",
        "pusat data", "manajemen risiko teknologi",
    )),
    ("perbankan", (
        "bank umum", "perbankan", "bank perkreditan", "bpr", "bprs",
        "bank syariah", "kredit", "permodalan bank", "kualitas aset",
    )),
    ("pasar-modal", (
        "pasar modal", "emiten", "efek", "bursa", "reksa dana",
        "perusahaan publik", "penawaran umum", "manajer investasi",
    )),
    ("iknb", (
        "asuransi", "dana pensiun", "lembaga pembiayaan", "penjaminan",
        "pergadaian", "modal ventura", "perusahaan pembiayaan", "iknb",
    )),
    ("perlindungan-konsumen", (
        "perlindungan konsumen", "edukasi konsumen", "pengaduan konsumen",
        "literasi keuangan",
    )),
    ("apu-ppt", (
        "pencucian uang", "pendanaan terorisme", "apu", "ppt",
        "know your customer", "prinsip mengenal nasabah",
    )),
    ("tata-kelola", (
        "tata kelola", "good corporate governance", "governance",
        "manajemen risiko", "kepatuhan", "audit intern", "pengendalian intern",
    )),
    ("kelembagaan", (
        "otoritas jasa keuangan", "organisasi dan tata kerja",
        "kelembagaan", "dewan komisioner",
    )),
]

UNCLASSIFIED = "lain-lain"
_SAFE = re.compile(r"[^a-z0-9\-]+")


def _slug(value: str, max_len: int = 60) -> str:
    slug = _SAFE.sub("-", value.lower().replace("/", "-")).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:max_len].strip("-") or "dokumen"


def classify(
    metadata: RegulationMetadata,
    text: str = "",
    hint: str | None = None,
) -> tuple[str, list[str]]:
    """Return (category, matched keywords). ``hint`` from config wins outright."""
    if hint:
        return _slug(hint), ["configured hint"]

    # Weight the title/subject heavily; the body is only a tie-breaker.
    title_blob = " ".join(
        filter(None, [metadata.title, metadata.subject])).lower()
    body_blob = (text or "")[:20000].lower()

    scores: dict[str, tuple[float, list[str]]] = {}
    for category, keywords in CATEGORY_RULES:
        hits, score = [], 0.0
        for kw in keywords:
            if kw in title_blob:
                score += 5.0
                hits.append(kw)
            elif kw in body_blob:
                score += 1.0 * min(body_blob.count(kw), 3)
                hits.append(kw)
        if score:
            scores[category] = (score, hits)

    if not scores:
        return UNCLASSIFIED, []
    best = max(scores.items(), key=lambda kv: kv[1][0])
    return best[0], best[1][1][:6]


STATUS_FOLDERS = {
    "berlaku": "berlaku",
    "dicabut": "dicabut",
    "diubah": "diubah",
    "rancangan": "rancangan",
}
UNKNOWN_STATUS_FOLDER = "status-tidak-diketahui"


def status_folder(status: str | None) -> str:
    return STATUS_FOLDERS.get((status or "").strip().lower(), UNKNOWN_STATUS_FOLDER)


def kb_relative_path(metadata: RegulationMetadata, category: str,
                     filename: str, source: str = "lainnya") -> Path:
    """Build <source>/<category>/<type>/<year>/<status>/<normalised-name>.pdf.

    Source comes first because it answers "how far can I trust this?" before
    anything else: JDIH is the official register, a draft is by definition not
    law yet, an upload is whatever someone handed in. Status comes last so a
    regulation that is revoked moves one folder sideways, not across the tree.
    """
    source_dir = _slug(source or "lainnya", 40)
    doc_type = _slug(metadata.doc_type or "tanpa-jenis", 24)
    year = str(metadata.year) if metadata.year else "tanpa-tahun"

    parts: list[str] = []
    if metadata.doc_type:
        parts.append(metadata.doc_type)
    if metadata.number:
        parts.append(metadata.number)
    if metadata.subject:
        parts.append(metadata.subject[:70])
    stem = _slug("-".join(parts), 110) if parts else _slug(Path(filename).stem, 110)
    return (Path(source_dir) / _slug(category or UNCLASSIFIED, 40) / doc_type
            / year / status_folder(metadata.status) / f"{stem}.pdf")
