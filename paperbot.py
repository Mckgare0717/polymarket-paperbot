#!/usr/bin/env python3
"""Polymarket paper-trading bot -- mean-reversion on short-term price spikes.

No real money, no wallet, no private keys. It pulls public Polymarket market
data, simulates fills against the last quoted book, and tracks a paper bankroll
in state.json / trades.csv.

Universe: active binary (2-outcome) markets with an order book, priced away from
the extremes, above a min 24h volume + liquidity, minus anything matching
`exclude_keywords` (head-to-head sports by default), sorted by 24h volume.

Strategies (run in `strategies` priority order; first to fire on a market wins,
and every trade is tagged so `report` shows per-strategy P&L):
  * mean_reversion -- fade a sharp spike (move >= spike_threshold over
    lookback_min minutes) that is NOT part of a longer trend.
  * momentum -- ride a slower, consistent trend over momentum_lookback_min.
  * favorite -- buy the heavy favorite (favorite_min/max_price) within
    favorite_max_days of resolution; the longshot-bias edge.

Exit (all strategies): take-profit / stop-loss on the held side's own price, a
hard time stop, or the market nearing resolution. Then a per-market cooldown.

AI filter: if GROQ_API_KEY (or AI_API_KEY) is set, every candidate trade is
sanity-checked by an LLM over an OpenAI-compatible endpoint -- it can veto a
trade whose price move looks news-driven rather than noise. Fails open (a dead
API never blocks trading). Toggle with cfg["ai_filter"].

Usage:
    python paperbot.py selftest   # internal checks, run this first
    python paperbot.py once       # one scan/trade pass (schedule via cron/agent)
    python paperbot.py run        # loop forever (persistent process)
    python paperbot.py report     # print paper P&L + open positions
    python paperbot.py trades     # dump all recorded trades as CSV to stdout
    python paperbot.py ai "<q>"   # test the LLM connection on one question
    python paperbot.py reset      # archive state + trades, start fresh at $1000

Set STATE_DIR to put state.json / trades.csv / bot.log somewhere else -- point
it at a mounted persistent disk in the cloud so a restart keeps the bankroll
and open positions. Set PORT and the `run` loop also serves a health endpoint
(Render sets PORT automatically).
"""

