#!/usr/bin/env python3
"""Turn completed auction drafts into normalized, comparable prices.

The rules that matter (all from CLAUDE.md):
  * every price is stored as percent-of-budget, never raw dollars
  * keeper picks are dropped, and >40%-keeper drafts are dropped whole
  * mock drafts (no league_id) are dropped
  * segments are kept apart: format, superflex, PPR, team count

Usage:
    python3 ingest.py                     # ingest everything the crawl queued
    python3 ingest.py --limit 5 --verbose
    python3 ingest.py --draft-id <id>     # one specific draft
    python3 ingest.py --show-picks        # print the resulting rows
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from typing import Any, Optional

import db
from client import SleeperClient
from config import Config

log = logging.getLogger("ingest")

# ---------------------------------------------------------------------------
# The winning bid
# ---------------------------------------------------------------------------
# UNVERIFIED: `metadata.amount` on auction picks is undocumented — Sleeper's
# official docs only show a snake draft example, where the field never appears.
# It is almost certainly right, but it has not been confirmed against a live
# auction response. Run `python3 verify.py --draft-id <id>` against one real
# auction draft before any large crawl; it reports which of these keys actually
# carried the bid and what type it was.
#
# This module is the only place that needs to change if the shape is different.
# The extra candidates below are defensive, in priority order — if a later one
# ever fires in practice, promote it and delete the rest rather than leaving a
# guessing game in the hot path.
AMOUNT_KEYS: tuple[tuple[str, ...], ...] = (
    ("metadata", "amount"),
    ("metadata", "bid_amount"),
    ("metadata", "winning_bid"),
    ("amount",),
)


def _dig(pick: dict, path: tuple[str, ...]) -> Any:
    node: Any = pick
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _coerce_amount(value: Any) -> Optional[int]:
    """Parse one candidate value into a whole-dollar bid, or None.

    Sleeper hands back most metadata as strings, so "54" is the expected shape,
    but ints and floats are accepted too. Anything non-numeric, negative, or
    zero is not a bid.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        amount = int(value)
    elif isinstance(value, str):
        text = value.strip().lstrip("$").replace(",", "")
        if not text:
            return None
        try:
            amount = int(float(text))
        except ValueError:
            return None
    else:
        return None
    return amount if amount > 0 else None


def parse_amount(pick: dict) -> Optional[int]:
    """The winning bid on an auction pick, in that league's dollars.

    Returns None when the pick carries no bid — which is what a snake pick
    looks like, and also what a malformed auction pick looks like, so callers
    should treat a draft with zero priced picks as suspect rather than empty.
    """
    for path in AMOUNT_KEYS:
        amount = _coerce_amount(_dig(pick, path))
        if amount is not None:
            return amount
    return None


def found_amount_key(pick: dict) -> Optional[tuple[str, ...]]:
    """Which candidate key actually carried the bid. For verify.py's report."""
    for path in AMOUNT_KEYS:
        if _coerce_amount(_dig(pick, path)) is not None:
            return path
    return None


def is_keeper(pick: dict) -> bool:
    """A keeper price is a contract from a prior season, not a clearing price.

    `is_keeper` is the documented field; it comes back as true, false, or null.
    Some leagues also stamp it into metadata, so that is checked too.
    """
    if pick.get("is_keeper"):
        return True
    meta = pick.get("metadata")
    if isinstance(meta, dict):
        flag = meta.get("is_keeper")
        if flag in (True, "true", "1", 1):
            return True
    return False


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------
# Prices are not comparable across these, so they are never averaged together.

LEAGUE_TYPE_NAMES = {0: "redraft", 1: "keeper", 2: "dynasty"}


def classify_format(league: Optional[dict], draft: dict) -> str:
    """redraft / keeper / dynasty, from league.settings.type.

    The draft object carries no format field, so a league we could not fetch
    leaves this "unknown" — which keeps it in its own segment rather than
    silently pooling it with redraft prices.
    """
    league_type = (league or {}).get("settings", {}).get("type")
    return LEAGUE_TYPE_NAMES.get(league_type, "unknown")


def classify_superflex(league: Optional[dict], draft: dict) -> bool:
    positions = (league or {}).get("roster_positions") or []
    if any(p in ("SUPER_FLEX", "SUPERFLEX") for p in positions):
        return True
    if positions and sum(1 for p in positions if p == "QB") >= 2:
        return True
    settings = draft.get("settings") or {}
    return bool(settings.get("slots_super_flex")) or (settings.get("slots_qb") or 0) >= 2


DRAFT_SCORING_TYPES = {"std": "standard", "half_ppr": "half_ppr", "ppr": "ppr"}


