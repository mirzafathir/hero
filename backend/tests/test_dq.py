"""Pengujian lapisan Data Quality.

Setiap pengujian di sini menegakkan satu keputusan analitik yang mudah
tergerus saat kode dirapikan kemudian hari — terutama soal *ruang lingkup*:
angka mutu hanya bermakna bila diukur terhadap penyebut yang benar.
"""
from __future__ import annotations

import sqlite3

import pytest

from hero.dq import (
    ALL_RULES,
    build_scorecard,
    harvest_funnel,
    profile_table,
    reconciliation_coverage,
    run_rules,
    unique_regulations,
)
from hero.dq import scorecard
from hero.dq.report import render_markdown
from hero.dq.rules import KEY_COLUMN, Rule

SCHEMA = """
CREATE TABLE inventory (
    record_key TEXT PRIMARY KEY, source TEXT, title TEXT, number TEXT,
    doc_type TEXT, jenis TEXT, sektor TEXT, category TEXT, year INTEGER,
    status TEXT, status_label TEXT, status_source TEXT, reg_key TEXT,
    detail_url TEXT, document_url TEXT, document_name TEXT, fields_json TEXT,
    attachments_json TEXT, listed_at TEXT, enriched_at TEXT,
    enrich_error TEXT, doc_id TEXT);
CREATE TABLE documents (
    doc_id TEXT PRIMARY KEY, sha256 TEXT, source_type TEXT, source_name TEXT,
    source_ref TEXT, original_filename TEXT, stored_path TEXT, category TEXT,
    size_bytes INTEGER, page_count INTEGER, ocr_pages INTEGER,
    is_scanned INTEGER, status TEXT, reason TEXT, title TEXT, subject TEXT,
    doc_type TEXT, number TEXT, year INTEGER, issued_date TEXT,
    issuing_body TEXT, reg_status TEXT, confidence REAL, metadata_json TEXT,
    ingested_at TEXT, source_key TEXT, status_source TEXT);
CREATE TABLE articles (
    id INTEGER PRIMARY KEY, doc_id TEXT, number TEXT, bab TEXT,
    page INTEGER, text TEXT);
"""


def _inv(conn, key, **over):
    row = {
        "record_key": key, "source": "ojk-regulasi", "title": f"Judul {key}",
        "number": "1/POJK.03/2020", "doc_type": "POJK", "year": 2020,
        "status": "berlaku", "status_source": "jdih", "reg_key": "POJK|1|2020",
        "detail_url": f"https://ojk.go.id/{key}",
        "document_url": f"https://ojk.go.id/{key}.pdf",
        "enriched_at": "2026-09-01T00:00:00",
    }
    row.update(over)
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO inventory ({cols}) VALUES "
                 f"({', '.join('?' * len(row))})", tuple(row.values()))


def _doc(conn, doc_id, **over):
    row = {
        "doc_id": doc_id, "sha256": f"sha-{doc_id}", "source_type": "web",
        "source_name": "OJK — Regulasi", "status": "ingested",
        "title": f"Peraturan {doc_id}", "doc_type": "POJK", "number": "1",
        "year": 2020, "page_count": 10, "ocr_pages": 0, "is_scanned": 0,
        "confidence": 1.0,
    }
    row.update(over)
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO documents ({cols}) VALUES "
                 f"({', '.join('?' * len(row))})", tuple(row.values()))


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return c


@pytest.fixture
def healthy(conn):
    """Katalog kecil yang memenuhi seluruh aturan."""
    for i in range(10):
        _inv(conn, f"r{i}", number=f"{i}/POJK.03/2020", reg_key=f"POJK|{i}|2020")
    for i in range(3):
        _doc(conn, f"d{i}")
        conn.execute(
            "INSERT INTO articles (doc_id, number, text) VALUES (?, ?, ?)",
            (f"d{i}", "Pasal 1", "Isi pasal."))
    conn.commit()
    return conn


# --------------------------------------------------------------- aturan


