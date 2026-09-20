"""End-to-end ingest through the shared pipeline."""
from pathlib import Path

import pymupdf

from hero.pipeline import IngestPipeline

REGULATION = """SALINAN
PERATURAN OTORITAS JASA KEUANGAN
REPUBLIK INDONESIA
NOMOR 11 TAHUN 2026
TENTANG
PENYELENGGARAAN TEKNOLOGI INFORMASI OLEH BANK UMUM

Pasal 1
Dalam Peraturan ini yang dimaksud dengan Sistem Elektronik adalah rangkaian
perangkat teknologi informasi.

Pasal 2
(1) Bank wajib menerapkan manajemen risiko teknologi informasi.
(2) Bank dilarang mengalihkan tanggung jawab sebagaimana dimaksud pada ayat (1).

Ditetapkan di Jakarta
pada tanggal 3 Maret 2026
"""


def write_pdf(path, text=REGULATION):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(50, 50, 545, 780), text, fontsize=10)
    doc.save(path)
    doc.close()
    return path


def test_upload_route_ingests_and_classifies(tmp_path, tmp_settings):
    pdf = write_pdf(tmp_path / "pojk.pdf")
    pipeline = IngestPipeline(tmp_settings)
    try:
        run = pipeline.run_upload([pdf])
    finally:
        pipeline.close()

    assert run.ingested == 1
    rec = run.records[0]
    assert rec.status == "ingested"
    assert rec.metadata.doc_type == "POJK"
    assert rec.metadata.number == "11 Tahun 2026"
    assert rec.category == "teknologi-informasi"
    # Filed under <source>/<category>/<type>/<year>/<status>/ in the KB.
    stored = (tmp_settings.knowledge_base / "unggah-manual" / "teknologi-informasi"
              / "pojk" / "2026" / "berlaku")
    assert stored.is_dir() and list(stored.glob("*.pdf"))


def test_duplicate_content_is_detected_not_restored(tmp_path, tmp_settings):
    a = write_pdf(tmp_path / "a.pdf")
    b = tmp_path / "b.pdf"
    b.write_bytes(a.read_bytes())          # same bytes, different name

    pipeline = IngestPipeline(tmp_settings)
    try:
        first = pipeline.run_upload([a])
        second = pipeline.run_upload([b])
    finally:
        pipeline.close()

    assert first.ingested == 1
    assert second.ingested == 0
    assert second.duplicates == 1
    assert "already stored" in second.records[0].reason
    assert len(list(tmp_settings.knowledge_base.rglob("*.pdf"))) == 1


def test_non_pdf_is_rejected_with_a_reason(tmp_path, tmp_settings):
    bad = tmp_path / "notreally.pdf"
    bad.write_text("plain text pretending to be a PDF")
    pipeline = IngestPipeline(tmp_settings)
    try:
        run = pipeline.run_upload([bad])
    finally:
        pipeline.close()
    assert run.rejected == 1
    assert run.records[0].reason == "not a valid PDF"


def test_analysis_is_stored_alongside_the_document(tmp_path, tmp_settings):
    import json

    pdf = write_pdf(tmp_path / "pojk.pdf")
    pipeline = IngestPipeline(tmp_settings)
    try:
        run = pipeline.run_upload([pdf])
        text_row = pipeline.catalog.get_text(run.records[0].doc_id)
    finally:
        pipeline.close()

    analysis = json.loads(text_row["analysis"])
    assert analysis["mode"] == "deterministic"
    assert analysis["statistics"]["article_count"] == 2
    assert analysis["it_relevance"]["level"] in ("low", "medium", "high")
    # "wajib" / "dilarang" must surface as normative takeaways.
    categories = {k["category"] for k in analysis["key_takeaways"]}
    assert {"Kewajiban", "Larangan"} & categories


def test_folder_route_reports_inaccessible_folder(tmp_settings):
    from hero.config import FolderSource

    pipeline = IngestPipeline(tmp_settings)
    try:
        run = pipeline.run_folders(
            [FolderSource(name="hilang", path="/no/such/folder")])
    finally:
        pipeline.close()
    assert run.records == []
    assert any("does not exist" in e for e in run.errors)


def test_folder_route_ingests_recursively(tmp_path, tmp_settings):
    from hero.config import FolderSource

    nested = tmp_path / "src" / "2026"
    nested.mkdir(parents=True)
    write_pdf(nested / "pojk.pdf")
    (tmp_path / "src" / "empty.pdf").write_bytes(b"")   # cloud placeholder

    pipeline = IngestPipeline(tmp_settings)
    try:
        run = pipeline.run_folders(
            [FolderSource(name="lokal", path=str(tmp_path / "src"))])
    finally:
        pipeline.close()

    assert run.ingested == 1
    assert any("placeholder" in e for e in run.errors)


def test_restructure_moves_legacy_layout_and_is_idempotent(tmp_path, tmp_settings, monkeypatch):
    from hero.kb.catalog import Catalog
    from hero.kb.restructure import restructure

    monkeypatch.chdir(tmp_path)
    pdf = write_pdf(tmp_path / "pojk.pdf")
    pipeline = IngestPipeline(tmp_settings)
    try:
        rec = pipeline.run_upload([pdf]).records[0]
    finally:
        pipeline.close()
    new_path = Path(rec.stored_path)

    # Simulate a file filed under the old <category>/<type>/<year>/ layout.
    legacy = tmp_settings.knowledge_base / "teknologi-informasi" / "pojk" / "2026" / "old.pdf"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    new_path.rename(legacy)
    with Catalog(tmp_settings.catalog_db) as cat:
        cat.update_stored_path(rec.doc_id, str(legacy))

    first = restructure(tmp_settings)
    assert first.moved == 1 and first.missing == 0
    with Catalog(tmp_settings.catalog_db) as cat:
        stored = Path(cat.get(rec.doc_id)["stored_path"])
    assert stored.exists()
    rel = stored.resolve().relative_to(tmp_settings.knowledge_base.resolve())
    assert rel.parts[:5] == ("unggah-manual", "teknologi-informasi", "pojk", "2026", "berlaku")
    assert not legacy.parent.exists()          # emptied legacy folders are removed

    second = restructure(tmp_settings)
    assert second.moved == 0 and second.unchanged == 1


def test_source_override_beats_parsed_year():
    from hero.models import RegulationMetadata

    md = RegulationMetadata(year=2027, number="1 Tahun 2027")
    IngestPipeline._apply_metadata_hint(md, {"year": 2026, "override": ("year",)})
    assert md.year == 2026
    md2 = RegulationMetadata(year=2027)
    IngestPipeline._apply_metadata_hint(md2, {"year": 2026})     # gap-fill only
    assert md2.year == 2027
