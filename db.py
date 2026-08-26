"""SQLite storage. Schema plus idempotent writes.

Everything here is designed so a rerun overwrites rather than duplicates —
crawls get interrupted, and re-ingesting a draft must be a no-op.

Privacy: no usernames, display names, or team names are stored anywhere. User
IDs appear only as salted hashes, and only to dedupe the crawl.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    draft_id         TEXT PRIMARY KEY,
    league_id        TEXT,
    season           TEXT,
    draft_type       TEXT,
    status           TEXT,
    budget           INTEGER,
    teams            INTEGER,
    league_format    TEXT,     -- redraft | keeper | dynasty
    superflex        INTEGER,  -- 0/1
    ppr_type         TEXT,     -- standard | half_ppr | ppr | custom
    scoring_rec      REAL,
    keeper_share     REAL,
    max_keepers      INTEGER,  -- league.settings.max_keepers; nonzero on 'redraft' leagues too
    pick_count       INTEGER,
    priced_picks     INTEGER,
    included         INTEGER,  -- 1 = contributes to aggregation
    exclusion_reason TEXT,
    ingested_at      TEXT
);

CREATE TABLE IF NOT EXISTS picks (
    draft_id      TEXT NOT NULL,
    pick_no       INTEGER NOT NULL,
    player_id     TEXT NOT NULL,
    amount        INTEGER NOT NULL,   -- raw winning bid, in that league's dollars
    pct_of_budget REAL NOT NULL,      -- 0-100, the comparable number
    is_keeper     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (draft_id, pick_no),
    FOREIGN KEY (draft_id) REFERENCES drafts(draft_id)
);
CREATE INDEX IF NOT EXISTS idx_picks_player ON picks(player_id);

-- Crawl bookkeeping. Deliberately holds hashes, not IDs: a visited user is a
-- fact we need for dedupe, but who they are is not.
CREATE TABLE IF NOT EXISTS seen_users (
    user_hash    TEXT NOT NULL,
    season       TEXT NOT NULL,
    depth        INTEGER,
    discovered_at TEXT,
    -- Keyed by season as well as hash: the same person has different drafts
    -- each year, so a 2025 crawl must revisit everyone a 2026 crawl saw.
    PRIMARY KEY (user_hash, season)
);

CREATE TABLE IF NOT EXISTS seen_leagues (
    league_id    TEXT PRIMARY KEY,
    season       TEXT,
    discovered_at TEXT
);

CREATE TABLE IF NOT EXISTS draft_queue (
    draft_id     TEXT PRIMARY KEY,
    league_id    TEXT,
    season       TEXT,
    state        TEXT,   -- discovered | ingested | skipped
    discovered_at TEXT
);

CREATE TABLE IF NOT EXISTS aav (
    season         TEXT,
    league_format  TEXT,
    superflex      INTEGER,
    ppr_type       TEXT,
    teams          INTEGER,
    player_id      TEXT,
    n_picks        INTEGER,
    n_drafts       INTEGER,
    mean_pct       REAL,
    median_pct     REAL,
    p25_pct        REAL,
    p75_pct        REAL,
    min_pct        REAL,
    max_pct        REAL,
    pct_above_min  REAL,
    computed_at    TEXT,
    PRIMARY KEY (season, league_format, superflex, ppr_type, teams, player_id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Columns added after the first schema shipped. CREATE TABLE IF NOT EXISTS
# will not add them to a database that already exists, so they go on by hand.
MIGRATIONS = (
    ("drafts", "max_keepers", "INTEGER"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in MIGRATIONS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # seen_users was keyed on user_hash alone, which made a second season's
    # crawl skip everyone the first had visited. Rebuild it with (hash, season).
    pk = [r[1] for r in conn.execute("PRAGMA table_info(seen_users)") if r[5]]
    if pk == ["user_hash"]:
        conn.executescript("""
            ALTER TABLE seen_users RENAME TO seen_users_old;
            CREATE TABLE seen_users (
                user_hash    TEXT NOT NULL,
                season       TEXT NOT NULL,
                depth        INTEGER,
                discovered_at TEXT,
                PRIMARY KEY (user_hash, season)
            );
            INSERT OR IGNORE INTO seen_users (user_hash, season, depth, discovered_at)
                SELECT user_hash, COALESCE(season, ''), depth, discovered_at
                FROM seen_users_old;
            DROP TABLE seen_users_old;
        """)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# -- crawl bookkeeping -----------------------------------------------------

def mark_user_seen(conn: sqlite3.Connection, user_hash: str, season: str, depth: int) -> bool:
    """Record a visited user. Returns True if this hash is new to us."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO seen_users (user_hash, season, depth, discovered_at) "
        "VALUES (?, ?, ?, ?)",
        (user_hash, season, depth, now_iso()),
    )
    return cur.rowcount > 0


