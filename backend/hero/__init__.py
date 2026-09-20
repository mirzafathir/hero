"""HERO — Harmonisasi & Analisa Regulasi Otomatis.

Baseline implementation of URD feature 3.2 (Scraping & Pengumpulan Dokumen)
plus the PDF/OCR extraction layer that feeds features 3.3–3.5.

Websites can be registered in ``config/sources.yaml`` or passed in directly:

    from hero import scrape_urls
    run = scrape_urls("https://ojk.go.id/id/regulasi/default.aspx", limit=5)
"""

__version__ = "0.1.0"


def __getattr__(name):
    # Imported lazily so `import hero` stays cheap for the CLI's --help path.
    if name in ("scrape_urls", "scrape_sites", "build_site"):
        from hero import api
        return getattr(api, name)
    raise AttributeError(f"module 'hero' has no attribute {name!r}")


__all__ = ["scrape_urls", "scrape_sites", "build_site", "__version__"]
