# Polymarket paper-trading bot

Phase 1 of the "trade like a pro" idea: **one strategy, paper money, prove an edge first.**

No wallet, no private keys, no real funds. It reads public Polymarket data,
simulates trades against the quoted book, and tracks a fake $1,000 bankroll.
If this doesn't make money on paper over a few weeks, it won't make money real.

## The strategy (what "you pick" picked)

**Short-term mean reversion on binary markets.**

- Watch the top ~40 active 2-outcome markets by 24h volume, priced between
  0.05 and 0.95.
- Sample outcome-0's price every poll. If it jumped more than **8 points**
  (`spike_threshold`) over the last **60 min** (`lookback_min`), bet on it
  reverting: spike up → buy the *other* side, spike down → buy *that* side.
- Enter at the current best ask, only if the spread is under 4 points.
- Exit on: **+15%** on the position (`take_profit_pct`), **-20%**
  (`stop_loss_pct`), **24h** elapsed (`max_hold_hours`), or the market getting
  close to resolution. Then a 6h cooldown on that market.
- Max 5 open positions, $50 each, never more than 20% of bankroll in one.

Why this one: it's simple to reason about, doesn't need a forecasting model,
and short-term overreactions in thin markets are a real, documented pattern.
It is **not** guaranteed to work — that's what the paper run tells us.

## Quick start

```
python paperbot.py selftest    # sanity checks — run first
python paperbot.py once         # a single scan/trade pass
python paperbot.py report       # current paper P&L + open positions
```

Needs Python 3.9+. No packages to install (standard library only).

Signals need warm-up: the bot must see a market for ~5+ polls before it can
detect a move. Expect zero trades for the first 10–15 minutes.

## Running it 24/7

**Option A — scheduled single passes (recommended, survives reboots):**
Windows Task Scheduler → new task → run every 3 minutes:

```
Program:   python
Arguments: C:\Users\mckin\polymarket-paperbot\paperbot.py once
Start in:  C:\Users\mckin\polymarket-paperbot
```

**Option B — persistent process:**

```
python paperbot.py run
```

Keeps looping every `poll_seconds`. Ctrl+C stops it cleanly. On a VPS, run it
under a supervisor (systemd, pm2, nssm) so it restarts on crash/reboot.

**Option C — a Claude Code scheduled agent** running `paperbot.py once` on a
cron. Ask me to set that up if you want it.

## Files it writes

| file | what |
|---|---|
| `state.json` | bankroll, open positions, price history, cooldowns — the bot's memory |
| `trades.csv` | every open/close, for analysis in Excel/pandas |
| `bot.log` | human-readable run log |

Delete all three to reset to a fresh $1,000.

## Tuning (`config.json`)

| knob | default | effect |
|---|---|---|
| `poll_seconds` | 120 | how often to scan (persistent mode) |
| `universe_size` | 40 | how many top markets to watch |
| `min_volume_24h` | 20000 | skip markets quieter than this |
| `min_liquidity` | 5000 | skip thin books |
| `lookback_min` | 60 | window for measuring a "spike" |
| `spike_threshold` | 0.08 | how big a move counts as a spike (prob. points) |
| `max_spread` | 0.04 | don't enter if the book is wider than this |
| `position_usd` | 50 | stake per trade |
| `max_open_positions` | 5 | concurrency cap |
| `take_profit_pct` / `stop_loss_pct` | 0.15 / 0.20 | exits on the position |
| `max_hold_hours` | 24 | time stop |
| `cooldown_hours` | 6 | pause a market after closing a position in it |
| `price_min` / `price_max` | 0.05 / 0.95 | ignore near-resolved longshots/favorites |

## Reading the results

After a week or two, in the folder:

```
python paperbot.py report
```

and open `trades.csv`. What matters:
- **Net P&L** vs the $1,000 start.
- **Win rate** and **average win vs average loss** — a 40% win rate is fine if
  wins are bigger than losses.
- **Number of trades** — under ~20 and the result is just noise.

Positive, consistent, enough trades → then we talk real money, risk limits,
and a kill switch. Flat or negative → we change the strategy, having risked
nothing.

## Known limitations (deliberate, for now)

- Fills assume the top of the book absorbs the whole $50 order at one price.
  Real fills are slightly worse. Results here are mildly optimistic.
- A market that resolves while held is closed at its last quote, not the true
  0/1 outcome.
- Price history starts empty each run of `once`; it builds up in `state.json`
  across runs. Don't delete `state.json` mid-experiment.
- No news / X sentiment yet. That's phase 2 and only gets added if phase 1
  shows an edge worth filtering.

## Phase 2 (not built)

A second signal — headline/sentiment scan — that the executor consults before
entering, so a technical spike that lines up with fresh news is skipped (it's
probably *not* noise) and one with no news behind it is taken. Built as another
function in this same file, not a separate bot.
