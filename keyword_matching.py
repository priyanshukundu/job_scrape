"""keyword_matching.py - word-boundary, alias-aware term matching.

Fixes a real bug: plain substring matching (`term in text`) false-positives
on short terms that appear inside unrelated words - e.g. "intern" inside
"Internal"/"International", or "engineer" inside "Financial Engineer" (as an
exclude term, that would wrongly reject the role). Word-boundary matching
(via lookaround, not \\b, so it also works correctly for terms that start or
end with punctuation) eliminates that class of false positive.

Also normalizes a curated set of full-phrase <-> acronym aliases, so a JD
that spells out "Asset Liability Management" matches "ALM", and vice versa.
"""
import functools
import re

ALIASES = {
    "asset liability management": "alm",
    "asset-liability management": "alm",
    "funds transfer pricing": "ftp",
    "liquidity coverage ratio": "lcr",
    "net stable funding ratio": "nsfr",
    "high quality liquid assets": "hqla",
    "high-quality liquid assets": "hqla",
    "value at risk": "var",
    "economic value of equity": "eve",
    "net interest income": "nii",
    "risk-weighted assets": "rwa",
    "risk weighted assets": "rwa",
    "internal capital adequacy assessment process": "icaap",
    "counterparty credit risk": "ccr",
    "credit valuation adjustment": "cva",
    "value-at-risk": "var",
}


def _term_variants(term):
    key = term.lower()
    variants = {term}
    if key in ALIASES:
        variants.add(ALIASES[key])
    else:
        for full, short in ALIASES.items():
            if key == short:
                variants.add(full)
    return variants


@functools.lru_cache(maxsize=None)  # re's own cache holds 512; the exclude lists now exceed that and thrash
def _pattern(term):
    escaped = re.escape(term)
    return re.compile(r"(?<![A-Za-z0-9])" + escaped + r"(?![A-Za-z0-9])", re.IGNORECASE)


def contains_term(text, term):
    return any(_pattern(v).search(text) for v in _term_variants(term))


@functools.lru_cache(maxsize=64)
def _combined(terms):
    variants = set()
    for term in terms:
        variants |= _term_variants(term)
    alt = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
    return re.compile(r"(?<![A-Za-z0-9])(?:" + alt + r")(?![A-Za-z0-9])", re.IGNORECASE) if alt else None


def contains_any(text, terms):
    """Same result as any(contains_term(text, t) for t in terms), in one regex pass (lists hold 600+ terms)."""
    rx = _combined(tuple(terms))
    return bool(rx and rx.search(text))
