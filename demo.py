#!/usr/bin/env python3
"""Run the whole pipeline against synthetic drafts, with no network at all.

Nothing here is real data. It exists so the shape of every stage — discovery,
the raw-pick verification report, the picks table, the values table — can be
seen and checked before a single live API call, and so the same walkthrough is
reproducible offline afterwards.

    python3 demo.py
"""

from __future__ import annotations

import aggregate
import crawl
import db
import ingest
import verify
from config import Config
from tests import fixtures as fx

CFG = Config(db_path=":memory:", season="2025", user_id_salt="demo-salt",
             seed_league_id="SEED", keeper_draft_threshold=0.40, reference_budget=200)

# Three real-money auction leagues on different budgets, one keeper-heavy
# league that should be thrown out whole, and one mock that should never be
# queued in the first place.
LEAGUES = {
    "SEED": fx.league("SEED", league_type=0, teams=12, rec=1.0),
    "L2":   fx.league("L2", league_type=0, teams=12, rec=1.0),
    "L3":   fx.league("L3", league_type=0, teams=12, rec=1.0),
    "L4":   fx.league("L4", league_type=1, teams=12, rec=1.0),
}
DRAFTS = {
    "D1": fx.draft("D1", "SEED", budget=200),
    "D2": fx.draft("D2", "L2", budget=100),
    "D3": fx.draft("D3", "L3", budget=400),
    "D4": fx.draft("D4", "L4", budget=200),
    "D5": fx.draft("D5", league_id=None, budget=200),      # mock
}
PICKS = {
    "D1": fx.auction_picks({"star": 54, "mid": 20, "fringe": 1, "scrub": 1}, draft_id="D1"),
    "D2": fx.auction_picks({"star": 29, "mid": 9,  "fringe": 1, "scrub": 1}, draft_id="D2"),
    "D3": fx.auction_picks({"star": 100, "mid": 48, "fringe": 12, "scrub": 4}, draft_id="D3"),
    "D4": fx.auction_picks({"star": 40}, keepers={"mid": 5, "fringe": 5}, draft_id="D4"),
    "D5": fx.auction_picks({"star": 90}, draft_id="D5"),
}
MEMBERS = {"SEED": [{"user_id": "u1", "display_name": "handle"}],
           "L2": [{"user_id": "u2", "display_name": "handle"}],
           "L3": [{"user_id": "u3", "display_name": "handle"}],
           "L4": [{"user_id": "u4", "display_name": "handle"}]}
USER_DRAFTS = {
    "u1": [DRAFTS["D1"], DRAFTS["D2"], DRAFTS["D5"]],
    "u2": [DRAFTS["D2"], DRAFTS["D3"]],
    "u3": [DRAFTS["D3"], DRAFTS["D4"]],
    "u4": [DRAFTS["D4"]],
}


def rule(title: str) -> None:
    print(f"\n\n{'=' * 74}\n{title}\n{'=' * 74}")


def main() -> int:
    print(__doc__.strip())
    conn = db.connect(":memory:")
    client = fx.FakeClient(drafts=DRAFTS, picks=PICKS, leagues=LEAGUES,
                           league_users=MEMBERS, user_drafts=USER_DRAFTS)

    rule("1. crawl — discovery through the league social graph")
    stats = crawl.Crawler(conn, client, CFG, max_drafts=20, max_depth=3).run()
    for key, value in stats.items():
        print(f"  {key}: {value}")
    print(f"  queued: {[r['draft_id'] for r in db.pending_drafts(conn, '2025')]}")
    print("  (D5 was a mock — no league_id — so it was never queued)")

    rule("2. verify — what the bid field looks like on one pick")
    print(verify.report(PICKS["D1"], DRAFTS["D1"]))

    rule("3. ingest — normalize to percent-of-budget, drop keepers and mocks")
    results = ingest.ingest_pending(conn, client, CFG)
    for r in results:
        verdict = "kept" if r["included"] else f"EXCLUDED — {r['exclusion_reason']}"
        print(f"  {r['draft_id']}  budget ${r['budget']:<4} {verdict}")

    rule("4. picks table")
    print(ingest.show_picks(conn, limit=40))
    print("\n  star went for $54/$200, $29/$100 and $100/$400 — three different")
    print("  dollar figures, nearly the same share of the budget.")

    rule("5. aggregation — one segment, spread included")
    rows = aggregate.aggregate(conn, season="2025", league_format="redraft",
                               superflex=0, ppr="ppr", teams=12, min_samples=1)
    print(aggregate.format_table(rows, {}, CFG.reference_budget, limit=20))

    rule("6. segments — never collapsed to grow the sample")
    for s in aggregate.list_segments(conn):
        print(f"  {s['season']} {s['league_format']:<8} "
              f"{'SF' if s['superflex'] else '1QB':<4} {s['ppr_type']:<9} "
              f"{s['teams']}tm  drafts={s['n_drafts']} picks={s['n_picks']}")

    print("\n\nAll synthetic. Real numbers need a live crawl — and the bid field")
    print("confirmed with verify.py first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