def classify_ppr(league: Optional[dict], draft: Optional[dict] = None) -> tuple[str, Optional[float]]:
    """(label, points-per-reception). Falls back to the draft's scoring_type."""
    scoring = (league or {}).get("scoring_settings") or {}
    rec = scoring.get("rec")
    if rec is None:
        hint = ((draft or {}).get("metadata") or {}).get("scoring_type")
        return DRAFT_SCORING_TYPES.get(hint, "unknown"), None
    try:
        rec = float(rec)
    except (TypeError, ValueError):
        return "unknown", None
    if rec == 0:
        return "standard", rec
    if abs(rec - 0.5) < 1e-9:
        return "half_ppr", rec
    if abs(rec - 1.0) < 1e-9:
        return "ppr", rec
    return "custom", rec


def classify_teams(league: Optional[dict], draft: dict) -> Optional[int]:
    if league and league.get("total_rosters"):
        return int(league["total_rosters"])
    teams = (draft.get("settings") or {}).get("teams")
    return int(teams) if teams else None


# ---------------------------------------------------------------------------
# Draft-level ingest
# ---------------------------------------------------------------------------

class Rejected(Exception):
    """This draft does not belong in the dataset, with a reason.

    Carries the keeper share when one was measured, so a draft thrown out for
    being keeper-heavy still contributes its number to the distribution — the
    40% threshold is a guess, and the rejected drafts are half the evidence for
    where it should actually sit.
    """

    def __init__(self, reason: str, keeper_share: Optional[float] = None):
        super().__init__(reason)
        self.reason = reason
        self.keeper_share = keeper_share


def screen_draft(draft: dict) -> None:
    """Raise Rejected if the draft itself disqualifies before we fetch picks."""
    if not draft:
        raise Rejected("draft not found")
    if draft.get("type") != "auction":
        raise Rejected(f"not an auction (type={draft.get('type')!r})")
    if draft.get("status") != "complete":
        raise Rejected(f"not complete (status={draft.get('status')!r})")
    # Mock drafts have no league behind them. Play money behaves differently
    # from real money. This is a heuristic, not a documented guarantee.
    if not draft.get("league_id"):
        raise Rejected("mock draft (no league_id)")
    budget = (draft.get("settings") or {}).get("budget")
    if not budget or int(budget) <= 0:
        raise Rejected(f"no auction budget (budget={budget!r})")


def normalize_picks(picks: list[dict], budget: int, keeper_threshold: float) -> tuple[list[dict], float]:
    """Drop keepers, price the rest as percent-of-budget.

    Returns (rows, keeper_share). Raises Rejected if the draft is mostly
    keepers, or if nothing in it carries a bid at all.
    """
    keepers = 0
    considered = 0
    rows: list[dict] = []

    for pick in picks:
        player_id = pick.get("player_id")
        pick_no = pick.get("pick_no")
        if not player_id or pick_no is None:
            continue
        considered += 1
        if is_keeper(pick):
            keepers += 1
            continue
        amount = parse_amount(pick)
        if amount is None:
            continue
        rows.append({
            "pick_no": int(pick_no),
            "player_id": str(player_id),
            "amount": amount,
            # A $54 in a $200 league and a $27 in a $100 league are the same
            # price. Raw dollars are never compared across leagues.
            "pct_of_budget": round(100.0 * amount / budget, 6),
            "is_keeper": False,
        })

    keeper_share = (keepers / considered) if considered else 0.0
    if keeper_share > keeper_threshold:
        raise Rejected(
            f"keeper share {keeper_share:.0%} above threshold {keeper_threshold:.0%}",
            keeper_share=keeper_share,
        )
    if not rows:
        raise Rejected("no priced picks — check parse_amount() against a raw pick",
                       keeper_share=keeper_share)
    return rows, keeper_share