def test_setiap_aturan_memilih_kolom_kunci_datasetnya(healthy):
    """Pengambilan contoh baris bergantung pada kesepakatan ini.

    ``run_rules`` merakit query contoh dengan menganggap kolom pertama
    ``violating_sql`` adalah kunci datasetnya. Aturan yang memilih kolom lain
    tetap menghasilkan hitungan benar tetapi contoh barisnya kacau — gagal
    yang sulit terlihat, karena angkanya tampak wajar.
    """
    for rule in ALL_RULES:
        cur = healthy.execute(f"SELECT * FROM ({rule.violating_sql}) LIMIT 0")
        assert [d[0] for d in cur.description] == [KEY_COLUMN[rule.dataset]], (
            f"{rule.id} harus memilih kolom {KEY_COLUMN[rule.dataset]}")


def test_katalog_sehat_lulus_semua_aturan(healthy):
    results = run_rules(healthy)
    gagal = [r.rule.id for r in results if r.verdict in ("gagal", "error")]
    assert gagal == []


def test_aturan_menemukan_judul_kosong(conn):
    _inv(conn, "r1")
    _inv(conn, "r2", title="   ")
    conn.commit()
    res = {r.rule.id: r for r in run_rules(conn)}
    assert res["INV-C01"].violating_rows == 1
    assert res["INV-C01"].verdict == "gagal"


def test_rancangan_tanpa_nomor_bukan_pelanggaran(conn):
    """Nomor peraturan baru terbit saat pengesahan.

    Tanpa pengecualian ini, 650 rancangan pada data nyata akan tercatat
    sebagai cacat kelengkapan, dan tim akan mengejar parser yang sebenarnya
    sudah benar.
    """
    _inv(conn, "r1", source="ojk-rancangan", number=None, doc_type="RPOJK",
         status="rancangan", reg_key=None, document_url=None)
    conn.commit()
    res = {r.rule.id: r for r in run_rules(conn)}
    assert res["INV-C02"].scope_rows == 0
    assert res["INV-C02"].violating_rows == 0


def test_duplikat_dalam_satu_sumber_terdeteksi(conn):
    _inv(conn, "r1", number="59/POJK.03/2017", year=2017)
    _inv(conn, "r2", number="59/POJK.03/2017", year=2017,
         detail_url="https://ojk.go.id/lain")
    conn.commit()
    res = {r.rule.id: r for r in run_rules(conn)}
    assert res["INV-U01"].violating_rows == 2


def test_spasi_dalam_nomor_tidak_menyembunyikan_duplikat(conn):
    """``KEP- 430/BL/2012`` dan ``KEP-430/BL/2012`` adalah peraturan yang sama."""
    _inv(conn, "r1", number="KEP-430/BL/2012", doc_type="BL", year=2012)
    _inv(conn, "r2", number="KEP- 430/BL/2012", doc_type="BL", year=2012)
    conn.commit()
    res = {r.rule.id: r for r in run_rules(conn)}
    assert res["INV-U01"].violating_rows == 2


def test_tahun_di_luar_nalar_ditolak(conn):
    _inv(conn, "r1", year=1830)
    conn.commit()
    res = {r.rule.id: r for r in run_rules(conn)}
    assert res["INV-V01"].violating_rows == 1


def test_surat_edaran_tanpa_pasal_bukan_cacat(conn):
    """SEOJK disusun dalam seksi angka Romawi, bukan pasal."""
    _doc(conn, "d1", doc_type="SEOJK")
    conn.commit()
    res = {r.rule.id: r for r in run_rules(conn)}
    assert res["DOC-S01"].scope_rows == 0


def test_pojk_tanpa_pasal_adalah_cacat(conn):
    """Pasal adalah unit pembanding fitur harmonisasi (URD 3.4)."""
    _doc(conn, "d1", doc_type="POJK")
    conn.commit()
    res = {r.rule.id: r for r in run_rules(conn)}
    assert res["DOC-S01"].violating_rows == 1
    assert res["DOC-S01"].verdict == "gagal"


def test_artefak_pengujian_di_kb_terdeteksi(conn):
    _doc(conn, "d1")
    _doc(conn, "d2", source_name="test-upload-regression", source_type="upload")
    conn.commit()
    res = {r.rule.id: r for r in run_rules(conn)}
    assert res["DOC-G01"].violating_rows == 1
    assert res["DOC-G01"].samples[0]["doc_id"] == "d2"


def test_duplikat_sha256_adalah_blocker(conn):
    _doc(conn, "d1", sha256="sama")
    _doc(conn, "d2", sha256="sama")
    conn.commit()
    res = {r.rule.id: r for r in run_rules(conn)}
    assert res["DOC-U01"].violating_rows == 2
    assert res["DOC-U01"].rule.severity == "blocker"


