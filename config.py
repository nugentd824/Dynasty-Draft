"""Runtime configuration, read from the environment (and `.env` if present).

Nothing in here has a seed league ID or username baked in as a default. See
CLAUDE.md, "Privacy rules": those are config, not constants.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = Path(__file__).with_name(".env")


def load_env(path: Path = ENV_FILE) -> None:
    """Populate os.environ from a `.env` file without adding a dependency.

    Existing environment variables win, so an explicit export can override the
    file. Unparseable lines are skipped rather than raising — a broken comment
    should not stop a crawl.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    seed_league_id: str = ""
    seed_username: str = ""
    season: str = "2025"
    user_id_salt: str = ""
    rate_limit_per_min: int = 300
    db_path: str = "sleeper_aav.sqlite3"
    player_cache: str = "players_nfl.json"
    keeper_draft_threshold: float = 0.40
    reference_budget: int = 200

    @classmethod
    def from_env(cls, load_dotenv: bool = True) -> "Config":
        if load_dotenv:
            load_env()
        return cls(
            seed_league_id=os.environ.get("SLEEPER_SEED_LEAGUE_ID", "").strip(),
            seed_username=os.environ.get("SLEEPER_SEED_USERNAME", "").strip(),
            season=os.environ.get("SLEEPER_SEASON", "2025").strip(),
            user_id_salt=os.environ.get("SLEEPER_USER_ID_SALT", "").strip(),
            rate_limit_per_min=_int("SLEEPER_RATE_LIMIT_PER_MIN", 300),
            db_path=os.environ.get("SLEEPER_DB_PATH", "sleeper_aav.sqlite3").strip(),
            player_cache=os.environ.get("SLEEPER_PLAYER_CACHE", "players_nfl.json").strip(),
            keeper_draft_threshold=_float("KEEPER_DRAFT_THRESHOLD", 0.40),
            reference_budget=_int("REFERENCE_BUDGET", 200),
        )

    def require_salt(self) -> str:
        if not self.user_id_salt:
            raise ConfigError(
                "SLEEPER_USER_ID_SALT is unset. User IDs are only ever stored as "
                "salted hashes, so the crawl cannot dedupe without it. Generate one "
                'with: python3 -c "import secrets; print(secrets.token_hex(32))"'
            )
        return self.user_id_salt

    def require_seed(self) -> None:
        if not (self.seed_league_id or self.seed_username):
            raise ConfigError(
                "No crawl seed. Set SLEEPER_SEED_LEAGUE_ID or SLEEPER_SEED_USERNAME "
                "in .env (copy .env.example). There is no draft search endpoint, so "
                "discovery has to start from a league or user you name."
            )

    def hash_user_id(self, user_id: str) -> str:
        """Salted SHA-256 of a Sleeper user ID, used only for crawl dedupe.

        The raw ID never reaches the database. The salt has to stay stable
        across runs or previously-visited users look new again.
        """
        salted = f"{self.require_salt()}:{user_id}".encode("utf-8")
        return hashlib.sha256(salted).hexdigest()


class ConfigError(RuntimeError):
    pass
