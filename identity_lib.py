#!/usr/bin/env python3
"""identity_lib.py - matches a candidate job posting against a list of existing records
(e.g. rows in your own application tracker) to answer "have I already seen/applied to this
one?" Used by dedup_applied.py. Not a standalone script - no __main__ block.

Two posting records are the SAME posting only if:
  - Both have a Link: same posting only if that Link matches (normalized).
    A differing Link means a DIFFERENT posting even if all other metadata is
    identical - never fall back to metadata matching when both sides have their
    own Link and it differs.
  - Only when AT LEAST ONE side has no Link does it fall back to
    Company + Role Title + Location (never Company + Role Title alone).

Usage:
    idx = IdentityIndex()
    for existing in existing_rows:
        idx.add(existing["company"], existing["role"], existing["location"], existing["link"], key=existing_key)
    for candidate in new_candidates:
        match = idx.lookup(candidate["company"], candidate["role"], candidate["location"], candidate["link"])
        # match is the `key` passed to add() for whichever existing record
        # this candidate identifies as the same posting, or None.
"""
import re

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "utm_id", "gclid", "fbclid", "msclkid"}


def normalize(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def normalize_link(s):
    """Heuristic URL normalization: strips scheme, trailing slash, and known
    tracking query params, then lowercases. Keeps any other query param
    (e.g. ?jobId=123) so distinct requisitions aren't collapsed into one."""
    if not s:
        return ""
    s = s.strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", s)
    if "?" in s:
        base, query = s.split("?", 1)
        kept = [p for p in query.split("&") if p and p.split("=", 1)[0] not in TRACKING_PARAMS]
        s = base + ("?" + "&".join(kept) if kept else "")
    return s.rstrip("/")


def metadata_key(company, role, location):
    return (normalize(company), normalize(role), normalize(location))


class IdentityIndex:
    """Three-index structure implementing the matching rule symmetrically -
    every add()/lookup() call checks both directions (does THIS record have
    a link the other lacks, or vice versa), not just one side."""

    def __init__(self):
        self.by_link = {}               # normalized link -> key, records WITH a link
        self.by_metadata_all = {}       # metadata key -> key, EVERY record
        self.by_metadata_linkless = {}  # metadata key -> key, records with NO link

    def add(self, company, role, location, link, key):
        mkey = metadata_key(company, role, location)
        self.by_metadata_all[mkey] = key
        lkey = normalize_link(link)
        if lkey:
            self.by_link[lkey] = key
        else:
            self.by_metadata_linkless[mkey] = key

    def lookup(self, company, role, location, link):
        """Returns the matching existing key, or None. Symmetric: a linked
        candidate falls back to metadata only against linkless existing
        records (an existing record with its OWN different link was already
        a non-match); a linkless candidate falls back against every
        existing record, linked or not - it has nothing stronger to offer."""
        mkey = metadata_key(company, role, location)
        lkey = normalize_link(link)
        if lkey:
            match = self.by_link.get(lkey)
            if match is not None:
                return match
            return self.by_metadata_linkless.get(mkey)
        return self.by_metadata_all.get(mkey)
