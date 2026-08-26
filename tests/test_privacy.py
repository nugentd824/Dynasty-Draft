"""The privacy rules in CLAUDE.md are non-negotiable, so they get tests.

We are reading strangers' league data through a public API. The dataset should
contain prices and nothing else that identifies anyone.
"""

import pathlib
import re
import sqlite3
import unittest

import crawl
import db
import ingest
from config import Config
from tests import fixtures as fx

REPO = pathlib.Path(__file__).resolve().parent.parent

CFG = Config(db_path=":memory:", season="2025", user_id_salt="test-salt",
             seed_league_id="SEED_LEAGUE")


def dump_all_values(conn: sqlite3.Connection) -> str:
    """Every value in every table, as one blob of text."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    chunks = []
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table}"):
            chunks.extend(str(v) for v in tuple(row))
    return "\n".join(chunks)


class StoredDataTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.client = fx.FakeClient(
            league_users={"SEED_LEAGUE": [
                {"user_id": "u1", "display_name": "RealPersonHandle",
                 "metadata": {"team_name": "SomeTeamName"}, "avatar": "abc"},
            ]},
            user_drafts={"u1": [fx.draft("D1", "L1")]},
            drafts={"D1": fx.draft("D1", "L1")},
            picks={"D1": fx.auction_picks({"4046": 54})},
            leagues={"L1": fx.league("L1")},
        )
        crawl.Crawler(self.conn, self.client, CFG, max_drafts=5).run()
        ingest.ingest_pending(self.conn, self.client, CFG)

    def test_no_display_names_or_team_names_are_stored(self):
        blob = dump_all_values(self.conn)
        for secret in ("RealPersonHandle", "SomeTeamName", "Some League Name", "Some Draft Name"):
            self.assertNotIn(secret, blob, f"{secret!r} leaked into the database")

    def test_raw_user_ids_are_never_stored(self):
        rows = self.conn.execute("SELECT user_hash FROM seen_users").fetchall()
        self.assertTrue(rows)
        for (user_hash,) in rows:
            self.assertNotEqual(user_hash, "u1")
            self.assertRegex(user_hash, r"^[0-9a-f]{64}$")

    def test_picked_by_is_dropped(self):
        # Fixture picks carry picked_by; the picks table has no such column.
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(picks)")}
        self.assertNotIn("picked_by", columns)
        self.assertNotIn("u1", dump_all_values(self.conn))

    def test_hashing_is_salted_and_stable(self):
        a = Config(user_id_salt="salt-a").hash_user_id("u1")
        b = Config(user_id_salt="salt-b").hash_user_id("u1")
        self.assertNotEqual(a, b)
        self.assertEqual(a, Config(user_id_salt="salt-a").hash_user_id("u1"))


class SourceTests(unittest.TestCase):
    """A league ID is the key to that league's full history with no auth.
    It belongs in .env, never in a tracked file."""

    SOURCE_FILES = sorted(
        p for p in REPO.glob("*.py") if p.name != "conftest.py"
    ) + sorted((REPO / "tests").glob("*.py"))

    def test_no_long_numeric_ids_hardcoded_in_source(self):
        # Real Sleeper league/user/draft IDs are ~18-digit snowflakes.
        snowflake = re.compile(r"\b\d{15,20}\b")
        for path in self.SOURCE_FILES:
            hits = snowflake.findall(path.read_text())
            self.assertEqual(hits, [], f"possible hardcoded Sleeper ID in {path.name}: {hits}")

    def test_seeds_come_from_the_environment(self):
        text = (REPO / "config.py").read_text()
        self.assertIn("SLEEPER_SEED_LEAGUE_ID", text)
        self.assertIn("SLEEPER_SEED_USERNAME", text)
        cfg = Config()
        self.assertEqual(cfg.seed_league_id, "")
        self.assertEqual(cfg.seed_username, "")

    def test_env_and_db_are_gitignored(self):
        ignored = (REPO / ".gitignore").read_text()
        self.assertIn(".env", ignored)
        self.assertIn("*.sqlite3", ignored)


if __name__ == "__main__":
    unittest.main()
