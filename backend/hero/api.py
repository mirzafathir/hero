"""Small programmatic API — the Python-side twin of the `hero scrape` CLI.

Lets a notebook or another script pass websites in directly, without editing
``config/sources.yaml`` first:

    from hero import scrape_urls

    run = scrape_urls("https://ojk.go.id/id/regulasi/default.aspx",
                      follow=["/regulasi/Pages/"], exclude=["abstrak"],
                      limit=5)
    print(run.ingested, "documents ingested")

    # JDIH register, with its two dynamic axes:
    run = scrape_urls(sektor=["01", "02"], jenis=["06"], limit=10)
    for rec in run.records:
        print(rec.status, rec.metadata.number, rec.metadata.subject)
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from hero.config import Settings, SiteSource, load_settings
from hero.pipeline import IngestPipeline, RunSummary

JDIH_INDEX = "https://jdih.ojk.go.id/Web/ViewPeraturan/Index"


def build_site(
    url: str,
    name: str = "ad-hoc",
    *,
    follow: Sequence[str] | None = None,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    category: str | None = None,
    adapter: str = "auto",
    sektor: Sequence[str] | None = None,
    jenis: Sequence[str] | None = None,
    max_pages: int = 2,
    max_documents: int = 25,
) -> SiteSource:
    """Construct a one-off ``SiteSource`` without touching the config file."""
    return SiteSource(
        name=name, url=url, max_pages=max_pages, max_documents=max_documents,
        follow_patterns=list(follow or []), include_patterns=list(include or []),
        exclude_patterns=list(exclude or []), category_hint=category,
        adapter=adapter, sektor=[str(s) for s in (sektor or [])],
        jenis_peraturan=[str(j) for j in (jenis or [])],
    )


def scrape_urls(
    *urls: str,
    name: str = "ad-hoc",
    follow: Sequence[str] | None = None,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    category: str | None = None,
    adapter: str = "auto",
    sektor: Sequence[str] | None = None,
    jenis: Sequence[str] | None = None,
    limit: int | None = None,
    max_pages: int = 2,
    settings: Settings | None = None,
    config: str | Path | None = None,
    analyze: bool = True,
) -> RunSummary:
    """Scrape one or more websites passed in directly from Python.

    With no ``urls`` but a ``sektor``/``jenis`` given, the JDIH OJK register
    is addressed directly. Storage, OCR and politeness settings still come
    from the config file (or an explicit ``settings``), so ad-hoc runs write
    into the same knowledge base as configured ones.
    """
    cfg = settings or load_settings(config)
    targets: list[str] = list(urls)
    if not targets:
        if not (sektor or jenis):
            raise ValueError(
                "pass at least one URL, or sektor/jenis for the JDIH register")
        targets = [JDIH_INDEX]
        adapter = "jdih_ojk" if adapter == "auto" else adapter

    sites = [
        build_site(
            u, name=name if len(targets) == 1 else f"{name}-{i + 1}",
            follow=follow, include=include, exclude=exclude, category=category,
            adapter=adapter, sektor=sektor, jenis=jenis,
            max_pages=max_pages, max_documents=limit or 25,
        )
        for i, u in enumerate(targets)
    ]

    pipeline = IngestPipeline(cfg, analyze=analyze)
    try:
        return pipeline.run_sites(sites, limit)
    finally:
        pipeline.close()


def scrape_sites(
    sites: Iterable[SiteSource],
    limit: int | None = None,
    settings: Settings | None = None,
    config: str | Path | None = None,
    analyze: bool = True,
) -> RunSummary:
    """Run a list of ``SiteSource`` objects you built yourself."""
    cfg = settings or load_settings(config)
    pipeline = IngestPipeline(cfg, analyze=analyze)
    try:
        return pipeline.run_sites(list(sites), limit)
    finally:
        pipeline.close()