import csv
import json
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def _pick_state_dir():
    want = os.environ.get("STATE_DIR") or HERE
    try:
        os.makedirs(want, exist_ok=True)
        probe = os.path.join(want, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return want
    except OSError:
        # e.g. STATE_DIR=/data set but no disk mounted yet -- run anyway,
        # ephemerally, so the service is up while the disk gets added.
        print(f"WARN: STATE_DIR {want!r} not writable; using {HERE} "
              f"(state will NOT survive a restart)")
        return HERE


STATE_DIR = _pick_state_dir()
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
TRADES_PATH = os.path.join(STATE_DIR, "trades.csv")
LOG_PATH = os.path.join(STATE_DIR, "bot.log")

GAMMA = "https://gamma-api.polymarket.com"

DEFAULTS = {
    "start_bankroll": 1000.0,
    "poll_seconds": 120,
    "universe_size": 40,
    "min_volume_24h": 20000.0,
    "min_liquidity": 5000.0,
    "lookback_min": 60,
    "min_samples": 5,
    "spike_threshold": 0.08,
    "max_spread": 0.04,
    "position_usd": 50.0,
    "max_alloc_frac": 0.20,
    "max_open_positions": 5,
    "take_profit_pct": 0.15,
    "stop_loss_pct": 0.20,
    "max_hold_hours": 24,
    "entry_close_buffer_hours": 12,
    "exit_close_buffer_hours": 2,
    "cooldown_hours": 6,
    "price_min": 0.05,
    "price_max": 0.97,

    # which strategies to run, in priority order (first one to fire on a
    # market wins). See the strategy functions for what each does.
    "strategies": ["mean_reversion", "momentum", "favorite"],

    # mean_reversion: fade a sharp move, betting it partly retraces
    #   uses lookback_min / spike_threshold above
    # momentum: ride a slower sustained trend
    "momentum_lookback_min": 180,
    "momentum_move_threshold": 0.10,
    "momentum_consistency": 0.66,
    # favorite: buy the heavy favorite near resolution (longshot-bias edge)
    "favorite_min_price": 0.88,
    "favorite_max_price": 0.96,
    "favorite_max_days": 21,

    # skip markets whose question contains any of these (case-insensitive).
    # default list targets head-to-head sports/esports, where "fade the
    # spike" logic backfires (a spike there is usually real game news).
    "exclude_keywords": [
        " vs. ", " vs ", " win on ", " beat ", "us open", "atp:", "wta:",
        "ufc", "nba ", "nfl ", "mlb ", "nhl ", "premier league", "la liga",
        "serie a", "bundesliga", "champions league", "valorant", "lol:",
        "cs2", "counter-strike", " bo3", " bo5", "vct",
    ],

    # LLM sanity-check on each candidate trade (only active when an API key is
    # in the environment). OpenAI-compatible endpoint -- swap provider freely.
    "ai_filter": True,
    "ai_base_url": "https://api.groq.com/openai/v1",
    "ai_model": "qwen/qwen3.8-27b",
    "ai_timeout": 20,
    # news lookup (Tavily) feeding the AI filter; only active if TAVILY_API_KEY set
    "news_days": 3,
    "news_max_results": 4,
}


# --------------------------------------------------------------------------- io

def now_ts():
    return time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def log(msg):
    line = f"{iso(now_ts())}  {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def get_json(url, timeout=20, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paperbot/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001 - network layer, retry on anything
            last = e
            time.sleep(1 + i)
    raise last


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


TRADE_HEADER = ["ts", "action", "strategy", "market_id", "question", "outcome",
                "price", "shares", "cost", "cash_after"]


def _fresh_state(cfg):
    return {
        "cash": cfg["start_bankroll"],
        "realized": 0.0,
        "realized_by_strategy": {},
        "positions": {},
        "history": {},
        "cooldowns": {},
        "created": iso(now_ts()),
    }


def load_state(cfg):
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return _fresh_state(cfg)


def save_state(state, broker):
    state["cash"] = broker.cash
    state["realized"] = broker.realized
    state["realized_by_strategy"] = broker.rbs
    state["positions"] = broker.positions
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def record_trade(action, pos, price, cash_after):
    # if an older-format trades.csv is sitting there, retire it rather than
    # append mismatched columns
    if os.path.exists(TRADES_PATH):
        with open(TRADES_PATH, encoding="utf-8") as f:
            header_ok = f.readline().strip() == ",".join(TRADE_HEADER)
        if not header_ok:
            os.replace(TRADES_PATH, TRADES_PATH[:-4] + ".legacy.csv")
    newfile = not os.path.exists(TRADES_PATH)
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if newfile:
            w.writerow(TRADE_HEADER)
        w.writerow([iso(now_ts()), action, pos.get("strategy", "?"),
                    pos["market_id"], pos["question"], pos["outcome"],
                    f"{price:.4f}", f"{pos['shares']:.4f}",
                    f"{pos['cost']:.2f}", f"{cash_after:.2f}"])


# ---------------------------------------------------------------- market access

def as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return []
    return []


def parse_end(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _row_to_market(m, cfg, apply_filters):
    if not m.get("enableOrderBook") or m.get("closed") or not m.get("active"):
        return None
    outcomes = as_list(m.get("outcomes"))
    tokens = as_list(m.get("clobTokenIds"))
    prices = as_list(m.get("outcomePrices"))
    if not (len(outcomes) == len(tokens) == len(prices) == 2):
        return None
    try:
        p0 = float(prices[0])
        best_bid0 = float(m.get("bestBid") or 0) or None
        best_ask0 = float(m.get("bestAsk") or 0) or None
        spread = float(m.get("spread") if m.get("spread") is not None else 1)
        vol24 = float(m.get("volume24hr") or 0)
        liq = float(m.get("liquidity") or m.get("liquidityNum") or 0)
    except (TypeError, ValueError):
        return None
    if best_bid0 is None or best_ask0 is None:
        return None
    question = m.get("question") or m.get("slug") or "?"
    if apply_filters:
        if vol24 < cfg["min_volume_24h"] or liq < cfg["min_liquidity"]:
            return None
        if not cfg["price_min"] <= p0 <= cfg["price_max"]:
            return None
        ql = f" {question.lower()} "
        if any(kw in ql for kw in cfg["exclude_keywords"]):
            return None
    return {
        "id": str(m.get("id")),
        "question": question,
        "outcomes": [str(o) for o in outcomes],
        "tokens": [str(t) for t in tokens],
        "end": parse_end(m.get("endDate")),
        "ref_price": p0,
        "best_bid0": best_bid0,
        "best_ask0": best_ask0,
        "spread": spread,
        "vol24": vol24,
    }


def fetch_markets(cfg):
    rows = get_json(f"{GAMMA}/markets?closed=false&active=true"
                    f"&order=volume24hr&ascending=false&limit=100")
    out = [x for x in (_row_to_market(m, cfg, True) for m in rows) if x]
    out.sort(key=lambda x: x["vol24"], reverse=True)
    return out


def fetch_one_market(cfg, market_id):
    try:
        rows = get_json(f"{GAMMA}/markets?id={market_id}")
    except Exception:  # noqa: BLE001
        return None
    for m in rows or []:
        mk = _row_to_market(m, cfg, False)
        if mk and mk["id"] == str(market_id):
            return mk
    return None


def side_fill_price(market, token_index, kind):
    """Best price to BUY (kind='ask') or SELL (kind='bid') a given outcome.

    Binary market: outcome-1 quotes are the mirror of outcome-0 quotes.
    """
    bid0, ask0 = market["best_bid0"], market["best_ask0"]
    if token_index == 0:
        return ask0 if kind == "ask" else bid0
    return (1.0 - bid0) if kind == "ask" else (1.0 - ask0)


# ---------------------------------------------------------------- pure strategy
#
# Each strategy is (market, history, cfg) -> (token_index, note) or None.
# history is a list of (ts, ref_price) ascending. token_index is the outcome
# to BUY. The executor tries them in cfg["strategies"] order; first hit wins.


def _window(history, minutes):
    if not history:
        return []
    cutoff = history[-1][0] - minutes * 60
    return [h for h in history if h[0] >= cutoff]


def strat_mean_reversion(market, history, cfg):
    """Fade a sharp *spike* -- bet it partly retraces."""
    if len(history) < cfg["min_samples"]:
        return None
    win = _window(history, cfg["lookback_min"])
    if len(win) < cfg["min_samples"]:
        return None
    move = win[-1][1] - win[0][1]
    if abs(move) < cfg["spike_threshold"]:
        return None
    # only a spike, not a longer trend -- if the move over the momentum window
    # is bigger, this is a trend; leave it for strat_momentum.
    long_win = _window(history, cfg["momentum_lookback_min"])
    if long_win:
        long_move = long_win[-1][1] - long_win[0][1]
        if abs(long_move) > abs(move) * 1.2:
            return None
    # spiked up -> buy the other outcome (1); spiked down -> buy ref (0)
    return (1 if move > 0 else 0), f"fade {move:+.3f}/{cfg['lookback_min']}m"


def strat_momentum(market, history, cfg):
    """Ride a slower, sustained, consistent trend."""
    if len(history) < cfg["min_samples"]:
        return None
    win = _window(history, cfg["momentum_lookback_min"])
    if len(win) < cfg["min_samples"]:
        return None
    move = win[-1][1] - win[0][1]
    if abs(move) < cfg["momentum_move_threshold"]:
        return None
    steps = [win[i + 1][1] - win[i][1] for i in range(len(win) - 1)]
    agree = sum(1 for s in steps if (s > 0) == (move > 0))
    if not steps or agree / len(steps) < cfg["momentum_consistency"]:
        return None
    # trending up -> buy ref (0) and ride; trending down -> buy other (1)
    return (0 if move > 0 else 1), f"ride {move:+.3f}/{cfg['momentum_lookback_min']}m"


def strat_favorite(market, history, cfg):
    """Buy the heavy favorite close to resolution (longshot-bias edge)."""
    end = market["end"]
    if end is None:
        return None
    days_left = (end - now_ts()) / 86400
    if not 1 <= days_left <= cfg["favorite_max_days"]:
        return None
    lo, hi = cfg["favorite_min_price"], cfg["favorite_max_price"]
    p = market["ref_price"]
    if lo <= p <= hi:
        return 0, f"fav {p:.2f} {days_left:.0f}d"
    if lo <= 1 - p <= hi:
        return 1, f"fav {1 - p:.2f} {days_left:.0f}d"
    return None


STRATEGIES = {
    "mean_reversion": strat_mean_reversion,
    "momentum": strat_momentum,
    "favorite": strat_favorite,
}


def eval_strategies(market, history, cfg):
    """Returns (strategy_name, token_index, note) for the first strategy that
    fires, in cfg order; or None."""
    for name in cfg["strategies"]:
        fn = STRATEGIES.get(name)
        if fn is None:
            continue
        hit = fn(market, history, cfg)
        if hit:
            return name, hit[0], hit[1]
    return None


# ------------------------------------------------------------------- ai filter

def _ai_key():
    return os.environ.get("GROQ_API_KEY") or os.environ.get("AI_API_KEY")


def ai_available(cfg):
    return bool(_ai_key()) and cfg.get("ai_filter", True)


def _ai_complete(cfg, prompt):
    body = json.dumps({
        "model": cfg["ai_model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        cfg["ai_base_url"].rstrip("/") + "/chat/completions",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {_ai_key()}",
                 "Content-Type": "application/json",
                 "User-Agent": "paperbot/1.0"})  # bare urllib UA gets CF-blocked
    with urllib.request.urlopen(req, timeout=cfg["ai_timeout"]) as r:
        d = json.loads(r.read().decode())
    return d["choices"][0]["message"]["content"]


def parse_ai_verdict(raw):
    """LLM JSON -> (allow: bool, reason: str). Anything unparseable allows."""
    try:
        v = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return True, "unparseable verdict"
    allow = str(v.get("decision", "take")).strip().lower() != "skip"
    return allow, str(v.get("reason", ""))[:140]


def news_headlines(query, cfg):
    """Recent news snippets for a topic via Tavily, or [] if unavailable."""
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return []
    try:
        body = json.dumps({
            "query": query,
            "topic": "news",
            "days": cfg["news_days"],
            "max_results": cfg["news_max_results"],
            "search_depth": "basic",
        }).encode()
        req = urllib.request.Request(
            "https://api.tavily.com/search", data=body, method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "User-Agent": "paperbot/1.0"})
        with urllib.request.urlopen(req, timeout=cfg["ai_timeout"]) as r:
            d = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001 - news is a bonus, never fatal
        return []
    out = []
    for x in d.get("results", []):
        snip = " ".join((x.get("content") or "").split())[:200]
        out.append(f"- {x.get('title', '?')} "
                   f"({x.get('published_date', '')}): {snip}")
    return out


def ai_check(market, strategy, token_index, note, cfg):
    """(allow, reason). Fails OPEN -- a dead API must never block trading."""
    side = market["outcomes"][token_index]
    heads = news_headlines(market["question"], cfg)
    news_block = ("\n\nRecent news (last few days):\n" + "\n".join(heads)
                  if heads else
                  "\n\n(No news feed available -- use your own knowledge.)")
    prompt = (
        "A technical trading rule found a statistical signal on a Polymarket "
        "prediction market and wants to place this trade. You are the veto "
        "check. DEFAULT TO ALLOWING. Only answer \"skip\" if you have a "
        "SPECIFIC concrete reason that makes THIS trade likely to lose. "
        "General caution or 'markets are hard' are NOT reasons to skip.\n\n"
        f"Market: {market['question']}\n"
        f"Price of '{market['outcomes'][0]}': {market['ref_price']:.2f}\n"
        f"Rule: {strategy} -- {note}\n"
        f"Trade: BUY '{side}'"
        f"{news_block}\n\n"
        "mean_reversion skips only if recent news plausibly explains the move "
        "(then it is information, not noise). momentum skips only if news "
        "points to an imminent reversal. favorite skips only if the favorite "
        "is in real jeopardy. Stale or irrelevant headlines are not a reason "
        "to skip.\n"
        'Reply ONLY as JSON: {"decision": "take" or "skip", "reason": "<15 words"}'
    )
    try:
        return parse_ai_verdict(_ai_complete(cfg, prompt))
    except Exception as e:  # noqa: BLE001 - never let the filter break the bot
        return True, f"ai unavailable ({type(e).__name__})"


def exit_signal(pos, side_price, market_end, cfg):
    entry = pos["entry_price"]
    held_h = (now_ts() - pos["entry_ts"]) / 3600
    if side_price >= entry * (1 + cfg["take_profit_pct"]):
        return "take_profit"
    if side_price <= entry * (1 - cfg["stop_loss_pct"]):
        return "stop_loss"
    if held_h >= cfg["max_hold_hours"]:
        return "max_hold"
    if market_end and now_ts() >= market_end - cfg["exit_close_buffer_hours"] * 3600:
        return "market_closing"
    return None


# ----------------------------------------------------------------- paper broker

class PaperBroker:
    def __init__(self, state, cfg):
        self.cash = state["cash"]
        self.realized = state["realized"]
        self.rbs = state.get("realized_by_strategy", {})
        self.positions = state["positions"]
        self.cfg = cfg

    def open(self, market, token_index, price, strategy, note):
        usd = min(self.cfg["position_usd"], self.cash * self.cfg["max_alloc_frac"])
        if usd < 1 or price <= 0:
            return None
        # ponytail: assumes top-of-book absorbs the whole order at one price.
        #           add a depth walk if paper fills look unrealistically clean.
        shares = usd / price
        self.cash -= usd
        pos = {
            "market_id": market["id"],
            "question": market["question"],
            "strategy": strategy,
            "note": note,
            "token_index": token_index,
            "token_id": market["tokens"][token_index],
            "outcome": market["outcomes"][token_index],
            "entry_price": price,
            "shares": shares,
            "cost": usd,
            "entry_ts": now_ts(),
            "last_price": price,
        }
        self.positions[market["id"]] = pos
        record_trade("OPEN", pos, price, self.cash)
        log(f"OPEN  [{strategy}] {market['question'][:52]!r}  buy {pos['outcome']} "
            f"@ {price:.3f}  ${usd:.2f} ({shares:.1f} sh)  {note}")
        return pos

    def close(self, market_id, price, reason):
        pos = self.positions.pop(market_id)
        proceeds = pos["shares"] * price
        pnl = proceeds - pos["cost"]
        self.cash += proceeds
        self.realized += pnl
        strat = pos.get("strategy", "?")
        self.rbs[strat] = self.rbs.get(strat, 0.0) + pnl
        record_trade(f"CLOSE:{reason}", pos, price, self.cash)
        log(f"CLOSE [{strat}] {pos['question'][:52]!r}  sell {pos['outcome']} "
            f"@ {price:.3f}  pnl ${pnl:+.2f} ({reason})  bankroll ${self.cash:.2f}")
        return pnl


# ---------------------------------------------------------------------- runtime

def manage_position(broker, pos, market, cooldowns):
    market_id = pos["market_id"]
    if market is None:
        # Dropped out of the active list -- likely resolving. Close at last quote.
        # ponytail: true value is 0/1 after resolution; last bid is close enough
        #           for paper. Add a resolution lookup if this case gets common.
        broker.close(market_id, pos["last_price"], "market_gone")
        cooldowns[market_id] = now_ts()
        return
    side = side_fill_price(market, pos["token_index"], "bid")
    pos["last_price"] = side
    reason = exit_signal(pos, side, market["end"], broker.cfg)
    if reason:
        broker.close(market_id, side, reason)
        cooldowns[market_id] = now_ts()


def run_iteration(cfg, state):
    broker = PaperBroker(state, cfg)
    history = state["history"]
    cooldowns = state["cooldowns"]

    try:
        markets = fetch_markets(cfg)
    except Exception as e:  # noqa: BLE001
        log(f"market fetch failed: {e}")
        return
    by_id = {m["id"]: m for m in markets}
    log(f"scan {len(markets)} mkts | cash ${broker.cash:.2f} | "
        f"realized ${broker.realized:+.2f} | open {len(broker.positions)}")

    # 1. manage open positions (any market, not just the top slice)
    for market_id, pos in list(broker.positions.items()):
        market = by_id.get(market_id) or fetch_one_market(cfg, market_id)
        manage_position(broker, pos, market, cooldowns)

    # 2. look for entries in the top slice by 24h volume
    for m in markets[: cfg["universe_size"]]:
        hist = history.setdefault(m["id"], [])
        hist.append([now_ts(), m["ref_price"]])
        span_min = max(cfg["lookback_min"], cfg["momentum_lookback_min"])
        keep_after = now_ts() - span_min * 60 * 1.5
        history[m["id"]] = [h for h in hist if h[0] >= keep_after][-300:]
        hist = history[m["id"]]

        if m["id"] in broker.positions:
            continue
        if len(broker.positions) >= cfg["max_open_positions"]:
            continue
        cd = cooldowns.get(m["id"])
        if cd and now_ts() - cd < cfg["cooldown_hours"] * 3600:
            continue
        if m["end"] and now_ts() >= m["end"] - cfg["entry_close_buffer_hours"] * 3600:
            continue
        if m["spread"] > cfg["max_spread"]:
            continue

        hit = eval_strategies(m, hist, cfg)
        if hit is None:
            continue
        strategy, idx, note = hit
        fill = side_fill_price(m, idx, "ask")
        if not 0.02 <= fill <= 0.98:
            continue
        if ai_available(cfg):
            allow, why = ai_check(m, strategy, idx, note, cfg)
            log(f"  ai {'OK  ' if allow else 'SKIP'} [{strategy}] "
                f"{m['question'][:44]!r}: {why}")
            if not allow:
                cooldowns[m["id"]] = now_ts()  # don't re-ask every cycle
                continue
            note = f"{note} | ai:{why}"
        broker.open(m, idx, fill, strategy, note)

    # drop history for markets we no longer track and hold no position in
    tracked = {m["id"] for m in markets[: cfg["universe_size"]]}
    for k in list(history):
        if k not in tracked and k not in broker.positions:
            del history[k]
    for k in list(cooldowns):
        if now_ts() - cooldowns[k] > cfg["cooldown_hours"] * 3600 * 4:
            del cooldowns[k]

    save_state(state, broker)
    equity = broker.cash + sum(p["shares"] * p["last_price"]
                               for p in broker.positions.values())
    log(f"end iter | equity ${equity:.2f} | cash ${broker.cash:.2f} | "
        f"realized ${broker.realized:+.2f} | open {len(broker.positions)}")


def start_health_server(cfg):
    """Serve the port Render needs, plus /report and /trades for checking in
    without the Render shell. No-op locally (no PORT set)."""
    port = os.environ.get("PORT")
    if not port:
        return
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path.startswith("/report"):
                    body = report_text(cfg, load_state(cfg)).encode()
                elif self.path.startswith("/trades"):
                    body = (open(TRADES_PATH, "rb").read()
                            if os.path.exists(TRADES_PATH)
                            else b"no trades yet\n")
                else:
                    body = b"paperbot ok"
            except Exception as e:  # noqa: BLE001
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"error: {e}".encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    srv = http.server.HTTPServer(("0.0.0.0", int(port)), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"health server on :{port}  (/report /trades)")


def run_dump_trades(cfg):
    if os.path.exists(TRADES_PATH):
        with open(TRADES_PATH, encoding="utf-8") as f:
            sys.stdout.write(f.read())
    else:
        sys.stdout.write(",".join(TRADE_HEADER) + "\n")


def report_text(cfg, state, live_prices=True):
    broker = PaperBroker(state, cfg)
    by_id = {}
    if live_prices:
        try:
            by_id = {m["id"]: m for m in fetch_markets(cfg)}
        except Exception:  # noqa: BLE001
            pass
    L = [
        f"created:        {state.get('created')}",
        f"as of:          {iso(now_ts())}",
        f"start bankroll: ${cfg['start_bankroll']:.2f}",
        f"cash:           ${broker.cash:.2f}",
        f"realized P&L:   ${broker.realized:+.2f}",
    ]
    if broker.rbs:
        L.append("  by strategy:")
        for k, v in sorted(broker.rbs.items(), key=lambda kv: -kv[1]):
            L.append(f"    {k:16} ${v:+.2f}")
    L.append(f"open positions: {len(broker.positions)}")
    equity = broker.cash
    for p in broker.positions.values():
        m = by_id.get(p["market_id"])
        cur = side_fill_price(m, p["token_index"], "bid") if m else p["last_price"]
        equity += p["shares"] * cur
        unreal = p["shares"] * cur - p["cost"]
        held_h = (now_ts() - p["entry_ts"]) / 3600
        L.append(f"  [{p.get('strategy', '?')}] {p['question'][:44]!r}  "
                 f"{p['outcome']}  entry {p['entry_price']:.3f} now {cur:.3f}  "
                 f"unreal ${unreal:+.2f}  {held_h:.1f}h")
    L.append(f"equity:         ${equity:.2f}  "
             f"({(equity / cfg['start_bankroll'] - 1) * 100:+.1f}%)")
    return "\n".join(L)


def run_report(cfg, state):
    print(report_text(cfg, state))


def run_selftest():
    # keep selftest off the real state/trade/log files
    global TRADES_PATH, LOG_PATH
    import tempfile
    tmp = tempfile.mkdtemp(prefix="paperbot-selftest-")
    TRADES_PATH = os.path.join(tmp, "trades.csv")
    LOG_PATH = os.path.join(tmp, "bot.log")

    cfg = dict(DEFAULTS)
    state = {"cash": 1000.0, "realized": 0.0, "realized_by_strategy": {},
             "positions": {}, "history": {}, "cooldowns": {}}
    b = PaperBroker(state, cfg)
    mkt = {"id": "m1", "question": "Test?", "outcomes": ["Yes", "No"],
           "tokens": ["tY", "tN"], "end": None,
           "best_bid0": 0.48, "best_ask0": 0.52, "ref_price": 0.50}

    b.open(mkt, 1, 0.50, "mean_reversion", "t")   # buy "No" at 0.50, $50
    assert abs(b.cash - 950.0) < 1e-6, b.cash
    assert abs(b.positions["m1"]["shares"] - 100.0) < 1e-6
    assert b.positions["m1"]["token_index"] == 1
    b.close("m1", 0.60, "take_profit")            # 100 * (0.60 - 0.50) = +10
    assert abs(b.realized - 10.0) < 1e-6, b.realized
    assert abs(b.cash - 1010.0) < 1e-6, b.cash
    assert abs(b.rbs["mean_reversion"] - 10.0) < 1e-6, b.rbs

    # mirror pricing for outcome 1
    assert abs(side_fill_price(mkt, 1, "ask") - (1 - 0.48)) < 1e-9
    assert abs(side_fill_price(mkt, 0, "ask") - 0.52) < 1e-9

    t = 1_000_000.0
    mm = {"end": None, "ref_price": 0.5}
    # mean_reversion: a sharp move fires, flat doesn't, too-few-samples doesn't
    up = [(t + i * 300, 0.40 + i * 0.02) for i in range(7)]        # +0.12 / 30m
    assert strat_mean_reversion(mm, up, cfg)[0] == 1
    down = [(t + i * 300, 0.60 - i * 0.02) for i in range(7)]
    assert strat_mean_reversion(mm, down, cfg)[0] == 0
    flat = [(t + i * 300, 0.50 + (0.001 if i % 2 else -0.001)) for i in range(7)]
    assert strat_mean_reversion(mm, flat, cfg) is None
    assert strat_mean_reversion(mm, up[:2], cfg) is None

    # momentum: a long consistent grind fires; a choppy series does not
    grind = [(t + i * 600, 0.30 + i * 0.02) for i in range(18)]    # +0.34 / 170m
    assert strat_momentum(mm, grind, cfg)[0] == 0
    choppy = [(t + i * 600, 0.50 + (0.05 if i % 2 else -0.05)) for i in range(18)]
    assert strat_momentum(mm, choppy, cfg) is None

    # favorite: strong favorite near resolution fires; not otherwise
    soon = now_ts() + 10 * 86400
    assert strat_favorite({"end": soon, "ref_price": 0.93}, [], cfg)[0] == 0
    assert strat_favorite({"end": soon, "ref_price": 0.07}, [], cfg)[0] == 1
    assert strat_favorite({"end": soon, "ref_price": 0.60}, [], cfg) is None
    far = now_ts() + 90 * 86400
    assert strat_favorite({"end": far, "ref_price": 0.93}, [], cfg) is None

    # eval_strategies respects priority order and the spike-vs-trend split
    name, idx, _ = eval_strategies(mm, up, cfg)
    assert name == "mean_reversion" and idx == 1
    name, idx, _ = eval_strategies(mm, grind, cfg)
    assert name == "momentum" and idx == 0, (name, idx)

    # ai verdict parsing; fails open on junk
    assert parse_ai_verdict('{"decision":"take","reason":"noise"}') == (True, "noise")
    assert parse_ai_verdict('{"decision":"SKIP","reason":"ceasefire talks broke down"}')[0] is False
    assert parse_ai_verdict("not json at all")[0] is True
    assert parse_ai_verdict('{"reason":"x"}')[0] is True  # missing decision -> allow
    os.environ.pop("GROQ_API_KEY", None)
    os.environ.pop("AI_API_KEY", None)
    assert ai_available(cfg) is False
    os.environ.pop("TAVILY_API_KEY", None)
    assert news_headlines("anything", cfg) == []

    p = {"entry_price": 0.50, "entry_ts": now_ts()}
    assert exit_signal(p, 0.58, None, cfg) == "take_profit"
    assert exit_signal(p, 0.39, None, cfg) == "stop_loss"
    assert exit_signal(p, 0.51, None, cfg) is None
    old = {"entry_price": 0.50, "entry_ts": now_ts() - 25 * 3600}
    assert exit_signal(old, 0.51, None, cfg) == "max_hold"
    assert exit_signal(p, 0.51, now_ts() + 3600, cfg) == "market_closing"

    print("selftest OK")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "selftest":
        run_selftest()
        return
    cfg = load_config()
    if mode == "reset":
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for p in (STATE_PATH, TRADES_PATH):
            if os.path.exists(p):
                os.replace(p, f"{p}.{stamp}.bak")
                print(f"archived {os.path.basename(p)} -> {os.path.basename(p)}.{stamp}.bak")
        json.dump(_fresh_state(cfg), open(STATE_PATH, "w"), indent=2)
        print(f"fresh state: ${cfg['start_bankroll']:.2f}")
        return
    if mode == "trades":
        run_dump_trades(cfg)
        return
    if mode == "ai":
        q = sys.argv[2] if len(sys.argv) > 2 else "Will the US enter a recession in 2026?"
        if not _ai_key():
            print("no GROQ_API_KEY / AI_API_KEY in environment")
            return
        fake = {"question": q, "outcomes": ["Yes", "No"], "ref_price": 0.42}
        print(f"model: {cfg['ai_model']}  @ {cfg['ai_base_url']}")
        heads = news_headlines(q, cfg)
        print(f"news: {len(heads)} headline(s)"
              + (" (no TAVILY_API_KEY)" if not os.environ.get('TAVILY_API_KEY')
                 else ""))
        for h in heads:
            print(f"  {h}")
        print(ai_check(fake, "mean_reversion", 0, "fade -0.10/60m", cfg))
        return
    if mode in ("report", "once"):
        state = load_state(cfg)
        (run_report if mode == "report" else run_iteration)(cfg, state)
        return
    if mode != "run":
        print(__doc__)
        return

    stop = {"v": False}

    def handler(*_):
        stop["v"] = True
        log("stop requested; finishing then exiting")

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    start_health_server(cfg)

    def nap(seconds):
        for _ in range(int(seconds)):
            if stop["v"]:
                return
            time.sleep(1)

    state = None
    while state is None and not stop["v"]:
        try:
            state = load_state(cfg)
        except Exception as e:  # noqa: BLE001 - KV may still be provisioning
            log(f"state load failed ({e}); retry in 15s")
            nap(15)

    while not stop["v"]:
        try:
            run_iteration(cfg, state)
        except Exception as e:  # noqa: BLE001 - loop must survive
            log(f"iteration error: {e}")
        nap(cfg["poll_seconds"])
    log("exited")


if __name__ == "__main__":
    main()
