"""Synthetic Sleeper responses and a fake client.

Shaped from the documented API plus the assumed `metadata.amount` on auction
picks. Until verify.py has been run against a live auction, treat the pick
shape here as the assumption under test, not as ground truth.
"""

from __future__ import annotations

from typing import Optional


def league(league_id="L1", league_type=0, teams=12, rec=1.0, superflex=False) -> dict:
    positions = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"]
    if superflex:
        positions.append("SUPER_FLEX")
    return {
        "league_id": league_id,
        "total_rosters": teams,
        "settings": {"type": league_type},
        "scoring_settings": {"rec": rec},
        "roster_positions": positions,
        # Present in real responses, never read, never stored:
        "name": "Some League Name",
    }


def draft(draft_id="D1", league_id="L1", budget=200, teams=12,
          draft_type="auction", status="complete", season="2025") -> dict:
    return {
        "draft_id": draft_id,
        "league_id": league_id,
        "type": draft_type,
        "status": status,
        "season": season,
        "sport": "nfl",
        "settings": {"budget": budget, "teams": teams, "rounds": 15},
        "metadata": {"scoring_type": "ppr", "name": "Some Draft Name"},
    }


def pick(pick_no: int, player_id: str, amount: Optional[int], draft_id="D1",
         keeper=False, amount_key="metadata.amount", as_string=True) -> dict:
    row = {
        "draft_id": draft_id,
        "pick_no": pick_no,
        "player_id": player_id,
        "round": 1,
        "roster_id": (pick_no % 12) + 1,
        "picked_by": f"user{pick_no}",
        "is_keeper": True if keeper else None,
        "metadata": {"first_name": "First", "last_name": "Last", "position": "WR"},
    }
    if amount is not None:
        value = str(amount) if as_string else amount
        if amount_key == "metadata.amount":
            row["metadata"]["amount"] = value
        elif amount_key == "metadata.bid_amount":
            row["metadata"]["bid_amount"] = value
        elif amount_key == "amount":
            row["amount"] = value
    return row


def auction_picks(prices: dict[str, int], draft_id="D1", keepers: Optional[dict[str, int]] = None,
                  **kwargs) -> list[dict]:
    """prices: player_id -> winning bid. keepers: player_id -> contract price."""
    picks = []
    n = 1
    for player_id, amount in prices.items():
        picks.append(pick(n, player_id, amount, draft_id=draft_id, **kwargs))
        n += 1
    for player_id, amount in (keepers or {}).items():
        picks.append(pick(n, player_id, amount, draft_id=draft_id, keeper=True, **kwargs))
        n += 1
    return picks


class FakeClient:
    """Stands in for SleeperClient. Counts calls so tests can assert on them."""

    def __init__(self, drafts=None, picks=None, leagues=None, league_users=None,
                 user_drafts=None, users=None):
        self.drafts = drafts or {}
        self.picks = picks or {}
        self.leagues = leagues or {}
        self.league_users = league_users or {}
        self.user_drafts = user_drafts or {}
        self.users = users or {}
        self.request_count = 0
        self.calls: list[str] = []

    def _log(self, what: str):
        self.request_count += 1
        self.calls.append(what)

    def get_draft(self, draft_id):
        self._log(f"draft/{draft_id}")
        return self.drafts.get(draft_id)

    def get_draft_picks(self, draft_id):
        self._log(f"draft/{draft_id}/picks")
        return self.picks.get(draft_id, [])

    def get_league(self, league_id):
        self._log(f"league/{league_id}")
        return self.leagues.get(league_id)

    def get_league_users(self, league_id):
        self._log(f"league/{league_id}/users")
        return self.league_users.get(league_id, [])

    def get_user_drafts(self, user_id, season, sport="nfl"):
        self._log(f"user/{user_id}/drafts")
        return self.user_drafts.get(user_id, [])

    def get_user(self, username_or_id):
        self._log(f"user/{username_or_id}")
        return self.users.get(username_or_id)
