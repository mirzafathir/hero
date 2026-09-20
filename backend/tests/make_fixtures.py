"""Regenerate the scanned-PDF fixture used by the OCR tests.

Takes a born-digital regulation and rasterises its first pages, producing an
image-only PDF that exercises the OCR fallback exactly like a real scan.

    python tests/make_fixtures.py <source.pdf> [pages]
"""
import sys
from pathlib import Path

import pymupdf

OUT = Path(__file__).parent / "fixtures" / "scanned_pojk_sample.pdf"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src, pages = Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 4
    doc, out = pymupdf.open(src), pymupdf.open()
    for i in range(min(pages, doc.page_count)):
        pix = doc[i].get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
        page = out.new_page(width=doc[i].rect.width, height=doc[i].rect.height)
        page.insert_image(page.rect, stream=pix.tobytes("jpeg", jpg_quality=65))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.save(OUT, deflate=True, garbage=4)
    out.close(); doc.close()
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
