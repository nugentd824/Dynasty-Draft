#!/usr/bin/env python3
"""Discover completed auction drafts by walking the league social graph.

There is no draft search endpoint. The only discovery route is
`/user/<id>/drafts/nfl/<season>`, so the crawl starts from a seed you supply
and expands outward: a league gives you its members, each member gives you
their drafts, each auction draft gives you another league. That constraint
shapes everything here.

Usage:
    python3 crawl.py --max-drafts 5 --verbose
    python3 crawl.py --max-drafts 500 --max-depth 4
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from collections import deque
from typing import Optional

import db
from client import SleeperClient
from config import Config, ConfigError

log = logging.getLogger("crawl")


class Crawler:
    def __init__(self, conn: sqlite3.Connection, client: SleeperClient, cfg: Config,
                 max_drafts: int = 50, max_depth: int = 3, max_users: int = 2000,
                 expand_all_leagues: bool = False) -> None:
        self.conn = conn
        self.client = client
        self.cfg = cfg
        self.max_drafts = max_drafts
        self.max_depth = max_depth
        self.max_users = max_users
        # By default the graph is only expanded through leagues that actually
        # ran an auction — auction players cluster together, and following
        # every snake league burns the request budget for nothing.
        self.expand_all_leagues = expand_all_leagues

        self.drafts_found = 0
        self.users_visited = 0
        self.auction_leagues = 0
        self._queue: deque[tuple[str, int]] = deque()   # (user_id, depth), in memory only
        self._queued_ids: set[str] = set()

    # -- seeding -----------------------------------------------------------

    def seed(self) -> None:
        """Load the frontier from SLEEPER_SEED_LEAGUE_ID / SLEEPER_SEED_USERNAME."""
        self.cfg.require_seed()
        reachable = 0

        if self.cfg.seed_league_id:
            log.info("seeding from configured league")
            # force=True: on a resumed crawl the seed league is already in
            # seen_leagues, and skipping it would leave the frontier empty.
            reachable += self._expand_league(self.cfg.seed_league_id, depth=0, force=True)

        if self.cfg.seed_username:
            user = self.client.get_user(self.cfg.seed_username)
            if not user or not user.get("user_id"):
                log.warning("seed username did not resolve to a user")
            else:
                # The username is resolved once and dropped here; only the ID
                # travels on, and only its hash is ever written down.
                log.info("seeding from configured username")
                reachable += 1
                self._enqueue_user(str(user["user_id"]), depth=0)

        if not reachable:
            raise ConfigError(
                "seed produced no users — check SLEEPER_SEED_LEAGUE_ID / SLEEPER_SEED_USERNAME"
            )
        if not self._queue:
            # Everyone the seed reaches has been visited on an earlier run.
            # That is a finished crawl, not a broken one.
            log.info("frontier empty: every user reachable from the seed is already crawled")

    # -- graph -------------------------------------------------------------

    def _enqueue_user(self, user_id: str, depth: int) -> None:
        if depth > self.max_depth or user_id in self._queued_ids:
            return
        if db.user_seen(self.conn, self.cfg.hash_user_id(user_id)):
            return
        self._queued_ids.add(user_id)
        self._queue.append((user_id, depth))

    def _expand_league(self, league_id: str, depth: int, force: bool = False) -> int:
        """Add a league's members to the frontier. Returns how many it had."""
        if depth > self.max_depth:
            return 0
        fresh = db.mark_league_seen(self.conn, league_id, self.cfg.season)
        if not fresh and not force:
            return 0
        members = 0
        for member in self.client.get_league_users(league_id):
            user_id = member.get("user_id")
            # `member` also carries display_name, avatar, and team metadata.
            # None of it is read, and none of it is stored.
            if user_id:
                members += 1
                self._enqueue_user(str(user_id), depth + 1)
        return members

    def _visit_user(self, user_id: str, depth: int) -> None:
        user_hash = self.cfg.hash_user_id(user_id)
        if not db.mark_user_seen(self.conn, user_hash, self.cfg.season, depth):
            return
        self.users_visited += 1

        drafts = self.client.get_user_drafts(user_id, self.cfg.season)
        log.debug("user %s… depth=%d: %d draft(s)", user_hash[:8], depth, len(drafts))

        for draft in drafts:
            league_id = draft.get("league_id")
            draft_id = draft.get("draft_id")
            if not draft_id:
                continue
            is_auction = draft.get("type") == "auction"
            complete = draft.get("status") == "complete"

            if is_auction and complete and league_id:
                if db.enqueue_draft(self.conn, str(draft_id), str(league_id), self.cfg.season):
                    self.drafts_found += 1
                    log.info("found auction draft %s (%d/%s)",
                             draft_id, self.drafts_found, self.max_drafts)
            elif is_auction and not league_id:
                log.debug("skip mock auction %s (no league_id)", draft_id)

            if league_id and (self.expand_all_leagues or is_auction):
                if is_auction:
                    self.auction_leagues += 1
                self._expand_league(str(league_id), depth)

    # -- driver ------------------------------------------------------------

    def run(self) -> dict:
        self.seed()
        while self._queue and self.drafts_found < self.max_drafts and self.users_visited < self.max_users:
            user_id, depth = self._queue.popleft()
            self._queued_ids.discard(user_id)
            self._visit_user(user_id, depth)
            self.conn.commit()
        self.conn.commit()

        stop = ("max_drafts reached" if self.drafts_found >= self.max_drafts
                else "max_users reached" if self.users_visited >= self.max_users
                else "frontier exhausted")
        return {
            "drafts_found": self.drafts_found,
            "users_visited": self.users_visited,
            "auction_leagues": self.auction_leagues,
            "requests": self.client.request_count,
            "frontier_remaining": len(self._queue),
            "stopped_because": stop,
        }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-drafts", type=int, default=50, help="stop after discovering this many auction drafts")
    ap.add_argument("--max-depth", type=int, default=3, help="how far out through the social graph to walk")
    ap.add_argument("--max-users", type=int, default=2000, help="hard cap on users visited")
    ap.add_argument("--season", help="override SLEEPER_SEASON")
    ap.add_argument("--expand-all-leagues", action="store_true",
                    help="follow snake leagues too (wider, much more expensive)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = Config.from_env()
    if args.season:
        cfg = Config(**{**cfg.__dict__, "season": args.season})

    conn = db.connect(cfg.db_path)
    client = SleeperClient(rate_limit_per_min=cfg.rate_limit_per_min)
    crawler = Crawler(
        conn, client, cfg,
        max_drafts=args.max_drafts,
        max_depth=args.max_depth,
        max_users=args.max_users,
        expand_all_leagues=args.expand_all_leagues,
    )

    log.info("crawling season %s at %d req/min", cfg.season, cfg.rate_limit_per_min)
    stats = crawler.run()

    print("\ncrawl finished:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    print(f"  db totals: {db.counts(conn)}")
    print("\nnext: python3 verify.py   (confirm the bid field before scaling up)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
