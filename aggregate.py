#!/usr/bin/env python3
"""Aggregate normalized picks into auction values, per segment.

Segments are never collapsed to grow the sample. A dynasty superflex price and
a redraft 1QB price describe different markets; averaging them produces a
number that describes no real league.

The spread is the useful part, so median/p25/p75 sit next to the mean, and
`pct_above_min` tracks how often a player went above the $1 floor — late
auctions collapse into everyone-costs-a-dollar and that flattens the fringe.

Usage:
    python3 aggregate.py --segments
    python3 aggregate.py --format redraft --ppr ppr --teams 12 --limit 40
    python3 aggregate.py --format dynasty --superflex 1 --materialize
    python3 aggregate.py --keeper-distribution
"""

from __future__ import annotations

import argparse
import sqlite3
from typing import Optional

import db
from client import SleeperClient
from config import Config

SEGMENT_COLUMNS = ("season", "league_format", "superflex", "ppr_type", "teams")


def percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile, q in [0, 1]. Assumes sorted input."""
    if not sorted_values:
        raise ValueError("no values")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    frac = pos - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


# Reception scoring, collapsed. Only half-PPR and full PPR are pooled: standard
# scoring is a genuinely different market for anyone who catches the ball, and
# pooling it in would not be a close call.
POOLED_PPR_LABEL = "half_ppr+ppr"
POOLED_PPR_TYPES = ("half_ppr", "ppr")

_PPR_EXPR = (
    f"CASE WHEN d.ppr_type IN {POOLED_PPR_TYPES} THEN '{POOLED_PPR_LABEL}' "
    "ELSE d.ppr_type END"
)


def list_segments(conn: sqlite3.Connection, pool_ppr: bool = False) -> list[sqlite3.Row]:
    """Segments and their sample sizes.

    With pool_ppr, half-PPR and full PPR are reported as one row so the combined
    sample is visible. That is a reporting choice, not a claim that they price
    the same — `compare_dimension` is what answers that.
    """
    ppr_expr = _PPR_EXPR if pool_ppr else "d.ppr_type"
    return list(conn.execute(
        f"""
        SELECT d.season, d.league_format, d.superflex, {ppr_expr} AS ppr_type, d.teams,
               MAX(COALESCE(d.max_keepers, 0)) AS max_keepers,
               COUNT(DISTINCT d.draft_id) AS n_drafts,
               COUNT(p.pick_no)           AS n_picks
        FROM drafts d LEFT JOIN picks p ON p.draft_id = d.draft_id
        WHERE d.included = 1
        GROUP BY d.season, d.league_format, d.superflex, ppr_type, d.teams
        ORDER BY n_drafts DESC
        """
    ))


def _segment_where(season=None, league_format=None, superflex=None, ppr=None, teams=None):
    """Build the WHERE clause for a segment.

    Any filter may be a list, which pools those values into one result set.
    Pooling is never implicit: the caller has to ask for it, and the caller is
    responsible for labelling the output as pooled. See `compare_dimension`
    for checking whether the values being pooled actually agree.
    """
    clauses = ["d.included = 1"]
    params: list = []
    for column, value in (
        ("d.season", season), ("d.league_format", league_format),
        ("d.superflex", superflex), ("d.ppr_type", ppr), ("d.teams", teams),
    ):
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            values = list(value)
            if not values:
                continue
            clauses.append(f"{column} IN ({','.join('?' * len(values))})")
            params.extend(values)
        else:
            clauses.append(f"{column} = ?")
            params.append(value)
    return " AND ".join(clauses), params


def compare_dimension(conn, dimension, values, min_samples=3, **filters):
    """Do two segments actually agree on price? Per-player, where both have data.

    Pooling segments is only harmless if the segments being pooled price players
    the same way. This measures that instead of assuming it: for each player
    with `min_samples` picks in both, the gap between the two means, in points
    of budget, and as a share of the wider segment's own p25-p75 spread. A gap
    small against that spread is noise; a gap comparable to it is a real
    difference the pooled number would hide.
    """
    sides = {}
    for value in values:
        rows = aggregate(conn, min_samples=min_samples, **{**filters, dimension: value})
        sides[value] = {r["player_id"]: r for r in rows}

    left, right = values[0], values[1]
    shared = sorted(set(sides[left]) & set(sides[right]))
    out = []
    for player_id in shared:
        a_, b_ = sides[left][player_id], sides[right][player_id]
        spread = max(a_["p75_pct"] - a_["p25_pct"], b_["p75_pct"] - b_["p25_pct"])
        delta = b_["mean_pct"] - a_["mean_pct"]
        out.append({
            "player_id": player_id,
            f"{left}_mean": a_["mean_pct"], f"{right}_mean": b_["mean_pct"],
            f"{left}_n": a_["n_picks"], f"{right}_n": b_["n_picks"],
            "delta_pct": delta,
            "delta_vs_spread": (abs(delta) / spread) if spread > 0 else None,
        })
    out.sort(key=lambda r: abs(r["delta_pct"]), reverse=True)
    return {"left": left, "right": right, "shared_players": len(shared),
            "left_only": len(set(sides[left]) - set(sides[right])),
            "right_only": len(set(sides[right]) - set(sides[left])),
            "rows": out}


def aggregate(conn: sqlite3.Connection, season=None, league_format=None, superflex=None,
              ppr=None, teams=None, min_samples: int = 1) -> list[dict]:
    """Per-player value rows for one segment (or the union of what's selected).

    Every price in here is percent-of-budget, so drafts of different budgets
    are directly comparable.
    """
    where, params = _segment_where(season, league_format, superflex, ppr, teams)
    rows = conn.execute(
        f"""
        SELECT p.player_id, p.pct_of_budget, p.amount, p.draft_id
        FROM picks p JOIN drafts d ON d.draft_id = p.draft_id
        WHERE {where}
        """,
        params,
    ).fetchall()

    buckets: dict[str, dict] = {}
    for row in rows:
        bucket = buckets.setdefault(row["player_id"], {"pcts": [], "above_min": 0, "drafts": set()})
        bucket["pcts"].append(row["pct_of_budget"])
        bucket["drafts"].add(row["draft_id"])
        # The floor is $1 in raw league dollars, whatever the budget.
        if row["amount"] > 1:
            bucket["above_min"] += 1

    results = []
    for player_id, bucket in buckets.items():
        pcts = sorted(bucket["pcts"])
        n = len(pcts)
        if n < min_samples:
            continue
        results.append({
            "player_id": player_id,
            "n_picks": n,
            "n_drafts": len(bucket["drafts"]),
            "mean_pct": sum(pcts) / n,
            "median_pct": percentile(pcts, 0.50),
            "p25_pct": percentile(pcts, 0.25),
            "p75_pct": percentile(pcts, 0.75),
            "min_pct": pcts[0],
            "max_pct": pcts[-1],
            "pct_above_min": bucket["above_min"] / n,
        })
    results.sort(key=lambda r: r["mean_pct"], reverse=True)
    return results


def materialize(conn, rows, season, league_format, superflex, ppr, teams) -> int:
    conn.executemany(
        """
        INSERT OR REPLACE INTO aav
            (season, league_format, superflex, ppr_type, teams, player_id, n_picks, n_drafts,
             mean_pct, median_pct, p25_pct, p75_pct, min_pct, max_pct, pct_above_min, computed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (season, league_format, superflex, ppr, teams, r["player_id"], r["n_picks"],
             r["n_drafts"], r["mean_pct"], r["median_pct"], r["p25_pct"], r["p75_pct"],
             r["min_pct"], r["max_pct"], r["pct_above_min"], db.now_iso())
            for r in rows
        ],
    )
    conn.commit()
    return len(rows)