def ingest_draft(
    conn: sqlite3.Connection,
    client: SleeperClient,
    draft_id: str,
    cfg: Config,
    league_cache: Optional[dict] = None,
) -> dict:
    """Fetch, screen, normalize and store one draft. Idempotent."""
    league_cache = league_cache if league_cache is not None else {}
    draft = client.get_draft(draft_id) or {}
    league_id = draft.get("league_id") or ""

    row = {
        "draft_id": draft_id,
        "league_id": league_id,
        "season": str(draft.get("season") or cfg.season),
        "draft_type": draft.get("type"),
        "status": draft.get("status"),
        "budget": (draft.get("settings") or {}).get("budget"),
        "teams": None,
        "league_format": "unknown",
        "superflex": 0,
        "ppr_type": "unknown",
        "scoring_rec": None,
        "keeper_share": None,
        "pick_count": None,
        "priced_picks": 0,
        "included": 0,
        "exclusion_reason": None,
    }

    try:
        screen_draft(draft)
    except Rejected as exc:
        row["exclusion_reason"] = exc.reason
        db.upsert_draft(conn, row)
        db.replace_picks(conn, draft_id, [])
        db.set_draft_state(conn, draft_id, "skipped")
        log.info("skip %s: %s", draft_id, exc.reason)
        return row

    if league_id not in league_cache:
        league_cache[league_id] = client.get_league(league_id)
    league = league_cache[league_id]

    ppr_type, scoring_rec = classify_ppr(league, draft)
    row.update({
        "teams": classify_teams(league, draft),
        "league_format": classify_format(league, draft),
        "superflex": 1 if classify_superflex(league, draft) else 0,
        "ppr_type": ppr_type,
        "scoring_rec": scoring_rec,
    })

    picks = client.get_draft_picks(draft_id)
    row["pick_count"] = len(picks)

    try:
        priced, keeper_share = normalize_picks(picks, int(row["budget"]), cfg.keeper_draft_threshold)
    except Rejected as exc:
        row["exclusion_reason"] = exc.reason
        if exc.keeper_share is not None:
            row["keeper_share"] = round(exc.keeper_share, 4)
        db.upsert_draft(conn, row)
        db.replace_picks(conn, draft_id, [])
        db.set_draft_state(conn, draft_id, "skipped")
        log.info("skip %s: %s", draft_id, exc.reason)
        return row

    row["keeper_share"] = round(keeper_share, 4)
    row["priced_picks"] = len(priced)
    row["included"] = 1
    db.upsert_draft(conn, row)
    db.replace_picks(conn, draft_id, priced)
    db.set_draft_state(conn, draft_id, "ingested")
    log.info(
        "ingest %s: %d priced picks, budget $%s, %s/%s/%steams/%s",
        draft_id, len(priced), row["budget"], row["league_format"],
        row["ppr_type"], row["teams"], "SF" if row["superflex"] else "1QB",
    )
    return row


def ingest_pending(conn, client, cfg, limit=None, draft_ids=None) -> list[dict]:
    if draft_ids:
        targets = list(draft_ids)
    else:
        targets = [r["draft_id"] for r in db.pending_drafts(conn, cfg.season, limit)]
    league_cache: dict = {}
    results = []
    for draft_id in targets:
        results.append(ingest_draft(conn, client, draft_id, cfg, league_cache))
        conn.commit()
    return results


def show_picks(conn: sqlite3.Connection, limit: int = 40, draft_id: Optional[str] = None) -> str:
    sql = (
        "SELECT p.draft_id, p.pick_no, p.player_id, p.amount, p.pct_of_budget, d.budget "
        "FROM picks p JOIN drafts d ON d.draft_id = p.draft_id"
    )
    params: list = []
    if draft_id:
        sql += " WHERE p.draft_id = ?"
        params.append(draft_id)
    sql += " ORDER BY p.draft_id, p.amount DESC LIMIT ?"
    params.append(limit)

    header = f"{'draft_id':<22} {'pick':>4} {'player_id':>10} {'amt':>6} {'budget':>7} {'pct':>7}"
    lines = [header, "-" * len(header)]
    for r in conn.execute(sql, params):
        lines.append(
            f"{r['draft_id']:<22} {r['pick_no']:>4} {r['player_id']:>10} "
            f"{r['amount']:>6} {r['budget']:>7} {r['pct_of_budget']:>6.2f}%"
        )
    if len(lines) == 2:
        lines.append("(no rows)")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--draft-id", action="append", dest="draft_ids",
                    help="ingest this draft only; repeatable")
    ap.add_argument("--limit", type=int, help="max queued drafts to ingest")
    ap.add_argument("--show-picks", action="store_true", help="print the resulting picks rows")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.from_env()
    conn = db.connect(cfg.db_path)
    client = SleeperClient(rate_limit_per_min=cfg.rate_limit_per_min)

    results = ingest_pending(conn, client, cfg, limit=args.limit, draft_ids=args.draft_ids)
    conn.commit()

    kept = [r for r in results if r["included"]]
    print(f"\ningested {len(results)} draft(s): {len(kept)} kept, {len(results) - len(kept)} excluded")
    for r in results:
        if not r["included"]:
            print(f"  excluded {r['draft_id']}: {r['exclusion_reason']}")
    print(f"totals: {db.counts(conn)}")

    if args.show_picks:
        print()
        print(show_picks(conn, limit=40))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
