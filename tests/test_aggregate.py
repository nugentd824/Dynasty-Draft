import unittest

import aggregate
import db
import ingest
from config import Config
from tests import fixtures as fx

CFG = Config(db_path=":memory:", season="2025", user_id_salt="test-salt")


class PercentileTests(unittest.TestCase):
    def test_interpolates(self):
        v = [1.0, 2.0, 3.0, 4.0]
        self.assertAlmostEqual(aggregate.percentile(v, 0.25), 1.75)
        self.assertAlmostEqual(aggregate.percentile(v, 0.50), 2.5)
        self.assertAlmostEqual(aggregate.percentile(v, 0.75), 3.25)

    def test_single_value(self):
        self.assertEqual(aggregate.percentile([7.0], 0.5), 7.0)

    def test_endpoints(self):
        v = [1.0, 5.0, 9.0]
        self.assertEqual(aggregate.percentile(v, 0.0), 1.0)
        self.assertEqual(aggregate.percentile(v, 1.0), 9.0)


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        # Same player, same price, three different budgets. Normalization
        # should make these identical rather than averaging $54/$27/$108.
        specs = [
            ("D1", 200, {"star": 54, "scrub": 1}),
            ("D2", 100, {"star": 27, "scrub": 1}),
            ("D3", 400, {"star": 108, "scrub": 8}),
        ]
        client = fx.FakeClient(
            drafts={d: fx.draft(draft_id=d, league_id="L1", budget=b) for d, b, _ in specs},
            picks={d: fx.auction_picks(p, draft_id=d) for d, _, p in specs},
            leagues={"L1": fx.league(teams=12, rec=1.0)},
        )
        for draft_id, _, _ in specs:
            db.enqueue_draft(self.conn, draft_id, "L1", "2025")
        ingest.ingest_pending(self.conn, client, CFG)

    def rows(self, **kw):
        return {r["player_id"]: r for r in aggregate.aggregate(self.conn, **kw)}

    def test_equal_prices_across_budgets_collapse_to_one_value(self):
        star = self.rows(min_samples=1)["star"]
        self.assertAlmostEqual(star["mean_pct"], 27.0)
        self.assertAlmostEqual(star["median_pct"], 27.0)
        self.assertAlmostEqual(star["p25_pct"], 27.0)
        self.assertAlmostEqual(star["p75_pct"], 27.0)
        self.assertEqual(star["n_picks"], 3)
        self.assertEqual(star["n_drafts"], 3)

    def test_pct_above_min_uses_raw_dollars(self):
        # scrub went for $1, $1, $8 — only the last is above the floor.
        scrub = self.rows(min_samples=1)["scrub"]
        self.assertAlmostEqual(scrub["pct_above_min"], 1 / 3)
        self.assertAlmostEqual(self.rows(min_samples=1)["star"]["pct_above_min"], 1.0)

    def test_min_samples_filters(self):
        self.assertEqual(self.rows(min_samples=4), {})

    def test_sorted_by_mean_descending(self):
        ordered = [r["player_id"] for r in aggregate.aggregate(self.conn, min_samples=1)]
        self.assertEqual(ordered, ["star", "scrub"])

    def test_excluded_drafts_do_not_contribute(self):
        self.conn.execute("UPDATE drafts SET included = 0 WHERE draft_id = 'D3'")
        scrub = self.rows(min_samples=1)["scrub"]
        self.assertEqual(scrub["n_picks"], 2)
        self.assertEqual(scrub["pct_above_min"], 0.0)


class SegmentTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        client = fx.FakeClient(
            drafts={"D1": fx.draft("D1", "L_RED"), "D2": fx.draft("D2", "L_DYN")},
            picks={
                "D1": fx.auction_picks({"star": 40}, draft_id="D1"),
                "D2": fx.auction_picks({"star": 80}, draft_id="D2"),
            },
            leagues={
                "L_RED": fx.league("L_RED", league_type=0, rec=1.0, superflex=False),
                "L_DYN": fx.league("L_DYN", league_type=2, rec=1.0, superflex=True),
            },
        )
        for draft_id, league_id in (("D1", "L_RED"), ("D2", "L_DYN")):
            db.enqueue_draft(self.conn, draft_id, league_id, "2025")
        ingest.ingest_pending(self.conn, client, CFG)

    def test_segments_are_listed_separately(self):
        segs = aggregate.list_segments(self.conn)
        self.assertEqual(len(segs), 2)
        self.assertEqual({s["league_format"] for s in segs}, {"redraft", "dynasty"})

    def test_filtering_keeps_markets_apart(self):
        redraft = aggregate.aggregate(self.conn, league_format="redraft", min_samples=1)
        dynasty = aggregate.aggregate(self.conn, league_format="dynasty", min_samples=1)
        self.assertAlmostEqual(redraft[0]["mean_pct"], 20.0)
        self.assertAlmostEqual(dynasty[0]["mean_pct"], 40.0)

    def test_unfiltered_pools_them_which_is_why_we_filter(self):
        both = aggregate.aggregate(self.conn, min_samples=1)
        self.assertEqual(both[0]["n_picks"], 2)
        self.assertAlmostEqual(both[0]["mean_pct"], 30.0)

    def test_materialize_is_idempotent(self):
        rows = aggregate.aggregate(self.conn, season="2025", league_format="redraft", min_samples=1)
        for _ in range(2):
            aggregate.materialize(self.conn, rows, "2025", "redraft", 0, "ppr", 12)
        n = self.conn.execute("SELECT COUNT(*) FROM aav").fetchone()[0]
        self.assertEqual(n, len(rows))


