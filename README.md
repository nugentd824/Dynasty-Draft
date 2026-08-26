# sleeper-aav

Fantasy football auction values built from **real completed Sleeper auction
drafts** — actual winning bids from actual leagues, aggregated into per-player
values. Not a projection feed.

See [CLAUDE.md](CLAUDE.md) for the design rationale, the constraints that shape
it, and the decisions that shouldn't be relitigated.

## Status

The pipeline is complete and tested end to end. The bid field was confirmed on
2026-08-26 against a completed 12-team half-PPR auction — `metadata.amount`
carries the winning bid as a string, exactly as assumed, and `parse_amount()`
needed no change.

Draft objects, league objects and `/state/nfl` have all been checked against
live responses too, and the crawler has run against the live API end to end.

Discovery yield is the open problem. One 2026 crawl exhausted its frontier at
568 users and 497 leagues for 3 usable drafts, at ~355 requests each. Reaching a
sample worth aggregating needs either more seeds, greater depth, or a season
whose drafts have all happened.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # SLEEPER_USER_ID_SALT
```

Then fill in `.env`: a seed (`SLEEPER_SEED_LEAGUE_ID` or
`SLEEPER_SEED_USERNAME`), the season, and the salt. `.env` is gitignored and
must stay that way — a league ID is the key to that league's full roster,
transaction and draft history with no auth required.

## Walkthrough

```bash
python3 demo.py                      # whole pipeline on synthetic data, no network
python3 crawl.py --max-drafts 5 -v   # discover auction drafts
python3 verify.py                    # <- confirm the bid field before going bigger
python3 ingest.py --show-picks       # normalize and store
python3 aggregate.py --segments      # what sample exists, per segment
python3 aggregate.py --format redraft --ppr ppr --teams 12 --limit 40
```

Verifying without network access — dump the response on a machine that can
reach the API, then:

```bash
python3 verify.py --picks-file picks.json --draft-file draft.json
```

## How it fits together

| file | role |
| --- | --- |
| `config.py` | runtime config from `.env`; salted hashing of user IDs |
| `client.py` | the only module that touches the network — rate limit, retries, player cache |
| `crawl.py` | social-graph discovery of completed auction drafts |
| `verify.py` | prints a raw pick and reports which key carried the bid |
| `ingest.py` | screening, keeper/mock exclusion, percent-of-budget normalization, segmentation |
| `aggregate.py` | per-player mean/median/p25/p75/`pct_above_min`, per segment |
| `db.py` | SQLite schema and idempotent writes |
| `demo.py` | the whole pipeline on fixtures, offline |

## What gets thrown away

Discovery and ingest are deliberately lossy — the exclusions are the product:

- **snake and incomplete drafts** — no clearing prices in them
- **mock drafts** (no `league_id`) — play money behaves differently
- **keeper picks** — a prior-season contract, not a market price
- **whole drafts above 40% keepers** — what's left isn't a real auction either
- **drafts with no budget** — nothing to normalize against

Every exclusion is recorded with its reason in the `drafts` table, so a crawl
that quietly discards everything is visible rather than silent.

## Privacy

No usernames, display names, or team names are stored, anywhere. User IDs exist
only as salted SHA-256 hashes, used purely for crawl dedupe. `tests/test_privacy.py`
enforces this against the database and the source tree.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

69 tests, no network, no fixtures on disk.

## Licence note

Sleeper's API is free for non-commercial use. Commercial use needs a licence
from Sleeper.
