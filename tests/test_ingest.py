import unittest

import db
import ingest
from config import Config
from tests import fixtures as fx


def cfg(**kw):
    return Config(**{"db_path": ":memory:", "season": "2025", "user_id_salt": "test-salt", **kw})


class ParseAmountTests(unittest.TestCase):
    def test_string_amount_is_the_assumed_shape(self):
        self.assertEqual(ingest.parse_amount({"metadata": {"amount": "54"}}), 54)

    def test_numeric_amounts(self):
        self.assertEqual(ingest.parse_amount({"metadata": {"amount": 54}}), 54)
        self.assertEqual(ingest.parse_amount({"metadata": {"amount": 54.0}}), 54)

    def test_dollar_sign_and_commas(self):
        self.assertEqual(ingest.parse_amount({"metadata": {"amount": "$1,054"}}), 1054)

    def test_missing_or_unusable_is_none(self):
        for pick in ({}, {"metadata": {}}, {"metadata": None},
                     {"metadata": {"amount": None}}, {"metadata": {"amount": ""}},
                     {"metadata": {"amount": "n/a"}}, {"metadata": {"amount": "0"}},
                     {"metadata": {"amount": True}}):
            self.assertIsNone(ingest.parse_amount(pick), pick)

    def test_snake_pick_has_no_bid(self):
        snake = fx.pick(1, "4046", amount=None)
        self.assertIsNone(ingest.parse_amount(snake))

    def test_alternate_keys_are_reported(self):
        alt = fx.pick(1, "4046", 30, amount_key="metadata.bid_amount")
        self.assertEqual(ingest.parse_amount(alt), 30)
        self.assertEqual(ingest.found_amount_key(alt), ("metadata", "bid_amount"))

    def test_metadata_amount_wins_over_alternates(self):
        pick = {"metadata": {"amount": "50", "bid_amount": "9"}, "amount": "1"}
        self.assertEqual(ingest.parse_amount(pick), 50)


class KeeperTests(unittest.TestCase):
    def test_is_keeper_variants(self):
        self.assertTrue(ingest.is_keeper({"is_keeper": True}))
        self.assertTrue(ingest.is_keeper({"metadata": {"is_keeper": "true"}}))
        self.assertFalse(ingest.is_keeper({"is_keeper": None}))
        self.assertFalse(ingest.is_keeper({"is_keeper": False, "metadata": {}}))


class ScreeningTests(unittest.TestCase):
    def _reason(self, draft):
        with self.assertRaises(ingest.Rejected) as ctx:
            ingest.screen_draft(draft)
        return ctx.exception.reason

    def test_snake_draft_rejected(self):
        self.assertIn("not an auction", self._reason(fx.draft(draft_type="snake")))

    def test_incomplete_draft_rejected(self):
        self.assertIn("not complete", self._reason(fx.draft(status="drafting")))

    def test_mock_draft_rejected(self):
        self.assertIn("mock draft", self._reason(fx.draft(league_id=None)))

    def test_zero_budget_rejected(self):
        self.assertIn("budget", self._reason(fx.draft(budget=0)))

    def test_real_auction_passes(self):
        ingest.screen_draft(fx.draft())


class NormalizeTests(unittest.TestCase):
    def test_price_is_percent_of_budget(self):
        rows, _ = ingest.normalize_picks(fx.auction_picks({"A": 54}), budget=200, keeper_threshold=0.4)
        self.assertAlmostEqual(rows[0]["pct_of_budget"], 27.0)

    def test_same_price_across_different_budgets(self):
        big, _ = ingest.normalize_picks(fx.auction_picks({"A": 54}), 200, 0.4)
        small, _ = ingest.normalize_picks(fx.auction_picks({"A": 27}), 100, 0.4)
        self.assertAlmostEqual(big[0]["pct_of_budget"], small[0]["pct_of_budget"])

    def test_keepers_are_dropped_from_prices(self):
        picks = fx.auction_picks({"A": 50, "B": 20}, keepers={"C": 5})
        rows, share = ingest.normalize_picks(picks, 200, 0.4)
        self.assertEqual({r["player_id"] for r in rows}, {"A", "B"})
        self.assertAlmostEqual(share, 1 / 3)

    def test_keeper_heavy_draft_is_rejected_whole(self):
        picks = fx.auction_picks({"A": 50}, keepers={"B": 5, "C": 5})
        with self.assertRaises(ingest.Rejected) as ctx:
            ingest.normalize_picks(picks, 200, 0.4)
        self.assertIn("keeper share", ctx.exception.reason)

    def test_threshold_boundary_is_inclusive(self):
        # 2 keepers of 5 picks = exactly 40%, which is not "above" the threshold.
        picks = fx.auction_picks({"A": 9, "B": 9, "C": 9}, keepers={"D": 5, "E": 5})
        rows, share = ingest.normalize_picks(picks, 200, 0.4)
        self.assertAlmostEqual(share, 0.4)
        self.assertEqual(len(rows), 3)

    def test_no_priced_picks_is_rejected_loudly(self):
        picks = [fx.pick(1, "A", amount=None)]
        with self.assertRaises(ingest.Rejected) as ctx:
            ingest.normalize_picks(picks, 200, 0.4)
        self.assertIn("parse_amount", ctx.exception.reason)


class IngestDraftTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.cfg = cfg()
        self.client = fx.FakeClient(
            drafts={"D1": fx.draft()},
            picks={"D1": fx.auction_picks({"4046": 54, "4034": 1})},
            leagues={"L1": fx.league(league_type=2, teams=12, rec=0.5, superflex=True)},
        )
        db.enqueue_draft(self.conn, "D1", "L1", "2025")

    def test_segments_are_recorded(self):
        row = ingest.ingest_draft(self.conn, self.client, "D1", self.cfg)
        self.assertEqual(row["league_format"], "dynasty")
        self.assertEqual(row["ppr_type"], "half_ppr")
        self.assertEqual(row["superflex"], 1)
        self.assertEqual(row["teams"], 12)
        self.assertEqual(row["included"], 1)

    def test_rerun_is_idempotent(self):
        ingest.ingest_draft(self.conn, self.client, "D1", self.cfg)
        first = db.counts(self.conn)
        ingest.ingest_draft(self.conn, self.client, "D1", self.cfg)
        self.assertEqual(db.counts(self.conn), first)
        self.assertEqual(first["picks"], 2)

    def test_reingest_removes_stale_picks(self):
        ingest.ingest_draft(self.conn, self.client, "D1", self.cfg)
        self.client.picks["D1"] = fx.auction_picks({"4046": 54})
        ingest.ingest_draft(self.conn, self.client, "D1", self.cfg)
        self.assertEqual(db.counts(self.conn)["picks"], 1)

    def test_excluded_draft_stores_its_reason_and_no_picks(self):
        self.client.drafts["D1"] = fx.draft(draft_type="snake")
        row = ingest.ingest_draft(self.conn, self.client, "D1", self.cfg)
        self.assertEqual(row["included"], 0)
        self.assertIn("not an auction", row["exclusion_reason"])
        self.assertEqual(db.counts(self.conn)["picks"], 0)

    def test_league_is_fetched_once_across_drafts(self):
        self.client.drafts["D2"] = fx.draft(draft_id="D2")
        self.client.picks["D2"] = fx.auction_picks({"4046": 30})
        db.enqueue_draft(self.conn, "D2", "L1", "2025")
        ingest.ingest_pending(self.conn, self.client, self.cfg)
        self.assertEqual(self.client.calls.count("league/L1"), 1)

    def test_queue_state_advances(self):
        ingest.ingest_pending(self.conn, self.client, self.cfg)
        self.assertEqual(db.pending_drafts(self.conn, "2025"), [])


if __name__ == "__main__":
    unittest.main()


class KeeperDistributionTests(unittest.TestCase):
    """A draft rejected for keepers still has to report its share — that is the
    evidence for replacing the 40% guess."""

    def test_rejected_draft_records_its_keeper_share(self):
        conn = db.connect(":memory:")
        client = fx.FakeClient(
            drafts={"D1": fx.draft()},
            picks={"D1": fx.auction_picks({"A": 50}, keepers={"B": 5, "C": 5, "D": 5})},
            leagues={"L1": fx.league()},
        )
        db.enqueue_draft(conn, "D1", "L1", "2025")
        row = ingest.ingest_draft(conn, client, "D1", cfg())
        self.assertEqual(row["included"], 0)
        self.assertAlmostEqual(row["keeper_share"], 0.75)
        stored = conn.execute("SELECT keeper_share FROM drafts WHERE draft_id='D1'").fetchone()[0]
        self.assertAlmostEqual(stored, 0.75)


class DefaultSeasonTests(unittest.TestCase):
    def test_default_season_is_the_current_year_not_a_stale_literal(self):
        from datetime import date

        import config
        self.assertEqual(config.default_season(), str(date.today().year))
        self.assertEqual(Config().season, str(date.today().year))


class MaxKeepersTests(unittest.TestCase):
    """A league typed redraft can still allow keepers — seen on real data."""

    def test_redraft_league_with_a_keeper_allowance_is_visible(self):
        league = fx.league(league_type=0)
        league["settings"]["max_keepers"] = 1
        conn = db.connect(":memory:")
        client = fx.FakeClient(
            drafts={"D1": fx.draft()},
            picks={"D1": fx.auction_picks({"A": 50, "B": 10})},
            leagues={"L1": league},
        )
        db.enqueue_draft(conn, "D1", "L1", "2025")
        row = ingest.ingest_draft(conn, client, "D1", cfg())
        self.assertEqual(row["league_format"], "redraft")
        self.assertEqual(row["max_keepers"], 1)

    def test_absent_max_keepers_is_none_not_zero(self):
        self.assertIsNone(ingest.max_keepers(fx.league()))
        self.assertIsNone(ingest.max_keepers(None))
        self.assertEqual(ingest.max_keepers({"settings": {"max_keepers": "2"}}), 2)

    def test_migration_adds_the_column_to_an_existing_database(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.sqlite3")
            old = sqlite3.connect(path)
            old.executescript(db.SCHEMA.replace(
                "    max_keepers      INTEGER,  -- league.settings.max_keepers; "
                "nonzero on 'redraft' leagues too\n", ""))
            old.execute("INSERT INTO drafts (draft_id) VALUES ('D_OLD')")
            old.commit()
            old.close()

            conn = db.connect(path)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(drafts)")}
            self.assertIn("max_keepers", columns)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM drafts WHERE draft_id='D_OLD'").fetchone()[0], 1)
