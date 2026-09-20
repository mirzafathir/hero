from hero.ingest.web import WebScraper, discover_pdf_links
from hero.ingest.folders import scan_folder, iter_pdf_files
from hero.ingest.onedrive import resolve_share_url, download_onedrive_share

__all__ = [
    "WebScraper",
    "discover_pdf_links",
    "scan_folder",
    "iter_pdf_files",
    "resolve_share_url",
    "download_onedrive_share",
]
