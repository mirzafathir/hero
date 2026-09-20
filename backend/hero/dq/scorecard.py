"""Kartu skor mutu data dan indikator penerimaan Fase 1.

Dua hal digabung di sini karena keduanya menjawab pertanyaan yang sama dari
sisi berbeda: *apakah fondasi data Fase 1 sudah layak dipakai fitur
berikutnya?*

Skor per dimensi merangkum aturan mutu menjadi angka yang dapat dipantau
antar sprint. Indikator Fase 1 menerjemahkan target URD bagian 5 menjadi
ukuran yang dihitung dari database — termasuk yang paling mudah disalahbaca,
"minimal 20 dokumen peraturan masuk knowledge base". Berkas yang mengendap
dari sesi pengujian tidak dihitung, sebab indikator itu menanyakan berapa
peraturan yang terkumpul, bukan berapa baris yang ada di tabel.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from hero.dq.coverage import harvest_funnel, reconciliation_coverage, unique_regulations
from hero.dq.rules import (
    ALL_RULES,
    SEVERITY_WEIGHT,
    TEST_SOURCE_PATTERNS,
    Rule,
    RuleResult,
    run_rules,
)

DIMENSIONS = ("completeness", "validity", "uniqueness",
              "consistency", "timeliness", "accuracy")

# Nilai ``documents.source_type`` yang ditulis pipeline untuk tiap jalur
# masuk URD 3.2. Nilainya harus sama persis dengan yang dipakai
# ``hero.pipeline``; menebak nama yang "masuk akal" di sini pernah membuat
# indikator folder terbaca nol padahal tiga dokumen sudah masuk.
ROUTE_WEB = "web"
ROUTE_UPLOAD = "upload"
ROUTE_LOCAL_FOLDER = "local_folder"
ROUTE_ONEDRIVE = "onedrive"

# Ambang indikator Fase 1, dari URD bagian 5 (tabel fase & indikator).
PHASE1_MIN_SITES = 3
PHASE1_MIN_DOCUMENTS = 20


@dataclass
class DimensionScore:
    """Ringkasan satu dimensi mutu."""

    dimension: str
    score: float
    rules_total: int
    passed: int
    warned: int
    failed: int
    errored: int

    @property
    def verdict(self) -> str:
        if self.errored:
            return "error"
        if self.failed:
            return "gagal"
        if self.warned:
            return "perhatian"
        return "lulus"


@dataclass
class Scorecard:
    """Seluruh hasil pengukuran mutu pada satu titik waktu."""

    results: list[RuleResult]
    dimensions: list[DimensionScore]
    overall_score: float
    blocking_failures: list[RuleResult] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    funnel: dict[str, Any] = field(default_factory=dict)
    population: dict[str, Any] = field(default_factory=dict)
    phase1: dict[str, Any] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        """Satu kata untuk keadaan mutu data keseluruhan.

        Kegagalan aturan ``blocker`` langsung menentukan hasil, berapa pun
        skor rata-ratanya: data yang melanggar satu jaminan inti tidak
        menjadi layak pakai hanya karena kolom lain rapi.
        """
        if any(r.verdict == "error" for r in self.results):
            return "error"
        if self.blocking_failures:
            return "gagal"
        if any(r.verdict == "gagal" for r in self.results):
            return "perlu-perbaikan"
        if any(r.verdict == "perhatian" for r in self.results):
            return "perhatian"
        return "lulus"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "overall_score": round(self.overall_score, 4),
            "dimensions": [
                {"dimension": d.dimension, "score": round(d.score, 4),
                 "verdict": d.verdict, "rules": d.rules_total,
                 "passed": d.passed, "warned": d.warned,
                 "failed": d.failed, "errored": d.errored}
                for d in self.dimensions
            ],
            "rules": [
                {"id": r.rule.id, "dataset": r.rule.dataset,
                 "dimension": r.rule.dimension, "severity": r.rule.severity,
                 "description": r.rule.description, "verdict": r.verdict,
                 "scope_rows": r.scope_rows, "violating_rows": r.violating_rows,
                 "rate": round(r.rate, 4), "threshold": r.rule.threshold,
                 "error": r.error, "samples": r.samples}
                for r in self.results
            ],
            "reconciliation_coverage": self.coverage,
            "harvest_funnel": self.funnel,
            "population": self.population,
            "phase1": self.phase1,
        }


def _test_artifact_clause(column: str = "source_name") -> str:
    return " OR ".join(
        f"lower(COALESCE({column}, '')) LIKE '%{p}%'" for p in TEST_SOURCE_PATTERNS)


def phase1_indicators(conn: sqlite3.Connection) -> dict[str, Any]:
    """Ukur keempat indikator keberhasilan Fase 1 dari isi database.

    Setiap indikator dilaporkan beserta cara hitungnya, supaya angkanya
    dapat diperiksa ulang oleh orang lain dan tidak perlu dipercaya begitu
    saja pada saat sprint review.
    """
    conn.row_factory = sqlite3.Row
    test_clause = _test_artifact_clause()

    def route_count(route: str) -> int:
        return int(conn.execute(
            f"SELECT COUNT(*) FROM documents WHERE source_type = ?"
            f" AND NOT ({test_clause})", (route,)).fetchone()[0])

    sites = [dict(r) for r in conn.execute(
        f"SELECT COALESCE(NULLIF(TRIM(source_name), ''), '(tanpa nama)') site,"
        f" COUNT(*) n FROM documents WHERE source_type = ?"
        f" AND NOT ({test_clause}) GROUP BY 1 ORDER BY 2 DESC", (ROUTE_WEB,))]
    uploads = route_count(ROUTE_UPLOAD)
    local_folders = route_count(ROUTE_LOCAL_FOLDER)
    onedrive = route_count(ROUTE_ONEDRIVE)
    folders = [dict(r) for r in conn.execute(
        f"SELECT source_type, COALESCE(NULLIF(TRIM(source_name), ''), '(tanpa nama)') src,"
        f" COUNT(*) n FROM documents WHERE source_type IN (?, ?)"
        f" AND NOT ({test_clause}) GROUP BY 1, 2",
        (ROUTE_LOCAL_FOLDER, ROUTE_ONEDRIVE))]
    # Jalur masuk yang benar-benar dipakai, dihitung dari data. Dipakai untuk
    # memeriksa bahwa keempat nama jalur di atas masih sesuai kenyataan.
    observed_routes = {
        r["source_type"]: int(r["n"]) for r in conn.execute(
            "SELECT source_type, COUNT(*) n FROM documents GROUP BY 1")}

    # Sebuah baris baru dihitung sebagai "dokumen peraturan" bila ia berhasil
    # di-ingest, bukan artefak pengujian, dan identitas hukumnya terbaca.
    # Tanpa syarat terakhir, berkas apa pun yang berformat PDF — termasuk
    # dokumen requirement proyek ini sendiri — akan ikut terhitung.
    genuine = int(conn.execute(
        f"SELECT COUNT(*) FROM documents WHERE status = 'ingested'"
        f" AND NOT ({test_clause})"
        f" AND COALESCE(TRIM(doc_type), '') <> ''"
        f" AND COALESCE(year, 0) > 0").fetchone()[0])
    raw_total = int(conn.execute(
        "SELECT COUNT(*) FROM documents WHERE status = 'ingested'").fetchone()[0])
    test_rows = int(conn.execute(
        f"SELECT COUNT(*) FROM documents WHERE {test_clause}").fetchone()[0])

    def indicator(name: str, actual: int, target: int, how: str) -> dict[str, Any]:
        return {"indikator": name, "aktual": actual, "target": target,
                "tercapai": actual >= target, "cara_hitung": how}

    return {
        "indicators": [
            indicator(
                "Dokumen tertarik dari ≥3 situs sumber yang diinput manual",
                len(sites), PHASE1_MIN_SITES,
                "COUNT(DISTINCT source_name) pada documents dengan "
                "source_type='web', tidak termasuk nama bernuansa pengujian"),
            indicator(
                "Fitur unggah manual berfungsi untuk dokumen PDF",
                uploads, 1,
                "COUNT(*) pada documents dengan source_type='upload'"),
            indicator(
                "Sistem membaca minimal 1 folder lokal",
                local_folders, 1,
                "COUNT(*) pada documents dengan source_type='local_folder'"),
            indicator(
                "Sistem membaca minimal 1 folder OneDrive public",
                onedrive, 1,
                "COUNT(*) pada documents dengan source_type='onedrive'"),
            indicator(
                "≥20 dokumen peraturan di knowledge base (mode Deterministik)",
                genuine, PHASE1_MIN_DOCUMENTS,
                "documents berstatus 'ingested', bukan artefak pengujian, "
                "dan identitas hukumnya (jenis + tahun) terbaca"),
        ],
        "sites": sites,
        "folders": folders,
        "observed_routes": observed_routes,
        "documents_raw_ingested": raw_total,
        "documents_genuine": genuine,
        "documents_test_artifacts": test_rows,
    }


def build_scorecard(
    conn: sqlite3.Connection,
    rules: tuple[Rule, ...] | list[Rule] = ALL_RULES,
    *,
    sample_limit: int = 5,
) -> Scorecard:
    """Jalankan seluruh pengukuran dan rangkai menjadi satu kartu skor."""
    results = run_rules(conn, rules, sample_limit=sample_limit)

    dimensions: list[DimensionScore] = []
    for dim in DIMENSIONS:
        subset = [r for r in results if r.rule.dimension == dim]
        if not subset:
            continue
        # Kepatuhan satu aturan adalah bagian baris yang memenuhinya. Dimensi
        # ditimbang menurut keparahan, sehingga satu pelanggaran blocker
        # menekan skor jauh lebih kuat daripada pelanggaran minor.
        weights = [SEVERITY_WEIGHT[r.rule.severity] or 0.5 for r in subset]
        compliance = [1.0 - r.rate if not r.error else 0.0 for r in subset]
        total_w = sum(weights)
        score = (sum(w * c for w, c in zip(weights, compliance)) / total_w
                 if total_w else 1.0)
        dimensions.append(DimensionScore(
            dimension=dim, score=score, rules_total=len(subset),
            passed=sum(1 for r in subset if r.verdict in ("lulus", "kosong")),
            warned=sum(1 for r in subset if r.verdict == "perhatian"),
            failed=sum(1 for r in subset if r.verdict == "gagal"),
            errored=sum(1 for r in subset if r.verdict == "error"),
        ))

    overall = (sum(d.score for d in dimensions) / len(dimensions)
               if dimensions else 1.0)
    blocking = [r for r in results
                if r.rule.severity == "blocker" and r.verdict == "gagal"]

    return Scorecard(
        results=results, dimensions=dimensions, overall_score=overall,
        blocking_failures=blocking,
        coverage=reconciliation_coverage(conn),
        funnel=harvest_funnel(conn),
        population=unique_regulations(conn),
        phase1=phase1_indicators(conn),
    )
