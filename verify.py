#!/usr/bin/env python3
"""Confirm the shape of the winning-bid field on a real auction pick.

`metadata.amount` is undocumented — Sleeper's published docs only show a snake
draft, where the field never appears. Run this against one real completed
auction draft before any large crawl. It prints a whole raw pick verbatim and
reports which key actually carried the bid, so `parse_amount()` in ingest.py
can be corrected if the shape differs.

Usage:
    python3 verify.py                        # first queued auction draft
    python3 verify.py --draft-id <id>
    python3 verify.py --picks-file picks.json    # offline, no network needed
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import db
from client import SleeperClient
from config import Config
from ingest import AMOUNT_KEYS, found_amount_key, is_keeper, parse_amount


def describe(value: object) -> str:
    return f"{type(value).__name__} {value!r}"


def report(picks: list[dict], draft: Optional[dict] = None) -> str:
    out: list[str] = []
    w = out.append

    if draft:
        settings = draft.get("settings") or {}
        w("draft")
        w(f"  draft_id  : {draft.get('draft_id')}")
        w(f"  type      : {draft.get('type')!r}   status: {draft.get('status')!r}")
        w(f"  league_id : {draft.get('league_id')!r}  (empty means mock — excluded)")
        w(f"  budget    : {settings.get('budget')!r}   teams: {settings.get('teams')!r}")
        w("")

    if not picks:
        w("no picks returned — nothing to verify")
        return "\n".join(out)

    w(f"picks returned: {len(picks)}")
    w("")
    w("=" * 72)
    w("RAW JSON OF ONE PICK (verbatim, as returned)")
    w("=" * 72)
    priced = next((p for p in picks if parse_amount(p) is not None), None)
    sample = priced or picks[0]
    if priced is None:
        w("!! no pick matched any known bid key — showing the first pick instead")
    w(json.dumps(sample, indent=2, sort_keys=True))
    w("")

    w("=" * 72)
    w("BID FIELD")
    w("=" * 72)
    meta = sample.get("metadata")
    if isinstance(meta, dict):
        w(f"metadata keys on this pick: {sorted(meta)}")
    else:
        w(f"metadata is not a dict: {describe(meta)}")

    for path in AMOUNT_KEYS:
        node: object = sample
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        marker = "<-- used" if found_amount_key(sample) == path else ""
        w(f"  {'.'.join(path):<24} {describe(node):<28} {marker}")
    w("")
    w(f"parse_amount() returns: {parse_amount(sample)!r}")
    w("")

    key_counts = Counter(".".join(k) if (k := found_amount_key(p)) else "NONE" for p in picks)
    w("across every pick in this draft:")
    for key, count in key_counts.most_common():
        w(f"  {key:<24} {count:>4} pick(s)")

    keepers = sum(1 for p in picks if is_keeper(p))
    w("")
    w(f"keeper picks: {keepers}/{len(picks)} ({keepers / len(picks):.0%}) — dropped before pricing")

    unpriced = key_counts.get("NONE", 0)
    w("")
    if unpriced == len(picks):
        w("VERDICT: no bid found on any pick. parse_amount() needs fixing — add the")
        w("         real key to AMOUNT_KEYS in ingest.py. Do not scale the crawl up.")
    elif found_amount_key(sample) == ("metadata", "amount"):
        w("VERDICT: metadata.amount confirmed. The assumption held; nothing to change.")
        if unpriced:
            w(f"         ({unpriced} pick(s) carried no bid — keepers and any unsold slots)")
    else:
        w(f"VERDICT: the bid is at {'.'.join(found_amount_key(sample))}, not metadata.amount.")
        w("         Promote that key in AMOUNT_KEYS in ingest.py and drop the rest.")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--draft-id", help="draft to inspect; defaults to the first queued auction draft")
    ap.add_argument("--picks-file", help="read picks JSON from a file instead of the API")
    ap.add_argument("--draft-file", help="optional draft JSON to go with --picks-file")
    args = ap.parse_args(argv)

    if args.picks_file:
        picks = json.loads(Path(args.picks_file).read_text())
        draft = json.loads(Path(args.draft_file).read_text()) if args.draft_file else None
        if isinstance(picks, dict):
            picks = picks.get("picks", [])
        print(report(picks, draft))
        return 0

    cfg = Config.from_env()
    draft_id = args.draft_id
    if not draft_id:
        conn = db.connect(cfg.db_path)
        queued = db.pending_drafts(conn, cfg.season, limit=1, include_ingested=True)
        if not queued:
            print("no drafts in the queue — run crawl.py first, or pass --draft-id",
                  file=sys.stderr)
            return 1
        draft_id = queued[0]["draft_id"]
        print(f"using queued draft {draft_id}\n")

    client = SleeperClient(rate_limit_per_min=cfg.rate_limit_per_min)
    draft = client.get_draft(draft_id)
    picks = client.get_draft_picks(draft_id)
    print(report(picks, draft))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
