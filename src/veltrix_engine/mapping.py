"""
Channel mapping: which source column is which canonical channel.

Logger software names channels differently, so every column is scored against
each channel's known names. Names are compared as whole words, so "ax" cannot
match "Max Speed" and "Brake Temp FL" is never taken for the brake channel.
"""

from __future__ import annotations

from .schema import CHANNELS, MEASURED, norm_name


def auto_map(columns) -> dict:
    """Best source column for each measured channel ({channel id: column}, only
    channels that found a match).

    An exact name scores highest; a column containing all of a name's words
    scores a little less for each extra word. The strongest matches are assigned
    first and each column is used at most once."""
    candidates = []
    for ci, col in enumerate(columns):
        norm = norm_name(col)
        tokens = norm.split()
        token_set = set(tokens)
        for cid in MEASURED:
            cdef = CHANNELS[cid]
            if token_set & cdef.exclude:
                continue
            best = None
            for ai, alias in enumerate(cdef.aliases):
                words = alias.split()
                if norm == alias or norm == cid.replace("_", " "):
                    score = 100
                elif all(w in tokens for w in words):
                    score = 80 - 5 * (len(tokens) - len(words))
                else:
                    continue
                if best is None or (score, -ai) > best:
                    best = (score, -ai)
            if best is None and norm == cid.replace("_", " "):
                best = (100, 0)
            if best:
                candidates.append((*best, -ci, cid, col))

    mapping, used = {}, set()
    for *_, cid, col in sorted(candidates, reverse=True):
        if cid not in mapping and col not in used:
            mapping[cid] = col
            used.add(col)
    return mapping


def validate_mapping(mapping: dict, columns) -> None:
    """Raise if a mapping names unknown channels or columns, or uses a column twice."""
    columns = list(columns)
    for cid, col in mapping.items():
        if cid not in CHANNELS or CHANNELS[cid].kind != "measured":
            raise ValueError(f"'{cid}' is not a measured channel in the schema.")
        if col not in columns:
            raise ValueError(f"Column '{col}' (mapped as {cid}) is not in the file.")
    cols = list(mapping.values())
    twice = sorted({c for c in cols if cols.count(c) > 1})
    if twice:
        raise ValueError(f"Column(s) mapped to more than one channel: {twice}")