def test_tabel_hilang_dilaporkan_bukan_menggagalkan_seluruh_laporan():
    c = sqlite3.connect(":memory:")
    c.executescript("CREATE TABLE inventory (record_key TEXT, title TEXT,"
                    " source TEXT, number TEXT, doc_type TEXT, year INTEGER,"
                    " status TEXT, status_source TEXT, reg_key TEXT,"
                    " document_url TEXT, enriched_at TEXT, doc_id TEXT);")
    results = run_rules(c)
    assert any(r.error and r.rule.dataset == "documents" for r in results)
    assert any(r.rule.dataset == "inventory" and not r.error for r in results)


def test_rule_menolak_definisi_yang_salah():
    with pytest.raises(ValueError):
        Rule(id="X", dataset="inventory", dimension="completeness",
             severity="katastrofik", description="", violating_sql="",
             scope_sql="", threshold=0.0, rationale="")
    with pytest.raises(ValueError):
        Rule(id="X", dataset="tabel-khayal", dimension="completeness",
             severity="major", description="", violating_sql="",
             scope_sql="", threshold=0.0, rationale="")


# --------------------------------------------------------------- cakupan


def test_cakupan_memakai_semesta_yang_dapat_dicocokkan(conn):
    """Inti temuan analitik proyek ini.

    PBI tidak pernah diregister JDIH OJK, sehingga menghitungnya sebagai
    kegagalan pencocokan membuat cakupan terlihat 50% padahal seluruh
    rekaman yang *dapat* dicocokkan memang sudah cocok.
    """
    _inv(conn, "j1", source="jdih-ojk", doc_type="POJK", reg_key="POJK|1|2020")
    _inv(conn, "r1", doc_type="POJK", status="berlaku")
    _inv(conn, "r2", doc_type="PBI", status="unknown", status_source=None,
         number="7/1/PBI/2005", year=2005, reg_key="PBI|7|2005")
    conn.commit()

    cov = reconciliation_coverage(conn)
    assert cov["total"] == 2
    assert cov["matchable"] == 1
    assert cov["out_of_universe"] == 1
    assert cov["true_coverage"] == 1.0
    assert cov["naive_coverage"] == 0.5
    assert cov["unresolved_addressable"] == 0
    assert cov["balances"] is True


def test_jenis_tidak_terbaca_tetap_masuk_hitungan(conn):
    """``NULL NOT IN (...)`` bernilai NULL di SQL, bukan benar.

    Tanpa COALESCE, rekaman berjenis kosong lenyap dari kedua kategori dan
    penjumlahannya diam-diam tidak lagi seimbang.
    """
    _inv(conn, "j1", source="jdih-ojk", doc_type="POJK")
    _inv(conn, "r1", doc_type=None, status="unknown", status_source=None,
         reg_key=None)
    conn.commit()

    cov = reconciliation_coverage(conn)
    assert cov["unresolved_structural"] == 1
    assert cov["balances"] is True


def test_populasi_unik_menghitung_lintas_sumber(conn):
    """Satu peraturan yang terdaftar di dua sumber tetap satu peraturan."""
    _inv(conn, "j1", source="jdih-ojk", number="5/POJK.03/2021", year=2021)
    _inv(conn, "r1", source="ojk-regulasi", number="5/POJK.03/2021", year=2021)
    _inv(conn, "r2", source="ojk-regulasi", number="6/POJK.03/2021", year=2021)
    conn.commit()

    pop = unique_regulations(conn)
    assert pop["total_records"] == 3
    assert pop["unique_regulations"] == 2
    assert pop["listed_in_multiple_sources"] == 1


def test_corong_panen_memakai_penyebut_yang_dapat_diunduh(conn):
    """Rekaman tanpa tautan tidak pernah bisa diunduh, jadi bukan penyebut."""
    _inv(conn, "r1", doc_id="d1")
    _inv(conn, "r2", doc_id=None)
    _inv(conn, "r3", document_url=None, doc_id=None)
    conn.commit()

    fun = harvest_funnel(conn)["totals"]
    assert fun["listed"] == 3
    assert fun["harvestable"] == 2
    assert fun["harvested"] == 1
    assert fun["harvest_rate"] == 0.5


# --------------------------------------------------------------- profil


