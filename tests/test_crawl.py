import unittest

import crawl
import db
from config import Config, ConfigError
from tests import fixtures as fx

CFG = Config(db_path=":memory:", season="2025", user_id_salt="test-salt",
             seed_league_id="SEED_LEAGUE")


def members(*user_ids):
    return [{"user_id": u, "display_name": f"name_{u}", "metadata": {"team_name": "Team"}}
            for u in user_ids]


class CrawlTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.client = fx.FakeClient(
            league_users={
                "SEED_LEAGUE": members("u1", "u2"),
                "L_AUCTION": members("u3"),
                "L_SNAKE": members("u4"),
            },
            user_drafts={
                "u1": [fx.draft("D1", "L_AUCTION")],
                "u2": [fx.draft("D2", "L_SNAKE", draft_type="snake")],
                "u3": [fx.draft("D3", "L_AUCTION2")],
                "u4": [fx.draft("D4", "L_SNAKE2", draft_type="snake")],
            },
            leagues={},
        )

    def crawler(self, **kw):
        return crawl.Crawler(self.conn, self.client, CFG, **kw)

    def test_finds_completed_auction_drafts(self):
        stats = self.crawler(max_drafts=10).run()
        queued = {r["draft_id"] for r in db.pending_drafts(self.conn, "2025")}
        self.assertIn("D1", queued)
        self.assertNotIn("D2", queued)   # snake
        self.assertEqual(stats["drafts_found"], len(queued))

    def test_expands_through_auction_leagues_only_by_default(self):
        self.crawler(max_drafts=10).run()
        # u3 is only reachable through L_AUCTION; u4 only through L_SNAKE.
        self.assertIn("user/u3/drafts", self.client.calls)
        self.assertNotIn("user/u4/drafts", self.client.calls)

    def test_expand_all_leagues_widens_the_walk(self):
        self.crawler(max_drafts=10, expand_all_leagues=True).run()
        self.assertIn("user/u4/drafts", self.client.calls)

    def test_max_drafts_stops_the_crawl(self):
        stats = self.crawler(max_drafts=1).run()
        self.assertEqual(stats["drafts_found"], 1)
        self.assertEqual(stats["stopped_because"], "max_drafts reached")

    def test_mock_drafts_are_not_queued(self):
        self.client.user_drafts["u1"] = [fx.draft("D_MOCK", league_id=None)]
        self.crawler(max_drafts=10).run()
        self.assertNotIn("D_MOCK", {r["draft_id"] for r in db.pending_drafts(self.conn, "2025")})

    def test_incomplete_drafts_are_not_queued(self):
        self.client.user_drafts["u1"] = [fx.draft("D_LIVE", "L1", status="drafting")]
        self.crawler(max_drafts=10).run()
        self.assertEqual(db.pending_drafts(self.conn, "2025"), [])

    def test_users_are_visited_once_across_runs(self):
        self.crawler(max_drafts=10).run()
        first = self.client.calls.count("user/u1/drafts")
        self.crawler(max_drafts=10).run()
        self.assertEqual(self.client.calls.count("user/u1/drafts"), first)

    def test_max_depth_bounds_the_walk(self):
        self.crawler(max_drafts=10, max_depth=1).run()
        self.assertIn("user/u1/drafts", self.client.calls)
        self.assertNotIn("user/u3/drafts", self.client.calls)

    def test_username_seed_resolves_then_is_discarded(self):
        cfg = Config(db_path=":memory:", season="2025", user_id_salt="s", seed_username="someone")
        self.client.users = {"someone": {"user_id": "u1", "display_name": "someone"}}
        crawler = crawl.Crawler(self.conn, self.client, cfg, max_drafts=10)
        crawler.run()
        self.assertIn("user/u1/drafts", self.client.calls)

    def test_no_seed_is_an_error(self):
        cfg = Config(db_path=":memory:", season="2025", user_id_salt="s")
        with self.assertRaises(ConfigError):
            crawl.Crawler(self.conn, self.client, cfg).run()

    def test_missing_salt_is_an_error(self):
        cfg = Config(db_path=":memory:", season="2025", seed_league_id="SEED_LEAGUE")
        with self.assertRaises(ConfigError):
            crawl.Crawler(self.conn, self.client, cfg).run()


if __name__ == "__main__":
    unittest.main()
