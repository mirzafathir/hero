import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def scanned_pdf() -> Path:
    """An image-only PDF (rasterised POJK 9/2026), used for the OCR path."""
    path = FIXTURES / "scanned_pojk_sample.pdf"
    if not path.exists():
        pytest.skip("scanned fixture not generated; run tests/make_fixtures.py")
    return path


@pytest.fixture
def tmp_settings(tmp_path):
    from hero.config import OcrSettings, ScraperSettings, Settings

    return Settings(
        knowledge_base=tmp_path / "kb",
        staging_dir=tmp_path / "staging",
        catalog_db=tmp_path / "catalog.db",
        scraper=ScraperSettings(delay_seconds=0.0, respect_robots=False),
        ocr=OcrSettings(enabled=False),
    )
