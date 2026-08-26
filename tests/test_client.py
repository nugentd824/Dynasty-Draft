import json
import logging
import unittest

import requests

from client import RateLimiter, SleeperClient, SleeperError

# These tests drive the retry paths on purpose; their warnings are noise here.
logging.getLogger("client").setLevel(logging.CRITICAL)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, body=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._body = body

    def json(self):
        if self._body is not None:
            return json.loads(self._body)
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class RateLimiterTests(unittest.TestCase):
    def test_holds_the_line_at_the_configured_rate(self):
        clock = Clock()
        limiter = RateLimiter(3, clock=clock, sleeper=clock.sleep)
        for _ in range(3):
            limiter.acquire()
        self.assertEqual(clock.t, 0.0)
        limiter.acquire()          # fourth call has to wait out the window
        self.assertGreaterEqual(clock.t, 60.0)

    def test_window_slides(self):
        clock = Clock()
        limiter = RateLimiter(2, clock=clock, sleeper=clock.sleep)
        limiter.acquire()
        limiter.acquire()
        clock.t = 61.0             # first two have aged out
        limiter.acquire()
        self.assertEqual(clock.t, 61.0)

    def test_default_is_well_under_sleepers_ceiling(self):
        from client import DEFAULT_RATE_LIMIT_PER_MIN
        self.assertLessEqual(DEFAULT_RATE_LIMIT_PER_MIN, 300)


class ClientTests(unittest.TestCase):
    def client(self, responses, **kw):
        clock = Clock()
        return SleeperClient(
            session=FakeSession(responses),
            limiter=RateLimiter(1000, clock=clock, sleeper=clock.sleep),
            sleeper=clock.sleep,
            **kw,
        ), clock

    def test_ok_returns_json(self):
        c, _ = self.client([FakeResponse(200, {"user_id": "1"})])
        self.assertEqual(c.get_user("someone"), {"user_id": "1"})

    def test_404_is_none_not_an_error(self):
        c, _ = self.client([FakeResponse(404)])
        self.assertIsNone(c.get_league("nope"))

    def test_null_list_endpoints_return_empty_list(self):
        c, _ = self.client([FakeResponse(200, None)])
        self.assertEqual(c.get_draft_picks("D1"), [])

    def test_429_is_retried_with_backoff(self):
        c, clock = self.client([FakeResponse(429), FakeResponse(200, [{"draft_id": "D1"}])])
        self.assertEqual(c.get_user_drafts("u1", "2025"), [{"draft_id": "D1"}])
        self.assertGreater(clock.t, 0)

    def test_retry_after_header_is_honoured(self):
        c, clock = self.client([FakeResponse(429, headers={"Retry-After": "30"}),
                                FakeResponse(200, {})])
        c.get_draft("D1")
        self.assertGreaterEqual(clock.t, 30.0)

    def test_gives_up_after_max_retries(self):
        c, _ = self.client([FakeResponse(503)] * 6, max_retries=2)
        with self.assertRaises(SleeperError):
            c.get_draft("D1")

    def test_network_errors_are_retried(self):
        c, _ = self.client([requests.ConnectionError("boom"), FakeResponse(200, {"ok": 1})])
        self.assertEqual(c.get_draft("D1"), {"ok": 1})

    def test_client_error_raises(self):
        c, _ = self.client([FakeResponse(400)])
        with self.assertRaises(SleeperError):
            c.get_draft("D1")

    def test_requests_are_counted(self):
        c, _ = self.client([FakeResponse(429), FakeResponse(200, {})])
        c.get_draft("D1")
        self.assertEqual(c.request_count, 2)

    def test_urls_are_built_against_the_v1_base(self):
        c, _ = self.client([FakeResponse(200, [])])
        c.get_user_drafts("u1", "2025")
        self.assertEqual(c.session.urls[-1], "https://api.sleeper.app/v1/user/u1/drafts/nfl/2025")


class PlayerCacheTests(unittest.TestCase):
    def test_cached_dump_is_not_refetched(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "players.json"
            clock = Clock()
            c = SleeperClient(
                session=FakeSession([FakeResponse(200, {"4046": {"full_name": "A B"}})]),
                limiter=RateLimiter(1000, clock=clock, sleeper=clock.sleep),
                sleeper=clock.sleep,
            )
            first = c.get_players(str(path))
            self.assertEqual(first, {"4046": {"full_name": "A B"}})
            # A second call with an empty response queue would raise IndexError
            # if it hit the network at all.
            self.assertEqual(c.get_players(str(path)), first)
            self.assertEqual(c.request_count, 1)


if __name__ == "__main__":
    unittest.main()


class StateTests(unittest.TestCase):
    """The one endpoint confirmed against a real response so far.

    Captured 2026-08-26 from https://api.sleeper.app/v1/state/nfl — the first
    live Sleeper data this project has seen. `metadata.amount` on auction picks
    is still unconfirmed; see verify.py.
    """

    REAL_RESPONSE = {
        "week": 3, "leg": 0, "season": "2026", "season_type": "pre",
        "league_season": "2026", "previous_season": "2025",
        "season_start_date": "2026-08-06", "display_week": 3,
        "league_create_season": "2026", "season_has_scores": True,
    }

    def test_state_is_parsed(self):
        clock = Clock()
        c = SleeperClient(
            session=FakeSession([FakeResponse(200, self.REAL_RESPONSE)]),
            limiter=RateLimiter(1000, clock=clock, sleeper=clock.sleep),
            sleeper=clock.sleep,
        )
        state = c.get_state()
        self.assertEqual(state["league_season"], "2026")
        self.assertEqual(c.session.urls[-1], "https://api.sleeper.app/v1/state/nfl")

    def test_league_season_is_the_one_to_crawl(self):
        # season and league_season can disagree around the new year; drafts are
        # filed under league_season, so that is what the crawl follows.
        self.assertEqual(self.REAL_RESPONSE["league_season"], "2026")
        self.assertEqual(self.REAL_RESPONSE["previous_season"], "2025")
