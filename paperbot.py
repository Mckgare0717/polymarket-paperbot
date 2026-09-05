#!/usr/bin/env python3
"""Polymarket paper-trading bot -- mean-reversion on short-term price spikes.

No real money, no wallet, no private keys. It pulls public Polymarket market
data, simulates fills against the last quoted book, and tracks a paper bankroll
in state.json / trades.csv.

Strategy (phase 1, "market + chart scanner" only):
  * Universe: active binary (2-outcome) markets with an order book, priced away
    from the extremes, above a minimum 24h volume + liquidity, sorted by 24h
    volume.
  * Signal: track outcome-0's price. If it moved more than `spike_threshold`
    (probability points) over the last `lookback_min` minutes, FADE it --
    spike up -> buy outcome 1, spike down -> buy outcome 0.
  * Entry fill: best ask of the side we buy; spread must be tight.
  * Exit: take-profit / stop-loss on the held side's own price, a hard time
    stop, or the market nearing resolution. Then a per-market cooldown.

Phase 2 (news / X sentiment) is NOT here yet -- that piece has to prove an edge
on its own before it gates real trades.

Usage:
    python paperbot.py selftest   # internal checks, run this first
    python paperbot.py once       # one scan/trade pass (schedule via cron/agent)
    python paperbot.py run        # loop forever (persistent process)
    python paperbot.py report     # print paper P&L + open positions
    python paperbot.py trades     # dump all recorded trades as CSV to stdout

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
STATE_DIR = os.environ.get("STATE_DIR") or HERE
os.makedirs(STATE_DIR, exist_ok=True)
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
    "price_max": 0.95,
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


TRADE_HEADER = ["ts", "action", "market_id", "question", "outcome",
                "price", "shares", "cost", "cash_after"]


def _fresh_state(cfg):
    return {
        "cash": cfg["start_bankroll"],
        "realized": 0.0,
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
    state["positions"] = broker.positions
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def record_trade(action, pos, price, cash_after):
    newfile = not os.path.exists(TRADES_PATH)
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if newfile:
            w.writerow(TRADE_HEADER)
        w.writerow([iso(now_ts()), action, pos["market_id"], pos["question"],
                    pos["outcome"], f"{price:.4f}", f"{pos['shares']:.4f}",
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
    if apply_filters:
        if vol24 < cfg["min_volume_24h"] or liq < cfg["min_liquidity"]:
            return None
        if not cfg["price_min"] <= p0 <= cfg["price_max"]:
            return None
    return {
        "id": str(m.get("id")),
        "question": m.get("question") or m.get("slug") or "?",
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

def entry_signal(history, cfg):
    """history: list of (ts, ref_price) ascending. Returns (token_index, move)."""
    if len(history) < cfg["min_samples"]:
        return None, 0.0
    cutoff = history[-1][0] - cfg["lookback_min"] * 60
    window = [h for h in history if h[0] >= cutoff]
    if len(window) < cfg["min_samples"]:
        return None, 0.0
    move = window[-1][1] - window[0][1]
    if abs(move) < cfg["spike_threshold"]:
        return None, move
    # fade: ref spiked up -> buy the other outcome (1); down -> buy ref (0)
    return (1 if move > 0 else 0), move


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
        self.positions = state["positions"]
        self.cfg = cfg

    def open(self, market, token_index, price, move):
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
            "token_index": token_index,
            "token_id": market["tokens"][token_index],
            "outcome": market["outcomes"][token_index],
            "entry_price": price,
            "shares": shares,
            "cost": usd,
            "entry_ts": now_ts(),
            "last_price": price,
            "trigger_move": move,
        }
        self.positions[market["id"]] = pos
        record_trade("OPEN", pos, price, self.cash)
        log(f"OPEN  {market['question'][:60]!r}  buy {pos['outcome']} @ {price:.3f}"
            f"  ${usd:.2f} ({shares:.1f} sh)  trigger {move:+.3f}")
        return pos

    def close(self, market_id, price, reason):
        pos = self.positions.pop(market_id)
        proceeds = pos["shares"] * price
        pnl = proceeds - pos["cost"]
        self.cash += proceeds
        self.realized += pnl
        record_trade(f"CLOSE:{reason}", pos, price, self.cash)
        log(f"CLOSE {pos['question'][:60]!r}  sell {pos['outcome']} @ {price:.3f}"
            f"  pnl ${pnl:+.2f} ({reason})  bankroll ${self.cash:.2f}")
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
        keep_after = now_ts() - cfg["lookback_min"] * 60 * 3
        history[m["id"]] = [h for h in hist if h[0] >= keep_after][-60:]
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

        idx, move = entry_signal(hist, cfg)
        if idx is None:
            continue
        fill = side_fill_price(m, idx, "ask")
        if not cfg["price_min"] <= fill <= cfg["price_max"]:
            continue
        broker.open(m, idx, fill, move)

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


def start_health_server():
    """Render web services need an open port. No-op locally (no PORT set)."""
    port = os.environ.get("PORT")
    if not port:
        return
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"paperbot ok")

        def log_message(self, *_):
            pass

    srv = http.server.HTTPServer(("0.0.0.0", int(port)), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"health server on :{port}")


def run_dump_trades(cfg):
    if os.path.exists(TRADES_PATH):
        with open(TRADES_PATH, encoding="utf-8") as f:
            sys.stdout.write(f.read())
    else:
        sys.stdout.write(",".join(TRADE_HEADER) + "\n")


def run_report(cfg, state):
    broker = PaperBroker(state, cfg)
    by_id = {}
    try:
        by_id = {m["id"]: m for m in fetch_markets(cfg)}
    except Exception as e:  # noqa: BLE001
        log(f"(report) market fetch failed, using last stored prices: {e}")
    print(f"created:        {state.get('created')}")
    print(f"start bankroll: ${cfg['start_bankroll']:.2f}")
    print(f"cash:           ${broker.cash:.2f}")
    print(f"realized P&L:   ${broker.realized:+.2f}")
    print(f"open positions: {len(broker.positions)}")
    equity = broker.cash
    for p in broker.positions.values():
        m = by_id.get(p["market_id"])
        cur = side_fill_price(m, p["token_index"], "bid") if m else p["last_price"]
        equity += p["shares"] * cur
        unreal = p["shares"] * cur - p["cost"]
        held_h = (now_ts() - p["entry_ts"]) / 3600
        print(f"  {p['question'][:52]!r}  {p['outcome']}  "
              f"entry {p['entry_price']:.3f} now {cur:.3f}  "
              f"unreal ${unreal:+.2f}  {held_h:.1f}h")
    print(f"equity:         ${equity:.2f}  "
          f"({(equity / cfg['start_bankroll'] - 1) * 100:+.1f}%)")


def run_selftest():
    # keep selftest off the real state/trade/log files
    global TRADES_PATH, LOG_PATH
    import tempfile
    tmp = tempfile.mkdtemp(prefix="paperbot-selftest-")
    TRADES_PATH = os.path.join(tmp, "trades.csv")
    LOG_PATH = os.path.join(tmp, "bot.log")

    cfg = dict(DEFAULTS)
    state = {"cash": 1000.0, "realized": 0.0, "positions": {},
             "history": {}, "cooldowns": {}}
    b = PaperBroker(state, cfg)
    mkt = {"id": "m1", "question": "Test?", "outcomes": ["Yes", "No"],
           "tokens": ["tY", "tN"], "end": None,
           "best_bid0": 0.48, "best_ask0": 0.52}

    b.open(mkt, 1, 0.50, 0.12)                 # buy "No" at 0.50, $50
    assert abs(b.cash - 950.0) < 1e-6, b.cash
    assert abs(b.positions["m1"]["shares"] - 100.0) < 1e-6
    assert b.positions["m1"]["token_index"] == 1
    b.close("m1", 0.60, "take_profit")        # 100 * (0.60 - 0.50) = +10
    assert abs(b.realized - 10.0) < 1e-6, b.realized
    assert abs(b.cash - 1010.0) < 1e-6, b.cash

    # mirror pricing for outcome 1
    assert abs(side_fill_price(mkt, 1, "ask") - (1 - 0.48)) < 1e-9
    assert abs(side_fill_price(mkt, 0, "ask") - 0.52) < 1e-9

    t = 1_000_000.0
    up = [(t + i * 300, 0.40 + i * 0.02) for i in range(7)]
    assert entry_signal(up, cfg) == (1, up[-1][1] - up[0][1])
    down = [(t + i * 300, 0.60 - i * 0.02) for i in range(7)]
    idx, move = entry_signal(down, cfg)
    assert idx == 0 and move < -0.08, (idx, move)
    flat = [(t + i * 300, 0.50 + (0.001 if i % 2 else -0.001)) for i in range(7)]
    assert entry_signal(flat, cfg)[0] is None
    assert entry_signal(up[:2], cfg)[0] is None

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
    if mode == "trades":
        run_dump_trades(cfg)
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
    start_health_server()

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
