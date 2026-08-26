# sleeper-aav

Builds fantasy football auction values (AAV) from **real completed Sleeper
auction drafts** rather than from a third-party projection feed. Real clearing
prices from real leagues, aggregated into per-player values.

## Why this approach

Sleeper's public API exposes no ADP, AAV, or projection endpoint — the whole
documented surface is users, leagues, drafts, players, and trending adds/drops.
But completed auction drafts contain the winning bid on every pick. That's
better than a projection feed: it's actual money, actual scarcity, and it
reflects how Sleeper's own user base values players rather than ESPN's.

## Hard constraints

- **The API is read-only.** No lineup setting, no waiver claims. Don't propose
  features that require writes.
- **No draft search exists.** Discovery only works per-user
  (`/user/<id>/drafts/nfl/<season>`), so we crawl outward through the league
  social graph. This is the central design constraint.
- **1000 calls/min is Sleeper's stated ceiling** before an IP block. We run at
  300 by default. Do not raise this to make a crawl finish faster.
- **Free for non-commercial use only.** Commercial use needs a license from
  Sleeper. Flag it if the project starts heading that direction.

## Decisions already made — don't relitigate without a reason

**Normalize every price to percent-of-budget on ingest.** A $54 back in a $200
league and a $27 back in a $100 league are the same price. Raw dollars are never
comparable across leagues. Render back to a reference budget only at display time.

**Exclude keeper picks entirely.** A keeper price is a contract from a prior
season, not a market clearing price. This is the single biggest source of
garbage in a naive AAV build. Drafts that are >40% keepers get discarded whole.

**Exclude mock drafts** by dropping drafts with no `league_id`. Play money
behaves differently from real money.

**Segment hard: redraft/keeper/dynasty, superflex, PPR, team count.** Prices
across these formats are not comparable, and averaging them produces numbers
that describe no real league. Never collapse segments to increase sample size.

**Report median, p25, and p75 alongside the mean.** The spread is the useful
part — a tight band means consensus, a wide one is where the edge is.

**Track `pct_above_min`** (share of drafts where the player went above $1).
Late auctions collapse into everyone-costs-a-dollar, which drags fringe players
toward an identical average and hides real separation between them.

## Unverified — check before trusting at scale

- **`metadata.amount` on auction picks is undocumented.** Sleeper's official
  docs only show a snake draft example, so the field never appears there. It's
  almost certainly correct but has never been confirmed against a live response.
  Verify the field name and type against one real auction draft before running a
  large crawl. `parse_amount()` in `ingest.py` is the only place that needs
  changing if it's shaped differently.
- **The 40% keeper threshold is a guess.** Look at the real distribution once
  there's data and move it.
- **Mock detection via missing `league_id` is a heuristic**, not a documented
  guarantee. Sanity-check it.

## Privacy rules — non-negotiable

We are reading strangers' league data through a public API. Keep the dataset to
what's needed for prices.

- **Never store** usernames, display names, or team names.
- User IDs appear only as salted SHA-256 hashes, used purely for crawl dedupe.
- `.env` and `*.sqlite3` stay gitignored. Never commit them.
- **Never hardcode a league ID or username** anywhere in the source. Seeds come
  from `SLEEPER_SEED_LEAGUE_ID` / `SLEEPER_SEED_USERNAME` at runtime. A league ID
  is the key to that league's full roster, transaction, and draft history with no
  auth required — treat it as config, not a constant.

## Conventions

- Stdlib + `requests` only unless there's a real reason to add a dependency.
  SQLite is sufficient; don't reach for Postgres at this scale.
- All network access goes through `SleeperClient` so rate limiting and retries
  stay in one place. Don't call `requests` directly.
- The `/players/nfl` dump is ~5MB and should be fetched at most once per day.
  Cache it to disk; never call it inside a loop.
- Ingestion is idempotent — reruns should not duplicate rows.

## Eventual goal

The aggregation layer becomes the data source for an MCP server (forked from
`sourknives/sleeper-mcp-server`) exposing tools like `get_auction_values`. MCP
tools read from the aggregation, never from raw picks.