def player_names(cfg: Config, offline: bool = False) -> dict:
    """player_id -> "Name POS". Falls back to bare IDs if the dump isn't cached."""
    try:
        client = SleeperClient(rate_limit_per_min=cfg.rate_limit_per_min)
        dump = client.get_players(cfg.player_cache) if not offline else {}
    except Exception:
        return {}
    names = {}
    for pid, p in dump.items():
        if not isinstance(p, dict):
            continue
        name = p.get("full_name") or " ".join(filter(None, [p.get("first_name"), p.get("last_name")]))
        pos = p.get("position") or ""
        names[pid] = f"{name} {pos}".strip() or pid
    return names


def format_table(rows: list[dict], names: dict, reference_budget: int, limit: int) -> str:
    header = (f"{'player':<26} {'n':>4} {'mean':>7} {'med':>7} {'p25':>7} {'p75':>7} "
              f"{'>$1':>5}   (pct of budget)")
    lines = [header, "-" * len(header)]
    for r in rows[:limit]:
        label = names.get(r["player_id"], r["player_id"])[:26]
        lines.append(
            f"{label:<26} {r['n_picks']:>4} "
            f"{r['mean_pct']:>6.2f}% {r['median_pct']:>6.2f}% "
            f"{r['p25_pct']:>6.2f}% {r['p75_pct']:>6.2f}% "
            f"{r['pct_above_min']:>4.0%}"
        )
    lines.append("")
    lines.append(f"rendered to a ${reference_budget} budget:")
    lines.append(f"{'player':<26} {'mean':>7} {'med':>7} {'p25':>7} {'p75':>7}")
    lines.append("-" * len(header))
    for r in rows[:limit]:
        label = names.get(r["player_id"], r["player_id"])[:26]
        scale = reference_budget / 100.0
        lines.append(
            f"{label:<26} ${r['mean_pct'] * scale:>6.1f} ${r['median_pct'] * scale:>6.1f} "
            f"${r['p25_pct'] * scale:>6.1f} ${r['p75_pct'] * scale:>6.1f}"
        )
    if not rows:
        lines.append("(no rows — nothing ingested for this segment yet)")
    return "\n".join(lines)


