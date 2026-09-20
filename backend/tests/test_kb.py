"""Classification, knowledge-base placement and the catalog."""
from pathlib import Path

from hero.kb.catalog import Catalog
from hero.kb.classify import classify, kb_relative_path
from hero.models import IngestRecord, RegulationMetadata


def md(**kw) -> RegulationMetadata:
    return RegulationMetadata(**kw)


def test_classify_prefers_the_subject_over_the_body():
    meta = md(subject="PENYELENGGARAAN TEKNOLOGI INFORMASI OLEH BANK UMUM")
    category, hits = classify(meta, text="bank umum " * 50)
    assert category == "teknologi-informasi"
    assert hits


def test_classify_falls_back_to_body_text():
    category, _ = classify(md(subject="KETENTUAN LAIN"),
                           text="pasar modal emiten bursa efek " * 5)
    assert category == "pasar-modal"


def test_classify_unmatched_goes_to_lain_lain():
    category, hits = classify(md(subject="ABCDEF"), text="xyz")
    assert category == "lain-lain"
    assert hits == []


def test_configured_hint_wins():
    category, hits = classify(md(subject="BANK UMUM"), hint="Rancangan Regulasi")
    assert category == "rancangan-regulasi"
    assert hits == ["configured hint"]


def test_kb_path_layout_is_source_category_type_year_status():
    meta = md(doc_type="POJK", number="9 Tahun 2026", year=2026,
              subject="PELAPORAN INSIDENTAL", status="berlaku")
    path = kb_relative_path(meta, "pasar-modal", "raw.pdf", source="jdih-ojk")
    assert path.parts[:5] == ("jdih-ojk", "pasar-modal", "pojk", "2026", "berlaku")
    assert path.suffix == ".pdf"
    assert "pelaporan-insidental" in path.stem


def test_kb_path_status_folders():
    for status, folder in (("dicabut", "dicabut"), ("diubah", "diubah"),
                           ("rancangan", "rancangan"),
                           ("unknown", "status-tidak-diketahui"),
                           (None, "status-tidak-diketahui")):
        meta = md(doc_type="POJK", year=2020, status=status or "unknown")
        assert kb_relative_path(meta, "perbankan", "x.pdf", "ojk-regulasi").parts[4] == folder


def test_kb_path_without_metadata_uses_filename():
    path = kb_relative_path(md(), "lain-lain", "Dokumen Tanpa Judul.pdf")
    assert path.parts[:5] == ("lainnya", "lain-lain", "tanpa-jenis", "tanpa-tahun",
                              "status-tidak-diketahui")
    assert path.stem == "dokumen-tanpa-judul"


def record(sha: str, **kw) -> IngestRecord:
    base = dict(
        doc_id=sha[:12], sha256=sha, source_type="web", source_name="s",
        source_ref="https://x/y.pdf", original_filename="y.pdf",
        status="ingested", metadata=md(doc_type="POJK", number="1 Tahun 2026",
                                       year=2026, subject="TENTANG X"),
    )
    base.update(kw)
    return IngestRecord(**base)


def test_catalog_roundtrip_and_dedupe(tmp_path):
    with Catalog(tmp_path / "c.db") as cat:
        rec = record("a" * 64, category="pasar-modal")
        cat.upsert_document(rec)
        assert cat.exists("a" * 64) is not None
        assert cat.exists("b" * 64) is None

        row = cat.get(rec.doc_id)
        assert row["doc_type"] == "POJK"
        assert row["category"] == "pasar-modal"

        # Re-ingesting identical content must not create a second row.
        cat.upsert_document(record("a" * 64, category="pasar-modal"))
        assert cat.stats()["total_documents"] == 1


def test_catalog_full_text_search(tmp_path):
    with Catalog(tmp_path / "c.db") as cat:
        rec = record("c" * 64)
        cat.upsert_document(rec)
        cat.save_text(rec.doc_id,
                      "Pelapor wajib menyampaikan Laporan Insidental daring.")
        hits = cat.search("Insidental")
        assert len(hits) == 1
        assert hits[0]["doc_id"] == rec.doc_id
        assert "Insidental" in hits[0]["snippet"]


def test_search_survives_invalid_fts_syntax(tmp_path):
    with Catalog(tmp_path / "c.db") as cat:
        rec = record("d" * 64)
        cat.upsert_document(rec)
        cat.save_text(rec.doc_id, "laporan bulanan perusahaan pembiayaan")
        # Unbalanced quote would raise straight from FTS5.
        assert cat.search('laporan "bulanan') is not None


def test_stats_group_by(tmp_path):
    with Catalog(tmp_path / "c.db") as cat:
        cat.upsert_document(record("e" * 64, category="perbankan"))
        cat.upsert_document(record("f" * 64, category="pasar-modal",
                                   source_type="upload"))
        stats = cat.stats()
        assert stats["total_documents"] == 2
        assert stats["by_category"]["perbankan"] == 1
        assert stats["by_source"]["upload"] == 1