def user_seen(conn: sqlite3.Connection, user_hash: str, season: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM seen_users WHERE user_hash = ? AND season = ?",
        (user_hash, season),
    ).fetchone()
    return row is not None


def mark_league_seen(conn: sqlite3.Connection, league_id: str, season: str) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO seen_leagues (league_id, season, discovered_at) VALUES (?, ?, ?)",
        (league_id, season, now_iso()),
    )
    return cur.rowcount > 0


def reset_frontier(conn: sqlite3.Connection, season: str) -> dict:
    """Forget which users and leagues have been walked, for one season.

    Needed to crawl deeper than a previous run. User IDs are never stored — only
    hashes — so the frontier lives solely in memory during a run and cannot be
    resumed outward afterwards. Clearing the bookkeeping lets a fresh, deeper
    walk proceed; drafts and picks are untouched, and re-ingest is idempotent.
    """
    users = conn.execute("DELETE FROM seen_users WHERE season = ?", (season,)).rowcount
    leagues = conn.execute("DELETE FROM seen_leagues WHERE season = ?", (season,)).rowcount
    conn.commit()
    return {"users_cleared": users, "leagues_cleared": leagues}


def enqueue_draft(conn: sqlite3.Connection, draft_id: str, league_id: str, season: str) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO draft_queue (draft_id, league_id, season, state, discovered_at) "
        "VALUES (?, ?, ?, 'discovered', ?)",
        (draft_id, league_id, season, now_iso()),
    )
    return cur.rowcount > 0


def set_draft_state(conn: sqlite3.Connection, draft_id: str, state: str) -> None:
    conn.execute("UPDATE draft_queue SET state = ? WHERE draft_id = ?", (state, draft_id))


def pending_drafts(conn: sqlite3.Connection, season: Optional[str] = None,
                   limit: Optional[int] = None, include_ingested: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM draft_queue WHERE 1=1"
    params: list = []
    if not include_ingested:
        sql += " AND state = 'discovered'"
    if season:
        sql += " AND season = ?"
        params.append(season)
    sql += " ORDER BY discovered_at"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql, params))


# -- ingest ----------------------------------------------------------------

def upsert_draft(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO drafts (draft_id, league_id, season, draft_type, status, budget, teams,
                            league_format, superflex, ppr_type, scoring_rec, keeper_share,
                            max_keepers, pick_count, priced_picks, included, exclusion_reason,
                            ingested_at)
        VALUES (:draft_id, :league_id, :season, :draft_type, :status, :budget, :teams,
                :league_format, :superflex, :ppr_type, :scoring_rec, :keeper_share,
                :max_keepers, :pick_count, :priced_picks, :included, :exclusion_reason,
                :ingested_at)
        ON CONFLICT(draft_id) DO UPDATE SET
            league_id=excluded.league_id, season=excluded.season,
            draft_type=excluded.draft_type, status=excluded.status,
            budget=excluded.budget, teams=excluded.teams,
            league_format=excluded.league_format, superflex=excluded.superflex,
            ppr_type=excluded.ppr_type, scoring_rec=excluded.scoring_rec,
            keeper_share=excluded.keeper_share, max_keepers=excluded.max_keepers,
            pick_count=excluded.pick_count,
            priced_picks=excluded.priced_picks, included=excluded.included,
            exclusion_reason=excluded.exclusion_reason, ingested_at=excluded.ingested_at
        """,
        {**row, "ingested_at": now_iso()},
    )


def replace_picks(conn: sqlite3.Connection, draft_id: str, picks: Iterable[dict]) -> int:
    """Write a draft's picks, replacing whatever was there.

    Delete-then-insert rather than upsert so a re-ingest that yields fewer
    picks (a corrected parse, say) doesn't leave orphans behind.
    """
    conn.execute("DELETE FROM picks WHERE draft_id = ?", (draft_id,))
    rows = [
        (draft_id, p["pick_no"], p["player_id"], p["amount"], p["pct_of_budget"],
         1 if p.get("is_keeper") else 0)
        for p in picks
    ]
    conn.executemany(
        "INSERT INTO picks (draft_id, pick_no, player_id, amount, pct_of_budget, is_keeper) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def counts(conn: sqlite3.Connection) -> dict:
    def one(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {
        "queued": one("SELECT COUNT(*) FROM draft_queue WHERE state='discovered'"),
        "drafts": one("SELECT COUNT(*) FROM drafts"),
        "drafts_included": one("SELECT COUNT(*) FROM drafts WHERE included=1"),
        "picks": one("SELECT COUNT(*) FROM picks"),
        "users_seen": one("SELECT COUNT(*) FROM seen_users"),
        "leagues_seen": one("SELECT COUNT(*) FROM seen_leagues"),
    }