if __name__ == "__main__":
    unittest.main()


class PoolingTests(unittest.TestCase):
    """Pooling segments is available but never implicit, and comes with a way
    to check whether the segments being pooled actually agree."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        # Same player, systematically dearer in full PPR than half.
        specs = [
            ("D1", "L_HALF", 0.5, {"wr": 40, "qb": 20}),
            ("D2", "L_HALF", 0.5, {"wr": 44, "qb": 20}),
            ("D3", "L_PPR", 1.0, {"wr": 60, "qb": 21}),
            ("D4", "L_PPR", 1.0, {"wr": 64, "qb": 19}),
        ]
        client = fx.FakeClient(
            drafts={d: fx.draft(d, lg, budget=200) for d, lg, _, _ in specs},
            picks={d: fx.auction_picks(p, draft_id=d) for d, _, _, p in specs},
            leagues={"L_HALF": fx.league("L_HALF", rec=0.5),
                     "L_PPR": fx.league("L_PPR", rec=1.0)},
        )
        for draft_id, league_id, _, _ in specs:
            db.enqueue_draft(self.conn, draft_id, league_id, "2025")
        ingest.ingest_pending(self.conn, client, CFG)

    def test_a_list_pools_the_values(self):
        half = aggregate.aggregate(self.conn, ppr="half_ppr", min_samples=1)
        both = aggregate.aggregate(self.conn, ppr=["half_ppr", "ppr"], min_samples=1)
        self.assertEqual({r["player_id"]: r["n_picks"] for r in half}["wr"], 2)
        self.assertEqual({r["player_id"]: r["n_picks"] for r in both}["wr"], 4)

    def test_pooling_hides_a_real_difference(self):
        half = {r["player_id"]: r for r in aggregate.aggregate(self.conn, ppr="half_ppr", min_samples=1)}
        full = {r["player_id"]: r for r in aggregate.aggregate(self.conn, ppr="ppr", min_samples=1)}
        both = {r["player_id"]: r for r in aggregate.aggregate(self.conn, ppr=["half_ppr", "ppr"], min_samples=1)}
        self.assertAlmostEqual(half["wr"]["mean_pct"], 21.0)
        self.assertAlmostEqual(full["wr"]["mean_pct"], 31.0)
        # The pooled mean matches neither market.
        self.assertAlmostEqual(both["wr"]["mean_pct"], 26.0)

    def test_comparison_surfaces_the_divergent_player(self):
        result = aggregate.compare_dimension(
            self.conn, "ppr", ["half_ppr", "ppr"], min_samples=1, season="2025")
        self.assertEqual(result["shared_players"], 2)
        biggest = result["rows"][0]
        self.assertEqual(biggest["player_id"], "wr")
        self.assertAlmostEqual(biggest["delta_pct"], 10.0)
        # The QB barely moves between scoring types, as expected.
        qb = next(r for r in result["rows"] if r["player_id"] == "qb")
        self.assertLess(abs(qb["delta_pct"]), 1.0)

    def test_comparison_reports_no_overlap_honestly(self):
        result = aggregate.compare_dimension(
            self.conn, "ppr", ["half_ppr", "standard"], min_samples=1, season="2025")
        self.assertEqual(result["shared_players"], 0)
        self.assertIn("Pooling would be a guess", aggregate.format_comparison(result, {}))

    def test_empty_filter_list_is_ignored(self):
        where, params = aggregate._segment_where(ppr=[])
        self.assertNotIn("ppr_type", where)
        self.assertEqual(params, [])

    def test_segment_listing_can_pool_reception_scoring(self):
        split = aggregate.list_segments(self.conn)
        pooled = aggregate.list_segments(self.conn, pool_ppr=True)
        self.assertEqual(len(split), 2)
        self.assertEqual({r["ppr_type"] for r in split}, {"half_ppr", "ppr"})
        # One row, not two rows wearing the same label: the counts must actually
        # merge, or the pooling is cosmetic.
        self.assertEqual(len(pooled), 1)
        self.assertEqual(pooled[0]["ppr_type"], aggregate.POOLED_PPR_LABEL)
        self.assertEqual(pooled[0]["n_drafts"], 4)
        self.assertEqual(pooled[0]["n_picks"], sum(r["n_picks"] for r in split))

    def test_standard_scoring_is_not_pooled_in(self):
        # Standard is a different market for anyone who catches the ball.
        self.assertNotIn("standard", aggregate.POOLED_PPR_TYPES)
