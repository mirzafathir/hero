"""Analisa cakupan: memilih penyebut yang benar sebelum menghitung persentase.

Modul ini lahir dari satu temuan. Laporan sebelumnya menyatakan 561 dari
1.570 rekaman ojk.go.id (36%) berstatus "tidak diketahui", dan angka itu
dibaca sebagai kelemahan sistem. Setelah ditelusuri, JDIH OJK hanya
meregister lima jenis peraturan — UU, PADK, POJK, SEOJK, PERPRES — karena ia
register milik OJK sendiri, bukan basis data hukum nasional. PBI terbitan
Bank Indonesia, PMK/KMK Kementerian Keuangan, dan Keputusan Bapepam-LK tidak
akan pernah ada di sana, seberapa pun baiknya pencocokan ditulis.

Diukur terhadap penyebut yang benar — rekaman yang jenisnya memang ada di
JDIH — cakupan rekonsiliasi adalah 92,4%, dan sisa yang benar-benar layak
diperbaiki tinggal puluhan, bukan ratusan. Angka pertama membuat tim
mengejar sesuatu yang mustahil; angka kedua menunjukkan pekerjaan yang nyata.

Prinsip yang dipegang seluruh modul ini: batas yang bersifat struktural
dilaporkan sebagai batas, bukan disembunyikan di dalam persentase kegagalan.
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from typing import Any

from hero.dq.rules import JDIH_REGISTERED_TYPES

SOURCE_JDIH = "jdih-ojk"
SOURCE_REGULASI = "ojk-regulasi"
SOURCE_RANCANGAN = "ojk-rancangan"

_WS = re.compile(r"\s+")


def _norm_number(value: str | None) -> str:
    """Samakan bentuk penulisan nomor agar dapat dibandingkan.

    ojk.go.id menuliskan nomor yang sama dengan spasi yang berbeda-beda
    (``KEP- 430/BL/2012`` dan ``KEP-430/BL/2012``), sehingga perbandingan
    mentah akan menganggapnya dua peraturan berbeda.
    """
    return _WS.sub("", (value or "")).upper()


def jdih_universe(conn: sqlite3.Connection) -> dict[str, Any]:
    """Jenis peraturan apa saja yang benar-benar diregister JDIH OJK.

    Dihitung dari isi tabel, bukan dari daftar tetap, supaya analisa ikut
    menyesuaikan bila JDIH kelak meregister jenis baru. Daftar tetap hanya
    dipakai sebagai cadangan saat rekaman JDIH belum ada di database.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT doc_type, COUNT(*) n FROM inventory"
        " WHERE source = ? AND COALESCE(TRIM(doc_type), '') <> ''"
        " GROUP BY 1 ORDER BY 2 DESC", (SOURCE_JDIH,)
    ).fetchall()
    observed = {r["doc_type"]: int(r["n"]) for r in rows}
    return {
        "types": tuple(observed) or JDIH_REGISTERED_TYPES,
        "counts": observed,
        "from_data": bool(observed),
    }


def reconciliation_coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    """Cakupan rekonsiliasi status, diukur terhadap semesta yang tepat.

    Mengembalikan dua angka yang sengaja disandingkan: cakupan naif
    (terhadap seluruh rekaman) dan cakupan terhadap semesta yang dapat
    dicocokkan. Selisih keduanya adalah besarnya salah baca yang selama ini
    terjadi, dan itu bagian dari temuan — bukan sesuatu yang perlu ditutup.
    """
    conn.row_factory = sqlite3.Row
    types = jdih_universe(conn)["types"]
    placeholders = ", ".join("?" * len(types))

    total = int(conn.execute(
        "SELECT COUNT(*) FROM inventory WHERE source = ?", (SOURCE_REGULASI,)
    ).fetchone()[0])
    if total == 0:
        return {"total": 0, "matchable": 0, "out_of_universe": 0,
                "resolved": 0, "unresolved_addressable": 0,
                "unresolved_structural": 0, "naive_coverage": 0.0,
                "true_coverage": 0.0, "jdih_types": list(types),
                "addressable_breakdown": []}

    matchable = int(conn.execute(
        f"SELECT COUNT(*) FROM inventory WHERE source = ?"
        f" AND doc_type IN ({placeholders})", (SOURCE_REGULASI, *types)
    ).fetchone()[0])
    resolved = int(conn.execute(
        f"SELECT COUNT(*) FROM inventory WHERE source = ?"
        f" AND doc_type IN ({placeholders}) AND status <> 'unknown'",
        (SOURCE_REGULASI, *types)
    ).fetchone()[0])
    # COALESCE wajib: dalam SQL, `NULL NOT IN (...)` bernilai NULL, bukan
    # benar. Tanpa ini, rekaman yang jenisnya gagal terbaca menghilang dari
    # kedua kategori sekaligus — persis jenis pengurangan diam-diam yang
    # ingin dicegah modul ini.
    unresolved_structural = int(conn.execute(
        f"SELECT COUNT(*) FROM inventory WHERE source = ? AND status = 'unknown'"
        f" AND COALESCE(doc_type, '') NOT IN ({placeholders})",
        (SOURCE_REGULASI, *types)
    ).fetchone()[0])
    breakdown = [dict(r) for r in conn.execute(
        f"SELECT doc_type,"
        f" SUM(CASE WHEN COALESCE(reg_key, '') = '' THEN 1 ELSE 0 END) tanpa_reg_key,"
        f" COUNT(*) n FROM inventory WHERE source = ? AND status = 'unknown'"
        f" AND doc_type IN ({placeholders}) GROUP BY 1 ORDER BY 3 DESC",
        (SOURCE_REGULASI, *types)
    )]
    out_types = [dict(r) for r in conn.execute(
        f"SELECT COALESCE(NULLIF(TRIM(doc_type), ''), '(jenis tidak terbaca)') doc_type,"
        f" COUNT(*) n FROM inventory WHERE source = ? AND status = 'unknown'"
        f" AND COALESCE(doc_type, '') NOT IN ({placeholders})"
        f" GROUP BY 1 ORDER BY 2 DESC LIMIT 10", (SOURCE_REGULASI, *types)
    )]

    unknown_total = int(conn.execute(
        "SELECT COUNT(*) FROM inventory WHERE source = ? AND status = 'unknown'",
        (SOURCE_REGULASI,)
    ).fetchone()[0])

    return {
        "total": total,
        "matchable": matchable,
        "out_of_universe": total - matchable,
        "resolved": resolved,
        "unresolved_addressable": matchable - resolved,
        "unresolved_structural": unresolved_structural,
        "unknown_total": unknown_total,
        # Penjumlahan yang wajib seimbang. Bila tidak, ada rekaman yang lolos
        # dari kedua kategori dan seluruh persentase di atas ikut salah —
        # karena itu hasilnya dibawa serta, bukan hanya diperiksa sekali.
        "balances": (matchable - resolved) + unresolved_structural == unknown_total,
        "naive_coverage": round(resolved / total, 4),
        "true_coverage": round(resolved / matchable, 4) if matchable else 0.0,
        "jdih_types": list(types),
        "addressable_breakdown": breakdown,
        "out_of_universe_types": out_types,
    }


