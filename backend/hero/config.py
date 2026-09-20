"""Configuration loading for HERO.

Everything the operator can tune lives in a single YAML file so that the
site list (URD 4.1: "scraping dari daftar situs yang diinput manual") can be
edited without touching code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config/sources.yaml")


@dataclass
class SiteSource:
    """One manually-registered website to harvest PDFs from."""

    name: str
    url: str
    enabled: bool = True
    max_pages: int = 1
    max_documents: int = 25
    # Follow links matching these substrings to find more PDF-bearing pages.
    follow_patterns: list[str] = field(default_factory=list)
    # Only keep PDF links whose URL or anchor text matches one of these.
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    category_hint: str | None = None
    # Optional sitemap.xml (or sitemap index) to seed the crawl queue from,
    # in addition to (not instead of) the entry URL above. Still bounded to
    # this one manually-registered site — not a general auto-crawler.
    sitemap_url: str | None = None
    # Re-check anchors without a .pdf extension via a HEAD request when their
    # URL or link text looks like a download (see web.py DOWNLOAD_HINTS).
    sniff_ambiguous_links: bool = True
    # Which harvesting strategy to use: "auto" picks by host, "generic" is
    # the HTML link crawler, "jdih_ojk" drives the AJAX register at
    # jdih.ojk.go.id (its listing page contains no links to crawl).
    adapter: str = "auto"
    # JDIH only — the portal's two dynamic axes. Leave both empty to take
    # whatever the configured URL specifies, or the full published matrix.
    sektor: list[str] = field(default_factory=list)
    jenis_peraturan: list[str] = field(default_factory=list)
    # Legal status every document from this site should carry regardless of
    # what its text says — e.g. "rancangan" for the draft-regulation listing,
    # whose documents are by definition not in force yet.
    status_hint: str | None = None

    def resolved_adapter(self) -> str:
        """Pick the adapter, falling back to host sniffing when set to auto."""
        if self.adapter and self.adapter != "auto":
            return self.adapter
        from urllib.parse import urlparse
        host = (urlparse(self.url).netloc or "").lower()
        if "jdih.ojk.go.id" in host:
            return "jdih_ojk"
        if host in ("ojk.go.id", "www.ojk.go.id") and "/regulasi/" in self.url.lower():
            return "ojk_sharepoint"
        return "generic"


@dataclass
class FolderSource:
    """A local directory, or a mounted/synced OneDrive folder."""

    name: str
    path: str
    enabled: bool = True
    recursive: bool = True
    category_hint: str | None = None


@dataclass
class OneDriveShareSource:
    """A public OneDrive/SharePoint share link."""

    name: str
    share_url: str
    enabled: bool = True
    category_hint: str | None = None
    # Folder inside the shared root to read, e.g. "downloads". The link grants
    # the whole shared folder; this keeps HERO to the one subfolder it needs.
    subfolder: str | None = None
    max_files: int = 100
    # Companion files that sit beside the regulation itself (same rule the
    # web sources apply through exclude_patterns).
    exclude_patterns: list[str] = field(
        default_factory=lambda: ["abstrak", "faq", "matriks"])


@dataclass
class ScraperSettings:
    user_agent: str = "HERO-RegulationBot/0.1 (+capstone; contact: admin@example.org)"
    request_timeout: int = 45
    delay_seconds: float = 1.5      # minimum gap between requests to one host
    respect_robots: bool = True
    max_file_mb: int = 80
    verify_tls: bool = True
    # Resilience: transient failures (timeouts, connection resets, 429/502/
    # 503/504) are retried with exponential backoff instead of giving up on
    # the whole site over one flaky response.
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    # Concurrent PDF downloads once the link list for a site is known. Each
    # host is still individually throttled by delay_seconds, so raising this
    # speeds up multi-host runs without hammering any single server harder.
    max_concurrent_downloads: int = 4
    # Conditional GET (ETag / Last-Modified) cache for listing pages, so a
    # repeat `hero scrape` skips re-downloading pages that have not changed.
    use_page_cache: bool = True


@dataclass
class OcrSettings:
    enabled: bool = True
    languages: str = "ind+eng"
    dpi: int = 300
    # Below this many characters on a page, the page is treated as image-only.
    min_chars_per_page: int = 120
    # If more than this fraction of pages is image-only, OCR the whole document.
    max_pages: int = 60
    # Image-quality pipeline run before Tesseract (orientation fix, deskew,
    # binarize). Turn off only to reproduce the raw-Tesseract baseline.
    preprocess: bool = True
    deskew: bool = True
    # Retry a low-confidence page once with a different page-segmentation
    # mode and keep whichever result scored higher.
    adaptive_retry: bool = True


@dataclass
class PdfSettings:
    # Passwords to try, in order, on an encrypted PDF before giving up.
    # Institutional exports are sometimes batch-protected with one shared
    # password; leave empty to only ever try the blank password.
    candidate_passwords: list[str] = field(default_factory=list)
    # If PyMuPDF cannot open a file at all, try repairing it with pikepdf
    # (rebuilds the cross-reference table) before declaring it unreadable.
    attempt_repair: bool = True


@dataclass
class Settings:
    knowledge_base: Path = Path("data/knowledge_base")
    staging_dir: Path = Path("data/staging")
    catalog_db: Path = Path("data/hero_catalog.db")
    scraper: ScraperSettings = field(default_factory=ScraperSettings)
    ocr: OcrSettings = field(default_factory=OcrSettings)
    pdf: PdfSettings = field(default_factory=PdfSettings)
    sites: list[SiteSource] = field(default_factory=list)
    folders: list[FolderSource] = field(default_factory=list)
    onedrive_shares: list[OneDriveShareSource] = field(default_factory=list)

    @property
    def enabled_sites(self) -> list[SiteSource]:
        return [s for s in self.sites if s.enabled]

    @property
    def enabled_folders(self) -> list[FolderSource]:
        return [f for f in self.folders if f.enabled]

    @property
    def enabled_shares(self) -> list[OneDriveShareSource]:
        return [s for s in self.onedrive_shares if s.enabled]


def _expand(path_str: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path_str)))


def load_settings(path: str | Path | None = None) -> Settings:
    """Read the YAML config, falling back to defaults for anything absent."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    storage = raw.get("storage", {}) or {}
    settings = Settings(
        knowledge_base=_expand(storage.get("knowledge_base", "data/knowledge_base")),
        staging_dir=_expand(storage.get("staging_dir", "data/staging")),
        catalog_db=_expand(storage.get("catalog_db", "data/hero_catalog.db")),
        scraper=ScraperSettings(**(raw.get("scraper", {}) or {})),
        ocr=OcrSettings(**(raw.get("ocr", {}) or {})),
        pdf=PdfSettings(**(raw.get("pdf", {}) or {})),
        sites=[SiteSource(**s) for s in raw.get("sites", []) or []],
        folders=[FolderSource(**f) for f in raw.get("folders", []) or []],
        onedrive_shares=[
            OneDriveShareSource(**s) for s in raw.get("onedrive_shares", []) or []
        ],
    )
    return settings