def test_profil_kolom_mengenali_kunci_dan_konstanta(healthy):
    profiles = {p.name: p for p in profile_table(healthy, "inventory")}
    assert profiles["record_key"].is_unique
    assert profiles["source"].is_constant
    assert profiles["title"].missing == 0


def test_profil_menganggap_spasi_sebagai_kosong(conn):
    """Scraper menulis NULL dan string kosong untuk hal yang sama."""
    _inv(conn, "r1", number="   ")
    _inv(conn, "r2", number=None)
    conn.commit()
    profiles = {p.name: p for p in profile_table(conn, "inventory")}
    assert profiles["number"].missing == 2


def test_profil_menolak_tabel_yang_tidak_ada(conn):
    with pytest.raises(ValueError):
        profile_table(conn, "tabel_khayal")


# --------------------------------------------------------------- kartu skor


def test_indikator_fase1_tidak_menghitung_artefak_uji(conn):
    """Indikator menanyakan berapa peraturan terkumpul, bukan berapa baris."""
    for i in range(3):
        _doc(conn, f"real{i}", source_name=f"Situs {i}")
    _doc(conn, "t1", source_name="JDIH test")
    _doc(conn, "t2", source_name="api-test")
    _doc(conn, "junk", source_name="OJK — Regulasi", doc_type=None, year=None)
    conn.commit()

    card = build_scorecard(conn)
    docs = next(i for i in card.phase1["indicators"] if "≥20" in i["indikator"])
    assert card.phase1["documents_raw_ingested"] == 6
    assert docs["aktual"] == 3
    assert docs["tercapai"] is False


def test_nama_jalur_masuk_sesuai_yang_ditulis_pipeline():
    """Indikator Fase 1 memakai nilai ``source_type`` yang persis.

    Versi pertama modul ini menebak ``'folder'`` padahal pipeline menulis
    ``'local_folder'``, sehingga indikator folder terbaca nol walau tiga
    dokumen sudah masuk — salah baca yang tidak menimbulkan galat apa pun.
    Pengujian ini membaca kode pipeline agar penggantian nama di sana
    langsung terlihat di sini.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "hero" / "pipeline.py"
    body = source.read_text(encoding="utf-8")
    for route in (scorecard.ROUTE_WEB, scorecard.ROUTE_UPLOAD,
                  scorecard.ROUTE_LOCAL_FOLDER, scorecard.ROUTE_ONEDRIVE):
        assert f'"{route}"' in body, f"pipeline tidak lagi memakai '{route}'"


def test_folder_lokal_dan_onedrive_dinilai_terpisah(conn):
    """URD meminta keduanya; menggabungkannya menyembunyikan yang belum ada."""
    _doc(conn, "f1", source_type="local_folder", source_name="folder-lokal")
    conn.commit()

    card = build_scorecard(conn)
    by_name = {i["indikator"]: i for i in card.phase1["indicators"]}
    lokal = by_name["Sistem membaca minimal 1 folder lokal"]
    onedrive = by_name["Sistem membaca minimal 1 folder OneDrive public"]
    assert lokal["tercapai"] is True
    assert onedrive["tercapai"] is False


def test_kegagalan_blocker_menentukan_kesimpulan(conn):
    """Skor rata-rata tinggi tidak boleh menutupi pelanggaran jaminan inti."""
    for i in range(200):
        _inv(conn, f"r{i}", number=f"{i}/POJK.03/2020", reg_key=f"POJK|{i}|2020")
    _inv(conn, "rusak", title="", number="999/POJK.03/2020",
         reg_key="POJK|999|2020")
    conn.commit()

    card = build_scorecard(conn)
    assert card.overall_score > 0.95
    assert card.verdict == "gagal"
    assert [r.rule.id for r in card.blocking_failures] == ["INV-C01"]


def test_kartu_skor_dapat_dirangkai_menjadi_markdown(healthy):
    card = build_scorecard(healthy)
    md = render_markdown(card)
    assert "# Laporan Mutu Data HERO" in md
    assert "Skor per dimensi" in md
    assert "Indikator keberhasilan Fase 1" in md


def test_kartu_skor_dapat_diserialisasi(healthy):
    import json

    card = build_scorecard(healthy)
    payload = json.loads(json.dumps(card.to_dict(), ensure_ascii=False, default=str))
    assert payload["verdict"] == "lulus"
    assert len(payload["rules"]) == len(ALL_RULES)