def format_comparison(result, names, limit=30) -> str:
    left, right = result["left"], result["right"]
    lines = [
        f"{left} vs {right}: {result['shared_players']} players with enough sample in both "
        f"({result['left_only']} only in {left}, {result['right_only']} only in {right})",
        "",
    ]
    if not result["rows"]:
        lines.append("Not enough overlap to compare. Pooling would be a guess.")
        return "\n".join(lines)

    header = (f"{'player':<26} {left[:9]:>9} {right[:9]:>9} {'delta':>8} {'vs spread':>10}")
    lines += [header, "-" * len(header)]
    for r in result["rows"][:limit]:
        ratio = r["delta_vs_spread"]
        lines.append(
            f"{names.get(r['player_id'], r['player_id'])[:26]:<26} "
            f"{r[f'{left}_mean']:>8.2f}% {r[f'{right}_mean']:>8.2f}% "
            f"{r['delta_pct']:>+7.2f}% {(f'{ratio:.2f}x' if ratio is not None else '-'):>10}"
        )

    deltas = [abs(r["delta_pct"]) for r in result["rows"]]
    ratios = [r["delta_vs_spread"] for r in result["rows"] if r["delta_vs_spread"] is not None]
    lines += ["", f"median absolute gap: {percentile(sorted(deltas), 0.5):.2f} points of budget"]
    if ratios:
        median_ratio = percentile(sorted(ratios), 0.5)
        lines.append(f"median gap as a share of the within-segment spread: {median_ratio:.2f}x")
        lines.append("")
        if median_ratio < 0.35:
            lines.append("Small against the noise these segments already carry. Pooling costs "
                         "little, though check the biggest movers above — they will be the "
                         "pass-catchers.")
        else:
            lines.append("Comparable to or larger than the within-segment spread. These are "
                         "different markets; a pooled average would describe neither.")
    return "\n".join(lines)