def unique_regulations(conn: sqlite3.Connection) -> dict[str, Any]:
    """Berapa peraturan yang sebenarnya ada di balik sekian rekaman.

    Satu peraturan dapat terdaftar di JDIH sekaligus di ojk.go.id, dan di
    ojk.go.id sendiri dapat muncul dua kali — sekali di kanal "semua sektor",
    sekali di kanal sektornya. Menghitung rekaman lalu menyebutnya "jumlah
    peraturan" akan melebih-lebihkan populasi secara diam-diam.

    Identitas yang dipakai adalah JENIS + nomor ternormalisasi + tahun.
    Rekaman tanpa jenis atau nomor — hampir seluruhnya rancangan — tidak
    dapat diidentifikasi dan dilaporkan terpisah, bukan diam-diam dibuang.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT source, doc_type, number, year FROM inventory").fetchall()

    identified: dict[tuple[str, str, Any], set[str]] = defaultdict(set)
    per_source: dict[str, dict[tuple, int]] = defaultdict(lambda: defaultdict(int))
    unidentified: dict[str, int] = defaultdict(int)

    for r in rows:
        doc_type = (r["doc_type"] or "").strip().upper()
        number = _norm_number(r["number"])
        if not doc_type or not number:
            unidentified[r["source"]] += 1
            continue
        key = (doc_type, number, r["year"])
        identified[key].add(r["source"])
        per_source[r["source"]][key] += 1

    intra_dupes = {
        src: sum(n - 1 for n in keys.values() if n > 1)
        for src, keys in per_source.items()
    }
    return {
        "total_records": len(rows),
        "identifiable_records": len(rows) - sum(unidentified.values()),
        "unidentifiable_records": dict(unidentified),
        "unique_regulations": len(identified),
        "listed_in_multiple_sources": sum(
            1 for srcs in identified.values() if len(srcs) > 1),
        "intra_source_duplicate_surplus": {
            k: v for k, v in intra_dupes.items() if v},
    }


def harvest_funnel(conn: sqlite3.Connection) -> dict[str, Any]:
    """Corong dari "terdaftar" sampai "tersimpan di knowledge base".

    Tiga angka yang sering tertukar dipisahkan di sini: rekaman yang
    terdaftar, rekaman yang *dapat* diunduh (punya tautan dokumen), dan
    rekaman yang sudah benar-benar menjadi berkas di knowledge base. Persen
    kemajuan panen hanya bermakna bila penyebutnya yang tengah, sebab
    rekaman tanpa tautan tidak akan pernah bisa diunduh.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT source, COUNT(*) listed,"
        " SUM(CASE WHEN COALESCE(TRIM(document_url), '') <> '' THEN 1 ELSE 0 END) harvestable,"
        " SUM(CASE WHEN COALESCE(TRIM(doc_id), '') <> '' THEN 1 ELSE 0 END) harvested"
        " FROM inventory GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()

    per_source = []
    for r in rows:
        listed, able, done = int(r["listed"]), int(r["harvestable"]), int(r["harvested"])
        per_source.append({
            "source": r["source"], "listed": listed,
            "harvestable": able, "harvested": done,
            "no_document_url": listed - able,
            "harvest_rate": round(done / able, 4) if able else 0.0,
        })
    totals = {
        k: sum(s[k] for s in per_source)
        for k in ("listed", "harvestable", "harvested", "no_document_url")
    }
    totals["harvest_rate"] = (
        round(totals["harvested"] / totals["harvestable"], 4)
        if totals["harvestable"] else 0.0)
    return {"per_source": per_source, "totals": totals}
