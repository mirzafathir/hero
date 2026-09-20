"""Penyajian hasil pengukuran mutu sebagai Markdown.

Laporan ditulis untuk dibaca manusia yang harus mengambil keputusan —
Product Owner pada sprint review, atau analis unit yang perlu tahu apakah
data boleh dipakai. Karena itu setiap angka disertai alasan mengapa ambangnya
demikian: tanpa itu, pembaca hanya dapat menerima atau menolak angka, tidak
dapat mendebatnya.
"""
from __future__ import annotations

import datetime as _dt

from hero.dq.scorecard import Scorecard

_VERDICT_MARK = {
    "lulus": "✅", "perhatian": "⚠️", "gagal": "❌",
    "error": "🛑", "kosong": "·", "perlu-perbaikan": "⚠️",
}


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_markdown(card: Scorecard, *, title: str = "Laporan Mutu Data HERO") -> str:
    """Rangkai kartu skor menjadi satu dokumen Markdown yang utuh."""
    now = _dt.datetime.now().strftime("%d %B %Y %H:%M")
    out: list[str] = [
        f"# {title}",
        "",
        f"*Dihasilkan otomatis oleh `hero dq --markdown` pada {now}.*",
        "",
        f"**Kesimpulan:** {_VERDICT_MARK.get(card.verdict, '')} "
        f"`{card.verdict}` — skor mutu keseluruhan "
        f"**{_pct(card.overall_score)}**",
        "",
    ]

    if card.blocking_failures:
        out += ["## Pelanggaran yang menghambat", "",
                "Aturan berikut berkeparahan *blocker*: selama masih "
                "dilanggar, data belum layak dipakai fitur di hilirnya.", ""]
        for r in card.blocking_failures:
            out.append(
                f"- **{r.rule.id}** — {r.rule.description}: "
                f"{r.violating_rows} dari {r.scope_rows} baris "
                f"({_pct(r.rate)}) melanggar.")
        out.append("")

    # ---- dimensi ----
    out += ["## Skor per dimensi", "",
            "| Dimensi | Skor | Hasil | Aturan | Lulus | Perhatian | Gagal |",
            "|---|---:|:---:|---:|---:|---:|---:|"]
    for d in card.dimensions:
        out.append(
            f"| {d.dimension} | {_pct(d.score)} | "
            f"{_VERDICT_MARK.get(d.verdict, '')} | {d.rules_total} | "
            f"{d.passed} | {d.warned} | {d.failed} |")
    out.append("")

    # ---- aturan ----
    out += ["## Rincian aturan", "",
            "| ID | Dataset | Dimensi | Keparahan | Pelanggar | Lingkup | "
            "Rasio | Ambang | Hasil |",
            "|---|---|---|---|---:|---:|---:|---:|:---:|"]
    for r in sorted(card.results, key=lambda x: (x.rule.dataset, x.rule.id)):
        out.append(
            f"| {r.rule.id} | {r.rule.dataset} | {r.rule.dimension} | "
            f"{r.rule.severity} | {r.violating_rows} | {r.scope_rows} | "
            f"{_pct(r.rate)} | {_pct(r.rule.threshold)} | "
            f"{_VERDICT_MARK.get(r.verdict, '')} |")
    out.append("")

    problems = [r for r in card.results if r.verdict in ("gagal", "perhatian", "error")]
    if problems:
        out += ["### Aturan yang perlu ditindaklanjuti", ""]
        for r in problems:
            out += [f"#### {r.rule.id} — {r.rule.description}", "",
                    f"- **Hasil:** {_VERDICT_MARK.get(r.verdict, '')} "
                    f"`{r.verdict}` · {r.violating_rows}/{r.scope_rows} "
                    f"({_pct(r.rate)}) · ambang {_pct(r.rule.threshold)}",
                    f"- **Dasar ambang:** {r.rule.rationale}"]
            if r.error:
                out.append(f"- **Galat:** `{r.error}`")
            if r.samples:
                keys = list(r.samples[0])
                out += ["", f"  | {' | '.join(keys)} |",
                        f"  |{'---|' * len(keys)}"]
                for s in r.samples:
                    cells = [str(s[k])[:60].replace("|", "\\|") for k in keys]
                    out.append(f"  | {' | '.join(cells)} |")
            out.append("")

    # ---- cakupan rekonsiliasi ----
    cov = card.coverage
    if cov and cov.get("total"):
        out += [
            "## Cakupan rekonsiliasi status",
            "",
            "Persentase hanya bermakna bila penyebutnya benar. JDIH OJK "
            "adalah register milik OJK sendiri, sehingga hanya jenis yang "
            "memang diregister di sana yang dapat dituntut cocok.",
            "",
            f"- Jenis yang diregister JDIH: `{', '.join(cov['jdih_types'])}`",
            f"- Rekaman ojk.go.id seluruhnya: **{cov['total']}**",
            f"- Di dalam semesta JDIH (dapat dicocokkan): **{cov['matchable']}**",
            f"- Di luar semesta JDIH (mustahil dicocokkan): "
            f"**{cov['out_of_universe']}**",
            "",
            f"| Ukuran | Nilai | Arti |",
            f"|---|---:|---|",
            f"| Cakupan naif | {_pct(cov['naive_coverage'])} | "
            f"terhadap seluruh rekaman — **menyesatkan** |",
            f"| Cakupan sebenarnya | {_pct(cov['true_coverage'])} | "
            f"terhadap rekaman yang dapat dicocokkan |",
            "",
            f"Sisa yang benar-benar layak diperbaiki: "
            f"**{cov['unresolved_addressable']} rekaman**. "
            f"Sisanya, {cov['unresolved_structural']} rekaman, berada di luar "
            f"register JDIH — batas struktural, bukan cacat.",
            "",
        ]
        if cov.get("addressable_breakdown"):
            out += ["| Jenis | Belum berstatus | Di antaranya tanpa reg_key |",
                    "|---|---:|---:|"]
            for b in cov["addressable_breakdown"]:
                out.append(f"| {b['doc_type']} | {b['n']} | {b['tanpa_reg_key']} |")
            out.append("")

    # ---- populasi ----
    pop = card.population
    if pop:
        out += [
            "## Populasi sebenarnya",
            "",
            f"- Rekaman inventaris: **{pop['total_records']}**",
            f"- Peraturan unik (setelah deduplikasi lintas sumber): "
            f"**{pop['unique_regulations']}**",
            f"- Terdaftar di lebih dari satu sumber: "
            f"{pop['listed_in_multiple_sources']}",
        ]
        surplus = pop.get("intra_source_duplicate_surplus") or {}
        if surplus:
            detail = ", ".join(f"{k}: {v}" for k, v in surplus.items())
            out.append(f"- Duplikat di dalam satu sumber (kelebihan baris): {detail}")
        unident = pop.get("unidentifiable_records") or {}
        if unident:
            detail = ", ".join(f"{k}: {v}" for k, v in unident.items())
            out.append(
                f"- Tidak dapat diidentifikasi (tanpa jenis/nomor): {detail} "
                f"— hampir seluruhnya rancangan yang memang belum bernomor")
        out.append("")

    # ---- corong panen ----
    fun = card.funnel
    if fun and fun.get("per_source"):
        t = fun["totals"]
        out += [
            "## Corong panen dokumen",
            "",
            "| Sumber | Terdaftar | Dapat diunduh | Sudah di KB | "
            "Tanpa tautan | Capaian |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for s in fun["per_source"]:
            out.append(
                f"| {s['source']} | {s['listed']} | {s['harvestable']} | "
                f"{s['harvested']} | {s['no_document_url']} | "
                f"{_pct(s['harvest_rate'])} |")
        out += [
            f"| **Total** | **{t['listed']}** | **{t['harvestable']}** | "
            f"**{t['harvested']}** | **{t['no_document_url']}** | "
            f"**{_pct(t['harvest_rate'])}** |",
            "",
            "Capaian dihitung terhadap rekaman yang *dapat* diunduh, bukan "
            "seluruh rekaman: rekaman tanpa tautan dokumen tidak akan pernah "
            "menjadi berkas, sehingga memasukkannya ke penyebut hanya "
            "membuat angka terlihat lebih buruk tanpa menunjuk pekerjaan.",
            "",
        ]

    # ---- indikator fase 1 ----
    ph = card.phase1
    if ph and ph.get("indicators"):
        out += ["## Indikator keberhasilan Fase 1 (URD bagian 5)", "",
                "| Indikator | Aktual | Target | Status |",
                "|---|---:|---:|:---:|"]
        for i in ph["indicators"]:
            mark = "✅" if i["tercapai"] else "❌"
            out.append(
                f"| {i['indikator']} | {i['aktual']} | {i['target']} | {mark} |")
        out += ["", "**Cara hitung tiap indikator:**", ""]
        for i in ph["indicators"]:
            out.append(f"- *{i['indikator']}* — {i['cara_hitung']}")
        raw, genuine = ph["documents_raw_ingested"], ph["documents_genuine"]
        if raw != genuine:
            out += [
                "",
                f"> Catatan penting: tabel `documents` memuat **{raw}** baris "
                f"berstatus ingested, tetapi hanya **{genuine}** yang "
                f"merupakan dokumen peraturan sungguhan. Selisihnya adalah "
                f"artefak sesi pengujian dan berkas non-peraturan. Indikator "
                f"dihitung memakai angka yang kedua.",
            ]
        out.append("")

    return "\n".join(out)
