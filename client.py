"""The only place in this project that touches the network.

Rate limiting, retries, and the players-dump cache all live here so there is
one place to reason about them. Don't call `requests` directly elsewhere.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.sleeper.app/v1"

# Sleeper's stated ceiling is 1000/min before an IP block. 300 is the default
# and deliberately conservative — see CLAUDE.md, "Hard constraints".
DEFAULT_RATE_LIMIT_PER_MIN = 300

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
PLAYER_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60


class SleeperError(RuntimeError):
    """A request failed in a way retrying would not fix."""


class RateLimiter:
    """Sliding-window limiter: at most `per_minute` acquisitions in any 60s.

    Takes a clock and a sleep function so tests can drive it without waiting.
    """

    def __init__(
        self,
        per_minute: int = DEFAULT_RATE_LIMIT_PER_MIN,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be >= 1")
        self.per_minute = per_minute
        self._clock = clock
        self._sleep = sleeper
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= 60.0:
                    self._calls.popleft()
                if len(self._calls) < self.per_minute:
                    self._calls.append(now)
                    return
                wait = 60.0 - (now - self._calls[0])
            self._sleep(max(wait, 0.01))


class SleeperClient:
    """Read-only client for the public Sleeper API.

    The API has no write surface we use and no ADP/AAV/projection endpoints —
    prices come from the picks of completed auction drafts.
    """

    def __init__(
        self,
        base_url: str = BASE_URL,
        rate_limit_per_min: int = DEFAULT_RATE_LIMIT_PER_MIN,
        session: Optional[requests.Session] = None,
        max_retries: int = 4,
        timeout: float = 20.0,
        limiter: Optional[RateLimiter] = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "sleeper-aav/0.1 (+non-commercial)")
        self.max_retries = max_retries
        self.timeout = timeout
        self.limiter = limiter or RateLimiter(rate_limit_per_min, sleeper=sleeper)
        self._sleep = sleeper
        self.request_count = 0

    # -- transport ---------------------------------------------------------

    def get(self, path: str) -> Any:
        """GET a path under the API base. Returns parsed JSON, or None on 404.

        Sleeper answers unknown users/leagues with 404 or a literal `null`;
        both mean "nothing here", which is a normal crawl outcome, not an error.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        attempt = 0
        while True:
            self.limiter.acquire()
            self.request_count += 1
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise SleeperError(f"GET {url} failed after {attempt} retries: {exc}") from exc
                self._backoff(attempt, f"{type(exc).__name__} on {url}")
                attempt += 1
                continue

            if resp.status_code == 404:
                return None
            if resp.status_code in RETRY_STATUSES:
                if attempt >= self.max_retries:
                    raise SleeperError(f"GET {url} still {resp.status_code} after {attempt} retries")
                self._backoff(attempt, f"HTTP {resp.status_code} on {url}", resp)
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise SleeperError(f"GET {url} returned HTTP {resp.status_code}")

            try:
                return resp.json()
            except ValueError as exc:
                raise SleeperError(f"GET {url} returned non-JSON body") from exc

    def _backoff(self, attempt: int, reason: str, resp: Optional[requests.Response] = None) -> None:
        delay = 2.0 ** attempt
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
        log.warning("retrying in %.1fs: %s", delay, reason)
        self._sleep(delay)

    # -- endpoints ---------------------------------------------------------

    def get_state(self, sport: str = "nfl") -> Optional[dict]:
        """Current season and week. Takes no parameters, so it doubles as a
        reachability check — a `null` from anything else means "not found",
        but this one always has an answer."""
        return self.get(f"state/{sport}")

    def get_user(self, username_or_id: str) -> Optional[dict]:
        return self.get(f"user/{username_or_id}")

    def get_user_leagues(self, user_id: str, season: str, sport: str = "nfl") -> list[dict]:
        return self.get(f"user/{user_id}/leagues/{sport}/{season}") or []

    def get_user_drafts(self, user_id: str, season: str, sport: str = "nfl") -> list[dict]:
        """The only discovery endpoint there is — hence the social-graph crawl."""
        return self.get(f"user/{user_id}/drafts/{sport}/{season}") or []

    def get_league(self, league_id: str) -> Optional[dict]:
        return self.get(f"league/{league_id}")

    def get_league_users(self, league_id: str) -> list[dict]:
        return self.get(f"league/{league_id}/users") or []

    def get_league_drafts(self, league_id: str) -> list[dict]:
        return self.get(f"league/{league_id}/drafts") or []

    def get_draft(self, draft_id: str) -> Optional[dict]:
        return self.get(f"draft/{draft_id}")

    def get_draft_picks(self, draft_id: str) -> list[dict]:
        return self.get(f"draft/{draft_id}/picks") or []

    # -- players dump ------------------------------------------------------

    def get_players(self, cache_path: str, max_age: int = PLAYER_CACHE_MAX_AGE_SECONDS) -> dict:
        """The ~5MB /players/nfl dump, cached to disk.

        Fetched at most once a day and never from inside a loop. Only used to
        put names on player IDs at display time.
        """
        path = Path(cache_path)
        if path.exists() and (time.time() - path.stat().st_mtime) < max_age:
            try:
                return json.loads(path.read_text())
            except ValueError:
                log.warning("player cache at %s is corrupt, refetching", path)

        players = self.get("players/nfl") or {}
        if players:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(players))
        return players
