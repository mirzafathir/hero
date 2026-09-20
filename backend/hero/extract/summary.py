"""Deterministic summary & Key Takeaways (URD 3.3, mode Deterministik).

Pure rule-based: sentence scoring by term frequency plus obligation-marker
mining. No model calls, so this is the fallback that must always be available.
The AI-Assisted mode of the URD is meant to sit *on top* of this output.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from hero.extract.structure import structure_quality
from hero.models import DocumentStructure, RegulationMetadata

# Indonesian stopwords, trimmed to what actually shows up in regulations.
STOPWORDS = set("""
yang dan di ke dari untuk pada dengan dalam atas oleh sebagai adalah atau
tidak akan telah dapat serta ini itu para bagi karena juga sudah bahwa
kepada terhadap antara setiap secara agar maka jika bila apabila sebagaimana
dimaksud tersebut huruf ayat pasal bab angka nomor tahun dan/atau lain
lainnya melalui tentang setelah sebelum sampai hingga yaitu antara lain
""".split())

# Markers that make a sentence normative rather than descriptive.
# Weights order the categories by legal salience, because one sentence often
# carries several markers ("... wajib menjaga kerahasiaan dan dilarang
# mendistribusikan ..."). A prohibition or a sanction is the more consequential
# reading, so it must outrank the generic obligation it sits beside.
OBLIGATION_MARKERS = {
    "dilarang": ("Larangan", 4.0),
    "tidak diperkenankan": ("Larangan", 4.0),
    "dikenakan sanksi": ("Sanksi", 3.6),
    "sanksi administratif": ("Sanksi", 3.6),
    "pencabutan izin": ("Sanksi", 3.4),
    "dicabut dan dinyatakan tidak berlaku": ("Pencabutan", 3.4),
    "denda": ("Sanksi", 3.2),
    "wajib memperoleh": ("Perizinan", 3.2),
    "wajib menyampaikan": ("Pelaporan", 3.1),
    "wajib": ("Kewajiban", 3.0),
    "menyampaikan laporan": ("Pelaporan", 2.6),
    "paling lambat": ("Batas Waktu", 2.6),
    "paling lama": ("Batas Waktu", 2.6),
    "harus": ("Kewajiban", 2.5),
    "berlaku sejak": ("Masa Berlaku", 2.4),
    "mulai berlaku": ("Masa Berlaku", 2.4),
    "persetujuan": ("Perizinan", 1.5),
    "izin": ("Perizinan", 1.5),
}

# Terms flagging IT relevance — the MVP pilot PoV is Unit Bisnis IT (URD 3.5).
IT_TERMS = {
    "teknologi informasi", "sistem elektronik", "keamanan siber", "siber",
    "data pribadi", "pusat data", "data center", "aplikasi", "digital",
    "elektronik", "infrastruktur ti", "cloud", "komputasi awan", "server",
    "jaringan", "perangkat lunak", "basis data", "interoperabilitas",
    "api", "enkripsi", "audit ti", "business continuity", "disaster recovery",
    "outsourcing ti", "insiden", "penyedia jasa teknologi",
}

RE_SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[A-Z(])|\n{2,}")
RE_WORD = re.compile(r"[a-zA-ZÀ-ɏ]{3,}")


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for chunk in RE_SENT_SPLIT.split(text or ""):
        s = re.sub(r"\s+", " ", chunk).strip()
        if 40 <= len(s) <= 600:
            out.append(s)
    return out


def _tokens(text: str) -> list[str]:
    return [w for w in RE_WORD.findall(text.lower()) if w not in STOPWORDS]


def _score_sentences(sentences: list[str]) -> list[tuple[float, int, str]]:
    """TF-based scoring with a light position bonus and length penalty."""
    freqs = Counter()
    for s in sentences:
        freqs.update(set(_tokens(s)))
    n = len(sentences) or 1
    idf = {w: math.log(1 + n / (1 + c)) for w, c in freqs.items()}

    scored: list[tuple[float, int, str]] = []
    for i, s in enumerate(sentences):
        toks = _tokens(s)
        if not toks:
            continue
        base = sum(freqs[t] * idf.get(t, 0) for t in toks) / math.sqrt(len(toks))
        low = s.lower()
        boost = 1.0 + sum(w for m, (_, w) in OBLIGATION_MARKERS.items()
                          if m in low) / 10
        position = 1.15 if i < n * 0.15 else 1.0
        scored.append((base * boost * position, i, s))
    return scored


def summarize(text: str, max_sentences: int = 7) -> str:
    """Extractive summary, sentences returned in original document order."""
    sentences = _sentences(text)
    if not sentences:
        return ""
    if len(sentences) <= max_sentences:
        return " ".join(sentences)
    top = sorted(_score_sentences(sentences), key=lambda x: -x[0])[:max_sentences]
    return " ".join(s for _, _, s in sorted(top, key=lambda x: x[1]))


def key_takeaways(
    struct: DocumentStructure, text: str, limit: int = 12
) -> list[dict[str, Any]]:
    """Mine normative statements, tagged by category and anchored to a Pasal."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def consider(sentence: str, pasal: str | None, page: int | None) -> None:
        low = sentence.lower()
        hits = [(cat, w, len(m))
                for m, (cat, w) in OBLIGATION_MARKERS.items() if m in low]
        if not hits:
            return
        key = re.sub(r"\W+", "", low)[:90]
        if key in seen:
            return
        seen.add(key)
        # Highest weight wins; a tie goes to the more specific (longer) marker,
        # so "wajib menyampaikan" is read as Pelaporan rather than Kewajiban.
        cat, weight, _ = max(hits, key=lambda h: (h[1], h[2]))
        found.append({
            "category": cat,
            "pasal": pasal,
            "page": page,
            "text": re.sub(r"\s+", " ", sentence).strip(),
            "score": round(weight + 0.2 * len(hits), 2),
            "it_relevant": any(t in low for t in IT_TERMS),
        })

    if struct.articles:
        for art in struct.articles:
            label = f"Pasal {art.number}"
            if art.ayat:
                for num, body in art.ayat:
                    for s in _sentences(body) or [body]:
                        consider(s, f"{label} ayat ({num})", art.page)
            else:
                for s in _sentences(art.text):
                    consider(s, label, art.page)
    else:
        for s in _sentences(text):
            consider(s, None, None)

    found.sort(key=lambda d: (-d["score"], d["pasal"] or ""))
    return found[:limit]


