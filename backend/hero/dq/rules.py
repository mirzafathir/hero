"""Aturan kualitas data deklaratif untuk katalog HERO.

Setiap aturan menyatakan satu harapan yang dapat diuji terhadap database,
dan — yang sama pentingnya — *ruang lingkup* di mana harapan itu berlaku.
Ruang lingkup adalah inti modul ini: tanpa itu, kondisi yang memang sudah
begitu sifatnya akan terbaca sebagai kerusakan data.

Contoh yang benar-benar terjadi pada data proyek ini: 650 rancangan peraturan
tidak memiliki nomor. Diukur terhadap seluruh tabel, kolom ``number`` terlihat
kosong 20,3% dan tampak seperti kegagalan parser. Padahal rancangan peraturan
memang belum bernomor sampai ia disahkan — nomor baru terbit bersama
pengesahan. Karena itu aturan kelengkapan nomor mengecualikan sumber
rancangan, sehingga angka yang tersisa benar-benar menunjuk cacat.

Dimensi mengikuti DAMA-DMBOK: completeness, validity, uniqueness,
consistency, timeliness, accuracy.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass, field
from typing import Any

# Jenis peraturan yang benar-benar diregister JDIH OJK. Dihitung dari data,
# bukan diasumsikan — lihat hero.dq.coverage.jdih_universe(). Daftar ini
# dipakai sebagai nilai bawaan saat database belum berisi rekaman JDIH.
JDIH_REGISTERED_TYPES = ("UU", "PADK", "POJK", "SEOJK", "PERPRES")

# Status hukum yang sah dalam sistem.
VALID_STATUSES = ("berlaku", "diubah", "dicabut", "rancangan", "unknown")

# Nama sumber yang menandai artefak pengujian, bukan panen produksi.
TEST_SOURCE_PATTERNS = ("test", "regression", "fixture", "dummy", "sample-")

# Jenis peraturan yang batang tubuhnya disusun dalam Pasal. Surat Edaran
# (SEOJK/SEBI) sengaja tidak masuk: ia memakai seksi angka Romawi, sehingga
# "tidak punya pasal" adalah bentuk normalnya, bukan cacat ekstraksi.
PASAL_BEARING_TYPES = ("POJK", "PBI", "PP", "PADK", "UU", "PERPRES", "PERMEN")

SEVERITY_WEIGHT = {"blocker": 4.0, "major": 2.0, "minor": 1.0, "info": 0.0}

# Kolom kunci tiap dataset. ``violating_sql`` setiap aturan wajib memilih
# tepat kolom ini, supaya pengambilan contoh baris pelanggar dapat dirakit
# secara seragam tanpa tiap aturan menulis query keduanya.
KEY_COLUMN = {
    "inventory": "record_key",
    "documents": "doc_id",
    "articles": "id",
}


@dataclass(frozen=True)
class Rule:
    """Satu harapan yang dapat diuji terhadap katalog.

    ``violating_sql`` memilih baris yang melanggar; ``scope_sql`` memilih
    baris tempat harapan itu berlaku. Tingkat pelanggaran adalah rasio
    keduanya, sehingga aturan tetap bermakna saat volume data bertambah.
    """

    id: str
    dataset: str
    dimension: str
    severity: str
    description: str
    violating_sql: str
    scope_sql: str
    threshold: float
    rationale: str
    sample_columns: tuple[str, ...] = ("record_key",)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_WEIGHT:
            raise ValueError(f"{self.id}: severity tidak dikenal: {self.severity}")
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError(f"{self.id}: threshold harus 0..1, bukan {self.threshold}")
        if self.dataset not in KEY_COLUMN:
            raise ValueError(f"{self.id}: dataset tidak dikenal: {self.dataset}")


@dataclass
class RuleResult:
    """Hasil satu aturan terhadap database nyata."""

    rule: Rule
    scope_rows: int
    violating_rows: int
    samples: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def rate(self) -> float:
        return self.violating_rows / self.scope_rows if self.scope_rows else 0.0

    @property
    def verdict(self) -> str:
        """``lulus`` / ``perhatian`` / ``gagal`` — atau ``error`` bila query gagal.

        ``perhatian`` dipakai saat aturan masih di bawah ambang tetapi sudah
        menyentuh lebih dari separuhnya: itu tanda kondisi sedang memburuk
        dan layak dilihat sebelum benar-benar melewati batas.
        """
        if self.error:
            return "error"
        if self.scope_rows == 0:
            return "kosong"
        if self.rate > self.rule.threshold:
            return "gagal"
        if self.rule.threshold > 0 and self.rate > self.rule.threshold * 0.5:
            return "perhatian"
        return "lulus"


def _types_sql(types: tuple[str, ...]) -> str:
    return ", ".join(f"'{t}'" for t in types)


def _test_name_predicate(column: str = "source_name") -> str:
    like = " OR ".join(
        f"lower({column}) LIKE '%{p}%'" for p in TEST_SOURCE_PATTERNS)
    return f"({like})"


def _build_rules() -> tuple[Rule, ...]:
    jdih = _types_sql(JDIH_REGISTERED_TYPES)
    statuses = _types_sql(VALID_STATUSES)
    pasal = _types_sql(PASAL_BEARING_TYPES)
    next_year = _dt.date.today().year + 1
    inv = "FROM inventory"
    doc = "FROM documents"

    return (
        # ---------------- inventory · completeness ----------------
        Rule(
            id="INV-C01", dataset="inventory", dimension="completeness",
            severity="blocker",
            description="Setiap rekaman inventaris memiliki judul",
            violating_sql=f"SELECT record_key {inv} WHERE COALESCE(TRIM(title), '') = ''",
            scope_sql=f"SELECT record_key {inv}",
            threshold=0.0,
            rationale=(
                "Judul adalah satu-satunya kolom yang selalu ditampilkan di "
                "setiap kanal sumber. Rekaman tanpa judul berarti halaman "
                "daftar gagal terbaca, bukan sekadar field kosong."),
        ),
        Rule(
            id="INV-C02", dataset="inventory", dimension="completeness",
            severity="major",
            description="Peraturan yang sudah disahkan memiliki nomor",
            violating_sql=(
                f"SELECT record_key {inv} WHERE source <> 'ojk-rancangan' "
                "AND COALESCE(TRIM(number), '') = ''"),
            scope_sql=f"SELECT record_key {inv} WHERE source <> 'ojk-rancangan'",
            threshold=0.02,
            rationale=(
                "Rancangan dikecualikan: nomor peraturan baru terbit saat "
                "pengesahan, sehingga rancangan tanpa nomor adalah kondisi "
                "normal. Ambang 2% menampung rekaman lawas era Bapepam yang "
                "penomorannya memang tidak seragam."),
        ),
        Rule(
            id="INV-C03", dataset="inventory", dimension="completeness",
            severity="major",
            description="Rekaman dapat di-harvest (punya tautan dokumen)",
            violating_sql=(
                f"SELECT record_key {inv} WHERE source <> 'ojk-rancangan' "
                "AND COALESCE(TRIM(document_url), '') = ''"),
            scope_sql=f"SELECT record_key {inv} WHERE source <> 'ojk-rancangan'",
            threshold=0.02,
            rationale=(
                "Tanpa tautan dokumen, rekaman tidak akan pernah menjadi PDF "
                "di knowledge base. Rancangan dikecualikan karena 197 di "
                "antaranya hanya terbit dalam format .docx — di luar cakupan "
                "PDF menurut URD 4.2, dan itu keputusan ruang lingkup."),
        ),
        Rule(
            id="INV-C04", dataset="inventory", dimension="completeness",
            severity="minor",
            description="Jenis peraturan terdeteksi",
            violating_sql=(
                f"SELECT record_key {inv} WHERE source <> 'ojk-rancangan' "
                "AND COALESCE(TRIM(doc_type), '') = ''"),
            scope_sql=f"SELECT record_key {inv} WHERE source <> 'ojk-rancangan'",
            threshold=0.05,
            rationale=(
                "Jenis dipakai untuk klasifikasi folder dan pencocokan "
                "lintas sumber. Kegagalan terbesar ada pada Surat Edaran "
                "Bank Indonesia yang judulnya tidak memuat akronim jenis."),
        ),

        # ---------------- inventory · validity ----------------
        Rule(
            id="INV-V01", dataset="inventory", dimension="validity",
            severity="major",
            description=f"Tahun peraturan masuk akal (1945–{next_year})",
            violating_sql=(
                f"SELECT record_key {inv} WHERE year IS NOT NULL AND TRIM(CAST(year AS TEXT)) <> '' "
                f"AND (CAST(year AS INTEGER) < 1945 OR CAST(year AS INTEGER) > {next_year})"),
            scope_sql=(
                f"SELECT record_key {inv} WHERE year IS NOT NULL "
                "AND TRIM(CAST(year AS TEXT)) <> ''"),
            threshold=0.0,
            rationale=(
                "1945 adalah batas bawah perundangan Indonesia. Tahun di luar "
                "rentang ini hampir selalu berarti angka lain — nomor urut "
                "atau potongan tanggal — yang salah terbaca sebagai tahun."),
        ),
        Rule(
            id="INV-V02", dataset="inventory", dimension="validity",
            severity="blocker",
            description="Status hukum berasal dari daftar nilai yang sah",
            violating_sql=f"SELECT record_key {inv} WHERE status NOT IN ({statuses})",
            scope_sql=f"SELECT record_key {inv}",
            threshold=0.0,
            rationale=(
                "Status menentukan penempatan folder knowledge base dan "
                "menjadi penyaring utama fitur harmonisasi: membandingkan "
                "draft terhadap peraturan yang sudah dicabut menghasilkan "
                "temuan yang menyesatkan."),
        ),
        Rule(
            id="INV-V03", dataset="inventory", dimension="validity",
            severity="major",
            description="Format reg_key konsisten: JENIS|nomor|tahun",
            violating_sql=(
                f"SELECT record_key {inv} WHERE COALESCE(reg_key, '') <> '' "
                "AND reg_key NOT GLOB '*|*|*'"),
            scope_sql=f"SELECT record_key {inv} WHERE COALESCE(reg_key, '') <> ''",
            threshold=0.0,
            rationale=(
                "reg_key adalah kunci penghubung antar sumber. Bentuk yang "
                "tidak konsisten akan gagal cocok secara diam-diam, bukan "
                "menimbulkan galat yang terlihat."),
        ),

        # ---------------- inventory · uniqueness ----------------
        Rule(
            id="INV-U01", dataset="inventory", dimension="uniqueness",
            severity="major",
            description="Satu peraturan tercatat sekali per sumber",
            violating_sql=f"""
                SELECT record_key {inv} WHERE COALESCE(TRIM(number), '') <> ''
                AND COALESCE(TRIM(doc_type), '') <> ''
                AND (source, UPPER(REPLACE(number, ' ', '')), doc_type, year) IN (
                    SELECT source, UPPER(REPLACE(number, ' ', '')), doc_type, year
                    FROM inventory
                    WHERE COALESCE(TRIM(number), '') <> ''
                      AND COALESCE(TRIM(doc_type), '') <> ''
                    GROUP BY 1, 2, 3, 4 HAVING COUNT(*) > 1)""",
            scope_sql=(
                f"SELECT record_key {inv} WHERE COALESCE(TRIM(number), '') <> '' "
                "AND COALESCE(TRIM(doc_type), '') <> ''"),
            threshold=0.03,
            rationale=(
                "Satu peraturan dapat muncul di kanal 'semua sektor' sekaligus "
                "di kanal sektornya sendiri, dengan URL detail berbeda. "
                "Duplikat membuat setiap hitungan populasi melebih-lebihkan "
                "jumlah peraturan yang sebenarnya ada."),
        ),

        # ---------------- inventory · consistency ----------------
        Rule(
            id="INV-S01", dataset="inventory", dimension="consistency",
            severity="major",
            description="Status 'unknown' tidak mengklaim punya sumber status",
            violating_sql=(
                f"SELECT record_key {inv} WHERE status = 'unknown' "
                "AND COALESCE(TRIM(status_source), '') <> ''"),
            scope_sql=f"SELECT record_key {inv} WHERE status = 'unknown'",
            threshold=0.0,
            rationale=(
                "Rekaman yang statusnya tidak diketahui tetapi mengaku punya "
                "sumber status berarti rekonsiliasi berhenti separuh jalan "
                "dan meninggalkan jejak yang saling bertentangan."),
        ),
        Rule(
            id="INV-S02", dataset="inventory", dimension="consistency",
            severity="minor",
            description="reg_key terbentuk bila jenis, nomor, dan tahun lengkap",
            violating_sql=(
                f"SELECT record_key {inv} WHERE COALESCE(TRIM(doc_type), '') <> '' "
                "AND COALESCE(TRIM(number), '') <> '' AND COALESCE(year, 0) > 0 "
                "AND COALESCE(reg_key, '') = ''"),
            scope_sql=(
                f"SELECT record_key {inv} WHERE COALESCE(TRIM(doc_type), '') <> '' "
                "AND COALESCE(TRIM(number), '') <> '' AND COALESCE(year, 0) > 0"),
            threshold=0.10,
            rationale=(
                "Bila ketiga bahan tersedia namun kunci tidak terbentuk, "
                "pembentuk kunci tidak mengenali pola penomorannya. Penyebab "
                "terbesar adalah Keputusan Bapepam-LK (KEP-208/BL/2012) yang "
                "diawali huruf, bukan angka. Ambang 10% adalah pengakuan "
                "bahwa peraturan pra-OJK memang bercorak lain."),
        ),

        # ---------------- inventory · timeliness ----------------
        Rule(
            id="INV-T01", dataset="inventory", dimension="timeliness",
            severity="minor",
            description="Halaman detail rekaman sudah pernah dibaca",
            violating_sql=(
                f"SELECT record_key {inv} WHERE COALESCE(TRIM(enriched_at), '') = ''"),
            scope_sql=f"SELECT record_key {inv}",
            threshold=0.02,
            rationale=(
                "Rekaman yang belum diperkaya hanya punya kolom seadanya dari "
                "halaman daftar. Selama angka ini kecil, ia menunjukkan "
                "discovery yang terhenti, bukan cacat data."),
        ),

        # ---------------- inventory · accuracy ----------------
        Rule(
            id="INV-A01", dataset="inventory", dimension="accuracy",
            severity="major",
            description=(
                "Rekaman yang seharusnya dapat dicocokkan ke JDIH "
                "benar-benar memperoleh status"),
            violating_sql=(
                f"SELECT record_key {inv} WHERE source = 'ojk-regulasi' "
                f"AND doc_type IN ({jdih}) AND status = 'unknown'"),
            scope_sql=(
                f"SELECT record_key {inv} WHERE source = 'ojk-regulasi' "
                f"AND doc_type IN ({jdih})"),
            threshold=0.10,
            rationale=(
                "Inilah ukuran rekonsiliasi yang jujur. Menghitung seluruh "
                "1.570 rekaman ojk.go.id sebagai penyebut adalah keliru: PBI, "
                "PMK, KMK, dan Keputusan Bapepam-LK memang tidak pernah "
                "diregister JDIH OJK, karena JDIH OJK adalah register milik "
                "OJK sendiri, bukan basis data hukum nasional. Hanya jenis "
                "yang benar-benar ada di JDIH yang boleh dituntut cocok."),
        ),

        # ---------------- documents · completeness ----------------
        Rule(
            id="DOC-C01", dataset="documents", dimension="completeness",
            severity="blocker",
            description="Dokumen knowledge base memiliki judul",
            violating_sql=f"SELECT doc_id {doc} WHERE COALESCE(TRIM(title), '') = ''",
            scope_sql=f"SELECT doc_id {doc}",
            threshold=0.0,
            rationale=(
                "Judul adalah label yang dipakai pada pencarian dan penyajian "
                "hasil; tanpa itu dokumen praktis tidak dapat ditemukan lagi."),
            sample_columns=("doc_id", "stored_path"),
        ),
        Rule(
            id="DOC-C02", dataset="documents", dimension="completeness",
            severity="major",
            description="Identitas peraturan lengkap (jenis, nomor, tahun)",
            violating_sql=(
                f"SELECT doc_id {doc} WHERE COALESCE(TRIM(doc_type), '') = '' "
                "OR COALESCE(TRIM(number), '') = '' OR COALESCE(year, 0) = 0"),
            scope_sql=f"SELECT doc_id {doc} WHERE status = 'ingested'",
            threshold=0.15,
            rationale=(
                "Ketiga field ini membentuk identitas yang dipakai fitur "
                "harmonisasi untuk merujuk peraturan secara eksplisit "
                "(URD 3.4). Ambang longgar karena knowledge base saat ini "
                "masih memuat berkas non-peraturan dari sesi pengujian."),
            sample_columns=("doc_id", "title"),
        ),

        # ---------------- documents · validity ----------------
        Rule(
            id="DOC-V01", dataset="documents", dimension="validity",
            severity="major",
            description="Skor keyakinan ekstraksi berada di rentang 0..1",
            violating_sql=(
                f"SELECT doc_id {doc} WHERE confidence IS NOT NULL "
                "AND (confidence < 0 OR confidence > 1)"),
            scope_sql=f"SELECT doc_id {doc} WHERE confidence IS NOT NULL",
            threshold=0.0,
            rationale=(
                "Skor di luar rentang menandakan dua skala tercampur — "
                "persen OCR (0–100) dan keyakinan metadata (0–1) — sehingga "
                "penyaringan mutu akan meloloskan dokumen yang buruk."),
            sample_columns=("doc_id", "title"),
        ),
        Rule(
            id="DOC-V02", dataset="documents", dimension="validity",
            severity="blocker",
            description="Dokumen yang berhasil di-ingest memiliki halaman",
            violating_sql=(
                f"SELECT doc_id {doc} WHERE status = 'ingested' "
                "AND COALESCE(page_count, 0) <= 0"),
            scope_sql=f"SELECT doc_id {doc} WHERE status = 'ingested'",
            threshold=0.0,
            rationale=(
                "PDF nol halaman yang dinyatakan berhasil berarti kegagalan "
                "ekstraksi lolos tanpa terdeteksi — persis jenis kegagalan "
                "diam yang dilarang URD 7.1."),
            sample_columns=("doc_id", "title"),
        ),

        # ---------------- documents · uniqueness ----------------
        Rule(
            id="DOC-U01", dataset="documents", dimension="uniqueness",
            severity="blocker",
            description="Tidak ada isi berkas yang sama tersimpan dua kali",
            violating_sql=f"""
                SELECT doc_id {doc} WHERE sha256 IN (
                    SELECT sha256 FROM documents WHERE COALESCE(sha256, '') <> ''
                    GROUP BY sha256 HAVING COUNT(*) > 1)""",
            scope_sql=f"SELECT doc_id {doc} WHERE COALESCE(sha256, '') <> ''",
            threshold=0.0,
            rationale=(
                "Deduplikasi SHA-256 adalah jaminan inti pipeline ingest. "
                "Bila ia bocor, satu peraturan akan terhitung berkali-kali "
                "pada setiap statistik di hilirnya."),
            sample_columns=("doc_id", "sha256", "title"),
        ),

        # ---------------- documents · consistency ----------------
        Rule(
            id="DOC-S01", dataset="documents", dimension="consistency",
            severity="major",
            description="Peraturan berbentuk pasal menghasilkan pasal terparsing",
            violating_sql=f"""
                SELECT doc_id {doc} WHERE status = 'ingested'
                AND doc_type IN ({pasal})
                AND doc_id NOT IN (SELECT DISTINCT doc_id FROM articles)""",
            scope_sql=(
                f"SELECT doc_id {doc} WHERE status = 'ingested' "
                f"AND doc_type IN ({pasal})"),
            threshold=0.05,
            rationale=(
                "Surat Edaran sengaja tidak termasuk: ia disusun dalam seksi "
                "angka Romawi, sehingga nol pasal adalah bentuk normalnya. "
                "Untuk POJK atau UU, nol pasal berarti parser struktur gagal "
                "— dan pasal adalah unit pembanding fitur harmonisasi, "
                "sehingga dokumen itu tidak dapat dipakai sama sekali."),
            sample_columns=("doc_id", "doc_type", "title"),
        ),
        Rule(
            id="DOC-S02", dataset="documents", dimension="consistency",
            severity="minor",
            description="Dokumen hasil scan mencatat halaman OCR",
            violating_sql=(
                f"SELECT doc_id {doc} WHERE is_scanned = 1 "
                "AND COALESCE(ocr_pages, 0) = 0"),
            scope_sql=f"SELECT doc_id {doc} WHERE is_scanned = 1",
            threshold=0.0,
            rationale=(
                "Dokumen ditandai hasil scan namun tidak ada halaman yang "
                "melalui OCR berarti teksnya berasal dari sumber lain — "
                "jejak asal-usul teks menjadi tidak dapat dipercaya."),
            sample_columns=("doc_id", "title"),
        ),

        # ---------------- documents · governance ----------------
        Rule(
            id="DOC-G01", dataset="documents", dimension="accuracy",
            severity="major",
            description="Knowledge base bebas dari artefak pengujian",
            violating_sql=(
                f"SELECT doc_id {doc} WHERE {_test_name_predicate()}"),
            scope_sql=f"SELECT doc_id {doc}",
            threshold=0.0,
            rationale=(
                "Berkas dari sesi pengujian yang mengendap di knowledge base "
                "membuat indikator Fase 1 ('minimal 20 dokumen') terpenuhi "
                "di atas kertas tanpa ada peraturan sungguhan yang bertambah. "
                "Pemisahan basis data uji dan produksi adalah syarat agar "
                "angka penerimaan dapat dipertanggungjawabkan."),
            sample_columns=("doc_id", "source_name", "title"),
        ),

        # ---------------- articles ----------------
        Rule(
            id="ART-C01", dataset="articles", dimension="completeness",
            severity="major",
            description="Setiap pasal memiliki isi teks",
            violating_sql=(
                "SELECT id FROM articles WHERE COALESCE(TRIM(text), '') = ''"),
            scope_sql="SELECT id FROM articles",
            threshold=0.0,
            rationale=(
                "Pasal kosong tetap terhitung sebagai unit pembanding pada "
                "fitur harmonisasi, lalu selalu dilaporkan 'tidak ada "
                "pertentangan' — kesimpulan yang terdengar aman padahal "
                "tidak pernah benar-benar diperiksa."),
            sample_columns=("doc_id", "number"),
        ),
    )


ALL_RULES: tuple[Rule, ...] = _build_rules()


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM ({sql})").fetchone()[0])


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def run_rules(
    conn: sqlite3.Connection,
    rules: tuple[Rule, ...] | list[Rule] = ALL_RULES,
    *,
    sample_limit: int = 5,
) -> list[RuleResult]:
    """Jalankan setiap aturan terhadap ``conn`` dan kembalikan hasilnya.

    Aturan yang gagal dieksekusi dicatat sebagai ``error`` dan tidak
    menghentikan aturan lain: satu kolom yang hilang setelah migrasi skema
    tidak boleh membuat seluruh laporan mutu ikut hilang.
    """
    conn.row_factory = sqlite3.Row
    results: list[RuleResult] = []
    for rule in rules:
        if not _table_exists(conn, rule.dataset):
            results.append(RuleResult(
                rule=rule, scope_rows=0, violating_rows=0,
                error=f"tabel '{rule.dataset}' tidak ada"))
            continue
        try:
            scope = _count(conn, rule.scope_sql)
            violating = _count(conn, rule.violating_sql)
            samples: list[dict[str, Any]] = []
            if violating:
                key = KEY_COLUMN[rule.dataset]
                cols = ", ".join(rule.sample_columns)
                rows = conn.execute(
                    f"SELECT {cols} FROM {rule.dataset} WHERE {key} IN "
                    f"({rule.violating_sql}) LIMIT {int(sample_limit)}"
                ).fetchall()
                samples = [dict(r) for r in rows]
            results.append(RuleResult(
                rule=rule, scope_rows=scope,
                violating_rows=violating, samples=samples))
        except sqlite3.Error as exc:  # skema berbeda dari yang diharapkan
            results.append(RuleResult(
                rule=rule, scope_rows=0, violating_rows=0, error=str(exc)))
    return results
