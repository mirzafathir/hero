"""Deterministic summary and Key Takeaways."""
from hero.extract.structure import parse_structure
from hero.extract.summary import build_summary, it_relevance, summarize
from hero.extract.metadata import extract_metadata
from hero.models import PageText

TEXT = """Pasal 1
Dalam Peraturan ini Sistem Elektronik adalah rangkaian perangkat teknologi
informasi yang digunakan untuk memproses data.

Pasal 2
(1) Bank wajib menyampaikan laporan paling lambat 10 (sepuluh) hari kerja.
(2) Bank dilarang mengalihkan tanggung jawab keamanan siber kepada pihak lain.
(3) Bank yang melanggar dikenakan sanksi administratif berupa denda.
"""


def struct():
    return parse_structure([PageText(number=1, text=TEXT, source="text-layer")])


def test_summary_is_non_empty_and_extractive():
    out = summarize(TEXT, max_sentences=3)
    assert out
    # Extractive: every clause in the summary is lifted verbatim from the text.
    normalised = " ".join(TEXT.split())
    for fragment in out.split(" (") [0].split(". "):
        fragment = fragment.strip().rstrip(".")
        if len(fragment) > 25:
            assert fragment in normalised


def test_key_takeaways_are_categorised_and_anchored():
    payload = build_summary(TEXT, struct(), extract_metadata(TEXT))
    takeaways = payload["key_takeaways"]
    assert takeaways
    by_cat = {t["category"] for t in takeaways}
    assert {"Kewajiban", "Larangan", "Sanksi"} & by_cat
    # Every takeaway points back at the pasal it came from.
    assert all(t["pasal"] and t["pasal"].startswith("Pasal") for t in takeaways)


def test_it_relevance_flags_technology_regulations():
    assert it_relevance(TEXT)["level"] in ("medium", "high")
    assert it_relevance("ketentuan mengenai setoran modal tunai")["level"] == "none"


def test_statistics_match_the_parsed_structure():
    payload = build_summary(TEXT, struct(), extract_metadata(TEXT))
    assert payload["statistics"]["article_count"] == 2
    assert payload["statistics"]["ayat_count"] == 3
    assert payload["mode"] == "deterministic"


def test_empty_document_does_not_crash():
    payload = build_summary("", parse_structure([]), extract_metadata(""))
    assert payload["summary"] == ""
    assert payload["key_takeaways"] == []
