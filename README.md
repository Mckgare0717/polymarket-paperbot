# Polymarket paper-trading bot

**Multiple strategies, paper money, prove an edge before risking anything real.**

No wallet, no private keys, no real funds. It reads public Polymarket data,
simulates trades against the quoted book, and tracks a fake $1,000 bankroll.
If this doesn't make money on paper over a few weeks, it won't make money real.

## Strategies

Run in priority order — the first that fires on a market takes it. Every trade
is tagged, so `report` shows P&L **per strategy** (that's the whole point: find
out which one works).

| Strategy | Bets that… | Fires when |
|---|---|---|
| `mean_reversion` | a sharp spike overshot | price moved ≥ `spike_threshold` in `lookback_min`, and it's a spike not a longer trend |
| `momentum` | a steady trend continues | price moved ≥ `momentum_move_threshold` over `momentum_lookback_min`, consistently one direction |
| `favorite` | heavy favorites are underpriced (longshot bias) | favorite side is `favorite_min_price`–`favorite_max_price` and resolves within `favorite_max_days` |

**Universe:** top markets by 24h volume, binary, priced 0.05–0.97, minus
anything matching `exclude_keywords` (head-to-head sports by default — fading a
spike there means betting against a real goal/injury).

**Exit (all strategies):** +15% take-profit, −20% stop-loss, 24h time stop, or
market near resolution. Then a 6h per-market cooldown.

**Sizing:** max 5 open positions, $50 each, ≤ 20% of bankroll per position.

## AI filter (optional)

If `GROQ_API_KEY` (or `AI_API_KEY`) is set, every candidate trade is checked
before it's placed:

1. **`TAVILY_API_KEY`** (optional) → Tavily pulls the last few days of news
   headlines for that market's topic.
2. **LLM** (OpenAI-compatible endpoint, default Groq `qwen/qwen3.8-27b`) reads
   the headlines + the trade and returns take / skip. It **defaults to take** —
   it only vetoes with a specific concrete reason, which is logged and stored
   on the trade.

**Fails open** at every step: no Tavily key → LLM uses training knowledge; any
API error → the trade proceeds. A dead filter never blocks trading.

Real examples from testing (Sept 2026):
- *"Russia–Ukraine ceasefire by Oct 31"* → **skip** — "Trump envoys in Moscow
  for peace talks; move is news-driven, not noise"
- *"Powell remains Fed Chair through 2026"* → **skip** — "news says Powell is
  no longer Fed Chair, Yes ≈ worthless"
- *"Anthropic best AI model end of Sept"* → **take** — "news supports the buy"

Toggle with `ai_filter`; swap provider via `ai_base_url` / `ai_model`; tune the
lookup with `news_days` / `news_max_results`.

    python paperbot.py ai "Will the Fed cut rates in September?"   # test the chain

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

**Option D — Render (cloud, always on):** deployed as a web service running
`paperbot.py run`.

- Repo: https://github.com/Mckgare0717/polymarket-paperbot (public, no secrets)
- Service: `polymarket-paperbot` — https://dashboard.render.com/web/srv-dae7rsfqj5pc73ajf4og
- `starter` plan, ~$7/mo, always on. Serves a dummy health page on `$PORT`.
- **State needs a disk.** `STATE_DIR=/data` is set. In the Render dashboard →
  the service → **Disks** → **Add Disk**: name `state`, mount path `/data`,
  size 1 GB (~$0.25/mo). Without it the bot still runs, but a redeploy/restart
  resets to a fresh $1,000.
- Read results: `python paperbot.py report` won't work remotely — use the
  Render **Shell** tab (`python paperbot.py report`, `python paperbot.py trades`)
  or just read `bot.log` in the **Logs** tab.
- The free Key Value store `paperbot-kv` from an earlier attempt is unused —
  delete it in the dashboard.

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