def topic_profile(text: str, top_n: int = 15) -> list[dict[str, Any]]:
    """Dominant terms + IT-relevance flags (URD 3.3: identifikasi topik)."""
    toks = _tokens(text)
    if not toks:
        return []
    counts = Counter(toks)
    bigrams = Counter(
        f"{a} {b}" for a, b in zip(toks, toks[1:])
        if len(a) > 3 and len(b) > 3
    )
    merged = Counter()
    for term, c in counts.items():
        if c > 2:
            merged[term] = c
    for term, c in bigrams.items():
        if c > 2:
            merged[term] = c * 2  # phrases carry more signal than single words
    return [
        {"term": t, "count": c, "it_relevant": t in IT_TERMS}
        for t, c in merged.most_common(top_n)
    ]


def it_relevance(text: str) -> dict[str, Any]:
    """How strongly this regulation touches the IT unit's remit (URD 3.5)."""
    low = (text or "").lower()
    hits = {t: low.count(t) for t in IT_TERMS if t in low}
    total = sum(hits.values())
    words = max(len(low.split()), 1)
    density = total / words * 1000
    if total == 0:
        level = "none"
    elif density < 0.5:
        level = "low"
    elif density < 2.0:
        level = "medium"
    else:
        level = "high"
    return {
        "level": level,
        "hit_count": total,
        "density_per_1k_words": round(density, 2),
        "terms": dict(sorted(hits.items(), key=lambda kv: -kv[1])[:12]),
    }


def build_summary(
    text: str,
    struct: DocumentStructure,
    metadata: RegulationMetadata,
    max_sentences: int = 7,
) -> dict[str, Any]:
    """Assemble the full deterministic analysis payload for one document."""
    body = text
    # Skip the preamble so the summary describes the substance, not the
    # recitals. Surat Edaran have no pasal, so they keep their full text.
    if struct.articles:
        body = "\n\n".join(a.text for a in struct.articles if a.text) or text
    return {
        "summary": summarize(body or text, max_sentences=max_sentences),
        "key_takeaways": key_takeaways(struct, text),
        "topics": topic_profile(body or text),
        "it_relevance": it_relevance(body or text),
        "statistics": {
            "characters": len(text or ""),
            "words": len((text or "").split()),
            "bab_count": len(struct.babs),
            "article_count": len(struct.articles),
            "section_count": len(struct.sections),
            "attachment_article_count": len(struct.attachment_articles),
            "ayat_count": sum(len(a.ayat) for a in struct.articles),
        },
        "structure_quality": structure_quality(struct),
        "metadata_warnings": metadata.warnings,
        "mode": "deterministic",
    }