def keeper_distribution(conn: sqlite3.Connection) -> str:
    """Keeper share per draft, bucketed. The 40% cutoff is a guess; this is
    what you look at to replace it with a number from the data."""
    rows = conn.execute(
        "SELECT keeper_share, included FROM drafts WHERE keeper_share IS NOT NULL"
    ).fetchall()
    if not rows:
        return "no keeper data yet — ingest some drafts first"
    shares = sorted(r["keeper_share"] for r in rows)
    rejected = sum(1 for r in rows if not r["included"])
    lines = [
        f"drafts with a measured keeper share: {len(shares)}  "
        f"({rejected} currently rejected by the threshold)",
        "",
    ]
    for low in range(0, 100, 10):
        high = low + 10
        count = sum(1 for s in shares if low / 100 <= s < high / 100)
        bar = "#" * min(count, 60)
        lines.append(f"  {low:>3}-{high:<3}% {count:>5}  {bar}")
    lines.append("")
    lines.append(f"  median {percentile(shares, 0.5):.1%}   p75 {percentile(shares, 0.75):.1%}   "
                 f"p90 {percentile(shares, 0.90):.1%}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--segments", action="store_true", help="list segments and their sample sizes")
    ap.add_argument("--pool-ppr", action="store_true",
                    help=f"report {POOLED_PPR_LABEL} as one segment; also expands a bare "
                         "--ppr half_ppr or --ppr ppr to cover both")
    ap.add_argument("--keeper-distribution", action="store_true",
                    help="keeper share per draft, for setting the threshold from data")
    ap.add_argument("--season")
    ap.add_argument("--format", dest="league_format", choices=["redraft", "keeper", "dynasty", "unknown"])
    ap.add_argument("--superflex", type=int, choices=[0, 1])
    ap.add_argument("--ppr", help="one scoring type, or several comma-separated to POOL "
                                 "them (e.g. half_ppr,ppr). Pooling is labelled in the output")
    ap.add_argument("--teams", help="team count, or several comma-separated to pool")
    ap.add_argument("--compare-ppr", metavar="A,B",
                    help="check whether two scoring types price players the same, "
                         "before deciding to pool them (e.g. half_ppr,ppr)")
    ap.add_argument("--min-samples", type=int, default=3, help="drop players seen in fewer drafts")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--budget", type=int, help="reference budget for display (default REFERENCE_BUDGET)")
    ap.add_argument("--materialize", action="store_true", help="write results to the aav table")
    ap.add_argument("--offline", action="store_true", help="never fetch the players dump")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    conn = db.connect(cfg.db_path)

    if args.segments:
        if args.pool_ppr:
            print(f"(reception scoring pooled: {' + '.join(POOLED_PPR_TYPES)}. "
                  "Standard scoring stays separate.)\n")
        print(f"{'season':<8} {'format':<9} {'sf':<3} {'ppr':<10} {'teams':>5} "
              f"{'keep':>5} {'drafts':>7} {'picks':>7}")
        print("-" * 68)
        for r in list_segments(conn, pool_ppr=args.pool_ppr):
            print(f"{r['season'] or '?':<8} {r['league_format'] or '?':<9} {r['superflex']:<3} "
                  f"{r['ppr_type'] or '?':<10} {str(r['teams'] or '?'):>5} "
                  f"{r['max_keepers']:>5} {r['n_drafts']:>7} {r['n_picks']:>7}")
        print("\n'keep' is the most keepers any league in the segment allows. Nonzero on a "
              "\nredraft segment means some of those players never reached the block.")
        return 0

    if args.keeper_distribution:
        print(keeper_distribution(conn))
        return 0

    season = args.season or cfg.season
    ppr = args.ppr.split(",") if args.ppr else None
    teams = [int(t) for t in args.teams.split(",")] if args.teams else None
    if args.pool_ppr and ppr and set(ppr) <= set(POOLED_PPR_TYPES):
        ppr = list(POOLED_PPR_TYPES)
    if ppr and len(ppr) == 1:
        ppr = ppr[0]
    if teams and len(teams) == 1:
        teams = teams[0]

    if args.compare_ppr:
        pair = args.compare_ppr.split(",")
        if len(pair) != 2:
            print("--compare-ppr takes exactly two scoring types, e.g. half_ppr,ppr")
            return 1
        result = compare_dimension(
            conn, "ppr", pair, min_samples=args.min_samples, season=season,
            league_format=args.league_format, superflex=args.superflex, teams=teams)
        print(format_comparison(result, player_names(cfg, offline=args.offline), args.limit))
        return 0

    rows = aggregate(conn, season, args.league_format, args.superflex, ppr,
                     teams, min_samples=args.min_samples)

    if args.materialize:
        n = materialize(conn, rows, season, args.league_format, args.superflex, args.ppr, args.teams)
        print(f"materialized {n} rows into aav")

    names = player_names(cfg, offline=args.offline)
    budget = args.budget or cfg.reference_budget
    pooled = [n for n, v in (("ppr", ppr), ("teams", teams))
              if isinstance(v, list) and len(v) > 1]
    seg = (f"season={season} format={args.league_format or 'any'} "
           f"superflex={args.superflex if args.superflex is not None else 'any'} "
           f"ppr={args.ppr or 'any'} teams={args.teams or 'any'}")
    print(f"segment: {seg}   players: {len(rows)}")
    if pooled:
        print(f"POOLED across {', '.join(pooled)} — these are not one real league's prices. "
              f"Check --compare-ppr before trusting them.")
    print()
    print(format_table(rows, names, budget, args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
