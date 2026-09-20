"""Lapisan Data Quality & profiling untuk knowledge base HERO.

Modul ini adalah pekerjaan peran *Data Analyst* pada Fase 1 (URD bagian 5):
mengukur apakah data yang masuk knowledge base layak dipakai sebagai fondasi
fitur 3.3 (analisa), 3.4 (harmonisasi), dan 3.5 (tanggapan PoV).

Tiga kemampuan yang disediakan:

* ``profile``   — profil kolom apa adanya (missing, kardinalitas, sebaran)
* ``rules``     — aturan validasi deklaratif berdimensi DAMA-DMBOK
* ``coverage``  — analisa cakupan: semesta yang benar-benar dapat dicocokkan,
  deduplikasi lintas kanal, dan corong unduhan (listed → harvestable → KB)

Semuanya deterministik (non-AI) sesuai URD 3.1: satu database yang sama
selalu menghasilkan angka yang sama.
"""
from __future__ import annotations

from hero.dq.coverage import (
    harvest_funnel,
    jdih_universe,
    reconciliation_coverage,
    unique_regulations,
)
from hero.dq.profile import ColumnProfile, profile_table
from hero.dq.rules import ALL_RULES, Rule, RuleResult, run_rules
from hero.dq.scorecard import (
    DimensionScore,
    Scorecard,
    build_scorecard,
    phase1_indicators,
)

__all__ = [
    "ALL_RULES",
    "ColumnProfile",
    "DimensionScore",
    "Rule",
    "RuleResult",
    "Scorecard",
    "build_scorecard",
    "harvest_funnel",
    "jdih_universe",
    "phase1_indicators",
    "profile_table",
    "reconciliation_coverage",
    "run_rules",
    "unique_regulations",
]
