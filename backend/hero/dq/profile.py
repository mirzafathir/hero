"""Profiling kolom: memotret isi tabel apa adanya, tanpa menghakimi.

Profiling menjawab "data ini sebenarnya berisi apa", sementara aturan mutu
(``hero.dq.rules``) menjawab "apakah isinya sesuai harapan". Keduanya
dipisah dengan sengaja: profil tetap berguna saat kita belum tahu harapan
yang benar, dan justru dari profil itulah ambang aturan disusun.

Contoh nyata pada proyek ini: profil kolom ``number`` memperlihatkan 20,3%
kosong. Menelusuri sebarannya per sumber menunjukkan seluruh kekosongan
berasal dari rancangan peraturan — temuan yang mengubah "kegagalan parser"
menjadi "sifat data", lalu mengubah rumusan aturan kelengkapannya.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ColumnProfile:
    """Potret satu kolom."""

    name: str
    declared_type: str
    total_rows: int
    missing: int
    distinct: int
    top_values: list[tuple[Any, int]] = field(default_factory=list)
    min_value: Any = None
    max_value: Any = None
    mean_length: float | None = None

    @property
    def missing_rate(self) -> float:
        return self.missing / self.total_rows if self.total_rows else 0.0

    @property
    def fill_rate(self) -> float:
        return 1.0 - self.missing_rate

    @property
    def is_constant(self) -> bool:
        """Kolom berisi satu nilai saja — biasanya tidak membawa informasi."""
        return self.distinct <= 1 and self.total_rows > 1

    @property
    def is_unique(self) -> bool:
        """Kolom berpotensi menjadi kunci."""
        return self.total_rows > 0 and self.distinct == self.total_rows - self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "declared_type": self.declared_type,
            "total_rows": self.total_rows,
            "missing": self.missing,
            "missing_rate": round(self.missing_rate, 4),
            "distinct": self.distinct,
            "is_constant": self.is_constant,
            "is_unique": self.is_unique,
            "min": self.min_value,
            "max": self.max_value,
            "mean_length": self.mean_length,
            "top_values": [{"value": v, "count": c} for v, c in self.top_values],
        }


# Kolom yang isinya panjang dan bebas; sebaran nilai teratasnya tidak
# informatif (hampir semua nilai unik) dan memuat teks dokumen penuh.
_SKIP_TOP_VALUES = {"text", "fields_json", "attachments_json", "metadata_json",
                    "title", "stored_path", "detail_url", "document_url"}


def profile_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    top_n: int = 5,
    columns: list[str] | None = None,
) -> list[ColumnProfile]:
    """Profil setiap kolom ``table``.

    Nilai dianggap hilang bila NULL atau berupa teks kosong/spasi saja —
    scraper menuliskan keduanya untuk hal yang sama, dan membedakannya hanya
    akan membuat angka kelengkapan terlihat lebih baik dari kenyataannya.
    """
    conn.row_factory = sqlite3.Row
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not info:
        raise ValueError(f"tabel '{table}' tidak ditemukan")
    total = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    wanted = set(columns) if columns else None
    profiles: list[ColumnProfile] = []
    for col in info:
        name, declared = col["name"], (col["type"] or "").upper()
        if wanted is not None and name not in wanted:
            continue
        present = f'COALESCE(TRIM(CAST("{name}" AS TEXT)), \'\') <> \'\''
        row = conn.execute(
            f'SELECT SUM(CASE WHEN {present} THEN 0 ELSE 1 END) AS missing,'
            f'       COUNT(DISTINCT CASE WHEN {present} THEN "{name}" END) AS distinct_n,'
            f'       MIN(CASE WHEN {present} THEN "{name}" END) AS min_v,'
            f'       MAX(CASE WHEN {present} THEN "{name}" END) AS max_v,'
            f'       AVG(CASE WHEN {present} THEN LENGTH(CAST("{name}" AS TEXT)) END) AS mean_len'
            f" FROM {table}"
        ).fetchone()

        top: list[tuple[Any, int]] = []
        if name not in _SKIP_TOP_VALUES and total:
            top = [
                (r[0], int(r[1])) for r in conn.execute(
                    f'SELECT "{name}", COUNT(*) FROM {table} WHERE {present}'
                    f' GROUP BY 1 ORDER BY 2 DESC LIMIT {int(top_n)}')
            ]

        mean_len = row["mean_len"]
        profiles.append(ColumnProfile(
            name=name, declared_type=declared, total_rows=total,
            missing=int(row["missing"] or 0), distinct=int(row["distinct_n"] or 0),
            top_values=top, min_value=row["min_v"], max_value=row["max_v"],
            mean_length=round(float(mean_len), 1) if mean_len is not None else None,
        ))
    return profiles
