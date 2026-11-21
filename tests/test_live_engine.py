"""
Live-engine tests against a fake Binance — no network, no real orders.

    python tests/test_live_engine.py

Also: AI Market Guard rules, untested strategies blocked from live, Trade-tab
and Telegram orders going through the risk manager, admin-only Telegram API.

Covers: stop loss + take profit placed on the exchange at entry, stop-based
sizing at the risk manager's risk %, stop-out booking, take-profit booking,
stop placement failure → position flattened, signal flip, max positions,
daily loss limit, kill switch, hedge-mode refusal, demo stop replay, and that
ccxt turns our stop/target orders into Binance algo (conditional) orders.
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
WORK = tempfile.mkdtemp()
os.chdir(WORK)                       # keep bots.json / risk files out of the repo

import server        # noqa: E402
import engine        # noqa: E402
import risk_manager  # noqa: E402
import market_guard  # noqa: E402
import trade_ledger  # noqa: E402

TF = "4h"
TF_MS = 4 * 3600 * 1000


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeMarket:
    """Public market data: a candle series we control."""
    def __init__(self):
        self.price = 100.0
        self.candles = []
        now = int((time.time() + CLOCK["offset"]) * 1000) // TF_MS * TF_MS
        for i in range(260, 0, -1):          # closed candles
            p = 100.0
            self.candles.append([now - i * TF_MS, p, p * 1.01, p * 0.99, p, 1000.0])
        self.candles.append([now, 100.0, 100.0, 100.0, 100.0, 10.0])   # forming

    def fetch_ohlcv(self, symbol, tf, limit=200):
        return [list(c) for c in self.candles[-limit:]]

    def move(self, price, high=None, low=None):
        """Update the forming candle with a new price."""
        self.price = price
        c = self.candles[-1]
        c[2] = max(c[2], high if high is not None else price)
        c[3] = min(c[3], low if low is not None else price)
        c[4] = price

    def new_candle(self, price):
        """Close the forming candle and start a new one; the fake clock moves
        forward one candle so the new one is the 'forming' candle."""
        self.move(price)
        CLOCK["offset"] += TF_MS / 1000
        self.candles.append([self.candles[-1][0] + TF_MS, price, price, price, price, 10.0])


class FakeBinance:
    def __init__(self, mkt, equity=1000.0):
        self.mkt = mkt
        self.equity = equity
        self.position = None          # dict(side long/short, contracts, entry)
        self.orders = []              # open conditional orders
        self.fills = []
        self.fail_stop = False
        self.dual = False
        self.leverage = None
        self.next_id = 1

    # account
    def fapiPrivatePostAlgoOrder(self, req):      # present on ccxt >= algo-order support
        raise AssertionError("not used directly")

    def fapiPrivateGetPositionSideDual(self):
        return {"dualSidePosition": self.dual}

    def fetch_balance(self):
        upnl = 0.0
        if self.position:
            p = self.position
            d = 1 if p["side"] == "long" else -1
            upnl = d * (self.mkt.price - p["entry"]) * p["contracts"]
        return {"USDT": {"total": self.equity}, "info": {"totalMarginBalance": str(self.equity + upnl)}}

    def load_markets(self):
        return {}

    def fetch_ticker(self, sym):
        return {"last": self.mkt.price, "close": self.mkt.price}

    def market(self, sym):
        return {"limits": {"amount": {"min": 0.001}, "cost": {"min": 5}}}

    def amount_to_precision(self, sym, q):
        return f"{int(q * 1000) / 1000:.3f}"

    def price_to_precision(self, sym, p):
        return f"{p:.4f}"

    def set_leverage(self, lev, sym):
        self.leverage = lev

    def set_margin_mode(self, *a, **k):
        pass

    # positions/orders
    def fetch_positions(self, symbols=None):
        if not self.position:
            return []
        p = self.position
        d = 1 if p["side"] == "long" else -1
        return [{"symbol": "TEST/USDT:USDT", "side": p["side"], "contracts": p["contracts"],
                 "unrealizedPnl": d * (self.mkt.price - p["entry"]) * p["contracts"]}]

    def _fill(self, side, qty, price, reduce):
        fee = qty * price * engine.TAKER_FEE
        realized = 0.0
        if reduce and self.position:
            p = self.position
            d = 1 if p["side"] == "long" else -1
            realized = d * (price - p["entry"]) * qty
            p["contracts"] = round(p["contracts"] - qty, 8)
            if p["contracts"] <= 1e-9:
                self.position = None
        elif not reduce:
            self.position = {"side": "long" if side == "buy" else "short", "contracts": qty, "entry": price}
        self.equity += realized - fee
        self.fills.append({"side": side, "amount": qty, "price": price, "fee": {"cost": fee, "currency": "USDT"},
                           "info": {"realizedPnl": str(realized)}, "timestamp": int((time.time() + CLOCK["offset"]) * 1000)})

    def create_order(self, sym, typ, side, qty, price=None, params=None):
        params = params or {}
        oid = str(self.next_id); self.next_id += 1
        if "stopLossPrice" in params or "takeProfitPrice" in params:
            assert params.get("reduceOnly") is True, "stop/target must be reduceOnly"
            if "stopLossPrice" in params and self.fail_stop:
                raise RuntimeError("Binance rejected stop (simulated)")
            kind = "sl" if "stopLossPrice" in params else "tp"
            self.orders.append({"id": oid, "kind": kind, "side": side, "qty": qty,
                                "trigger": params.get("stopLossPrice") or params.get("takeProfitPrice")})
            return {"id": oid}
        self._fill(side, qty, self.mkt.price, bool(params.get("reduceOnly")))
        return {"id": oid, "average": self.mkt.price, "filled": qty}

    def cancel_all_orders(self, sym, params=None):
        assert (params or {}).get("trigger") is True
        self.orders = []

    def cancel_order(self, oid, sym, params=None):
        self.orders = [o for o in self.orders if o["id"] != oid]

    def fetch_my_trades(self, sym, since=None, limit=100):
        return [f for f in self.fills if since is None or f["timestamp"] >= since]

    def trigger_orders(self):
        """Exchange-side: fire any stop/target the price has crossed."""
        if not self.position:
            return
        for o in list(self.orders):
            long = self.position["side"] == "long"
            hit = (o["kind"] == "sl" and ((long and self.mkt.price <= o["trigger"]) or (not long and self.mkt.price >= o["trigger"]))) or \
                  (o["kind"] == "tp" and ((long and self.mkt.price >= o["trigger"]) or (not long and self.mkt.price <= o["trigger"])))
            if hit:
                self.orders.remove(o)
                self._fill(o["side"], o["qty"], o["trigger"], True)
                return


# ── harness ───────────────────────────────────────────────────────────────────
import types  # noqa: E402
CLOCK = {"offset": 0.0}
_real_time = time.time
server._time = types.SimpleNamespace(time=lambda: _real_time() + CLOCK["offset"], sleep=lambda s: None)
SIGNAL = {"v": None}
_REAL_SIGNAL = server._get_bot_signal
server._get_bot_signal = lambda bot, ohlcv: SIGNAL["v"]
server._tg_notify = lambda *a, **k: None
server._schedule_tick = lambda *a, **k: None          # we drive ticks by hand
server._save_bots = lambda: None


def fresh(equity=1000.0, mode="live"):
    mkt = FakeMarket()
    ex = FakeBinance(mkt, equity)
    server._get_pub_mkt = lambda: mkt
    server._crypto_bots.clear()
    server._wallet_cache.clear()
    server._hedge_mode_checked.clear()
    server._rm = risk_manager.RiskManager(os.path.join(WORK, f"rm_{time.time_ns()}.json"))
    server._active_ex.clear()
    server._active_ex["admin"] = {"binance": ex}
    server._pos_cache.clear()
    server._ledger = trade_ledger.TradeLedger(os.path.join(WORK, f"ledger_{time.time_ns()}.json"))
    server._guard = market_guard.MarketGuard({"btc_ohlcv": lambda: None, "fear_greed": lambda: None,
                                              "funding": lambda: {}},
                                             path=os.path.join(WORK, f"guard_{time.time_ns()}.json"))
    return mkt, ex


def add_bot(bid="b1", mode="live", sym="TEST/USDT:USDT", **kw):
    bot = server._strategy_bot_template("rsi_trend", sym, TF, {"sl_atr": 2.0, "reward_r": 3.0, **kw})
    bot.update({"id": bid, "username": "admin", "exchange": "binance", "mode": mode, "status": "active",
                "leverage": 10, "margin_mode": "isolated", "trades": [], "total_signals": 0,
                "demo_balance": 1000, "demo_equity": 1000, "open_amount": 0, "open_trade_count": 0})
    server._crypto_bots[bid] = bot
    return bot


def tick(bid="b1"):
    server._bot_tick(bid)
    return server._crypto_bots[bid]


PASS = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"FAIL: {name} {detail}")
    PASS.append(name)
    print(f"  ✓ {name}")


# ── tests ─────────────────────────────────────────────────────────────────────
def test_open_places_stop_and_target():
    mkt, ex = fresh()
    bot = add_bot()
    SIGNAL["v"] = "BUY"
    mkt.new_candle(100.0)
    tick()
    check("live BUY opened", bot["open_side"] == "BUY" and ex.position and ex.position["side"] == "long", bot.get("last_error"))
    kinds = sorted(o["kind"] for o in ex.orders)
    check("stop loss AND take profit on exchange", kinds == ["sl", "tp"], kinds)
    sl = [o for o in ex.orders if o["kind"] == "sl"][0]["trigger"]
    tp = [o for o in ex.orders if o["kind"] == "tp"][0]["trigger"]
    check("stop below entry, target above", sl < 100 < tp, (sl, tp))
    check("target = 3R", abs((tp - 100) / (100 - sl) - 3.0) < 0.05, (sl, tp))
    loss_at_stop = (100 - sl) * ex.position["contracts"]
    check("risk at stop ≈ 1% of 1000", 7.0 <= loss_at_stop <= 10.0, loss_at_stop)
    check("leverage capped by risk manager", ex.leverage == 10)
    return mkt, ex, bot, sl


def test_stop_out_booked():
    mkt, ex, bot, sl = test_open_places_stop_and_target()
    SIGNAL["v"] = None
    mkt.move(sl - 0.5, low=sl - 0.5)
    ex.trigger_orders()
    check("exchange stop fired", ex.position is None)
    tick()
    check("bot booked the close", bot["open_side"] is None)
    t = bot["trades"][-1]
    check("reason = stop_loss", t["exit_reason"] == "stop_loss", t)
    check("loss ≈ 1R incl. fees", -11.0 <= t["pnl"] <= -7.0, t["pnl"])
    check("leftover take profit cancelled", ex.orders == [])
    check("risk manager booked the loss", server._rm.state["realized_today"]["live"] < 0)


def test_take_profit_booked():
    mkt, ex, bot, sl = test_open_places_stop_and_target()
    SIGNAL["v"] = None
    tp = [o for o in ex.orders if o["kind"] == "tp"][0]["trigger"]
    mkt.move(tp + 0.1, high=tp + 0.1)
    ex.trigger_orders()
    tick()
    t = bot["trades"][-1]
    check("reason = take_profit", t["exit_reason"] == "take_profit", t)
    check("profit ≈ 3R", 20 <= t["pnl"] <= 31, t["pnl"])


def test_stop_failure_flattens():
    mkt, ex = fresh()
    bot = add_bot()
    ex.fail_stop = True
    SIGNAL["v"] = "BUY"
    mkt.new_candle(100.0)
    tick()
    check("no position left when stop fails", ex.position is None)
    check("bot flat", bot["open_side"] is None)
    check("error shown", "Stop loss could not be placed" in (bot.get("last_error") or ""), bot.get("last_error"))


def test_signal_flip():
    mkt, ex = fresh()
    bot = add_bot()
    SIGNAL["v"] = "BUY"; mkt.new_candle(100.0); tick()
    SIGNAL["v"] = "SELL"; mkt.new_candle(101.0); tick()
    check("flip closed the long", bot["open_side"] is None and ex.position is None)
    check("flip reason", bot["trades"][-1]["exit_reason"] == "signal_flip")
    check("flip removed exchange orders", ex.orders == [])
    tick()
    check("no re-entry on same candle", bot["open_side"] is None)
    mkt.new_candle(101.0); tick()
    check("opens SELL on next candle", bot["open_side"] == "SELL")


def test_max_positions():
    mkt, ex = fresh()
    # each bot gets its own symbol and its own fake exchange position slot
    SIGNAL["v"] = "BUY"
    mkt.new_candle(100.0)
    for i in range(4):
        b = add_bot(f"m{i}", mode="demo", sym=f"C{i}/USDT:USDT")
        tick(f"m{i}")
    opened = [b for b in server._crypto_bots.values() if b.get("open_side")]
    check("only 3 positions opened", len(opened) == 3, len(opened))
    check("4th told why", "3/3 positions" in (server._crypto_bots["m3"].get("last_error") or ""),
          server._crypto_bots["m3"].get("last_error"))
    SIGNAL["v"] = None
    mkt.new_candle(100.0)
    tick("m3")
    check("a waiting bot keeps saying every slot is in use", "3/3 trade slots are in use" in (server._crypto_bots["m3"].get("last_error") or ""),
          server._crypto_bots["m3"].get("last_error"))


def test_daily_loss_limit():
    mkt, ex = fresh()
    add_bot()
    SIGNAL["v"] = "BUY"
    mkt.new_candle(100.0)
    server._rm.check_daily("live", 1000.0)      # day starts at 1000
    ex.equity = 965.0                            # -3.5%
    server._wallet_cache.clear()
    bot = tick()
    check("daily limit blocks entry", bot["open_side"] is None and "Daily loss limit" in (bot.get("last_error") or ""),
          bot.get("last_error"))


def test_old_ccxt_refused():
    mkt, ex = fresh()
    bot = add_bot()
    type(ex).fapiPrivatePostAlgoOrder, saved = None, type(ex).fapiPrivatePostAlgoOrder
    del type(ex).fapiPrivatePostAlgoOrder
    try:
        SIGNAL["v"] = "BUY"; mkt.new_candle(100.0); tick()
        check("old ccxt refuses live trading", bot["open_side"] is None and "ccxt is too old" in (bot.get("last_error") or ""))
    finally:
        type(ex).fapiPrivatePostAlgoOrder = saved


def test_kill_switch_and_hedge():
    mkt, ex = fresh()
    bot = add_bot()
    server._rm.set_manual_halt(True)
    SIGNAL["v"] = "BUY"; mkt.new_candle(100.0); tick()
    check("kill switch blocks entry", bot["open_side"] is None and "Kill switch" in (bot.get("last_error") or ""))
    server._rm.set_manual_halt(False)
    ex.dual = True
    server._hedge_mode_checked.clear()
    mkt.new_candle(100.0); tick()
    check("hedge mode refused", bot["open_side"] is None and "Hedge Mode" in (bot.get("last_error") or ""), bot.get("last_error"))


def test_demo_stop_replay():
    mkt, ex = fresh()
    bot = add_bot(mode="demo")
    SIGNAL["v"] = "BUY"; mkt.new_candle(100.0); tick()
    check("demo opened with stop", bot["open_side"] == "BUY" and bot["sl"] < 100)
    wallet0 = server._rm.state["demo_wallet"]
    SIGNAL["v"] = None
    mkt.move(bot["sl"] - 1, low=bot["sl"] - 1)
    tick()
    check("demo stop hit from candle low", bot["open_side"] is None and bot["trades"][-1]["exit_reason"] == "stop_loss")
    check("demo wallet charged", server._rm.state["demo_wallet"] < wallet0)


def test_ccxt_builds_algo_orders():
    """Real ccxt object, network blocked at the HTTP layer: check the request
    our stop/target produce is a Binance algo (conditional) order."""
    import ccxt
    ex = ccxt.binanceusdm()
    try:
        ex.load_markets()
    except Exception as e:
        print(f"  (skipped ccxt mapping test — no network: {e})")
        return
    ex.apiKey, ex.secret = "x", "y"
    sent = {}
    ex.fapiPrivatePostAlgoOrder = lambda req, *a: sent.setdefault("algo", req) or {}
    ex.fapiPrivatePostOrder = lambda req, *a: sent.setdefault("plain", req) or {}
    try:
        ex.create_order("BTC/USDT:USDT", "market", "sell", 0.01, None,
                        {"stopLossPrice": 50000, "reduceOnly": True, "workingType": "MARK_PRICE"})
    except Exception:
        pass
    req = sent.get("algo") or {}
    check("stop goes to Binance algo endpoint", "algo" in sent, sent)
    check("stop type STOP_MARKET reduceOnly", req.get("type") == "STOP_MARKET" and str(req.get("reduceOnly")).lower() == "true", req)
    sent.clear()
    try:
        ex.create_order("BTC/USDT:USDT", "market", "sell", 0.01, None,
                        {"takeProfitPrice": 90000, "reduceOnly": True, "workingType": "MARK_PRICE"})
    except Exception:
        pass
    check("target type TAKE_PROFIT_MARKET", (sent.get("algo") or {}).get("type") == "TAKE_PROFIT_MARKET", sent)



# ── AI Market Guard ───────────────────────────────────────────────────────────
def _guard(btc=None, fg=None, funding=None, now=None):
    clock = {"t": now or _real_time()}
    g = market_guard.MarketGuard({
        "btc_ohlcv": lambda: btc, "fear_greed": lambda: fg, "funding": lambda: funding or {},
        "now": lambda: clock["t"],
    }, path=os.path.join(WORK, f"guard_{time.time_ns()}.json"))
    return g, clock


def _btc_series(last_move_pct=0.0, hours=1):
    rows = [[i, 100, 100, 100, 100.0, 1] for i in range(30)]
    rows[-1][4] = 100.0 * (1 + last_move_pct / 100)
    if hours > 1:
        for k in range(1, hours):
            rows[-1 - k][4] = 100.0
    return rows


def test_guard_rules():
    g, clock = _guard(btc=_btc_series(0.5), fg={"score": 50, "label": "Neutral"})
    g.refresh()
    check("guard: calm market allows entries at full risk", g.entry_check("BTC/USDT:USDT", "BUY") == {"ok": True, "risk_mult": 1.0, "reason": ""})
    g, clock = _guard(btc=_btc_series(-5.0), fg={"score": 50, "label": "Neutral"})
    g.refresh()
    r = g.entry_check("ETH/USDT:USDT", "SELL")
    check("guard: BTC -5% in 1h pauses new entries", not r["ok"] and "paused" in r["reason"], r)
    clock["t"] += 2 * 3600 + 60
    g.h["btc_ohlcv"] = lambda: _btc_series(0.1)
    g.refresh()
    check("guard: pause ends 2h after the shock", g.entry_check("ETH/USDT:USDT", "SELL")["ok"])
    g, _ = _guard(btc=_btc_series(0.1), fg={"score": 6, "label": "Extreme Fear"})
    g.refresh()
    check("guard: extreme fear halves risk", g.entry_check("SOL/USDT:USDT", "BUY")["risk_mult"] == 0.5)
    g, _ = _guard(btc=_btc_series(0.1), fg={"score": 50, "label": "Neutral"}, funding={"DOGE": 0.15, "XRP": -0.2})
    g.refresh()
    check("guard: crowded longs skip a LONG", not g.entry_check("DOGE/USDT:USDT", "BUY")["ok"])
    check("guard: crowded longs still allow a SHORT", g.entry_check("DOGE/USDT:USDT", "SELL")["ok"])
    check("guard: crowded shorts skip a SHORT", not g.entry_check("XRP/USDT:USDT", "SELL")["ok"])
    g.apply_llm({"risk_multiplier": 3.0, "avoid_symbols": ["ADA/USDT:USDT", "sol"]})
    check("guard: AI advice can never raise risk", g.entry_check("BTC/USDT:USDT", "BUY")["risk_mult"] == 1.0)
    check("guard: AI avoid list blocks the coin", not g.entry_check("ADA/USDT:USDT", "BUY")["ok"] and not g.entry_check("SOL/USDT:USDT", "SELL")["ok"])
    g.apply_llm({"risk_multiplier": 0.1})
    check("guard: AI risk floor is x0.5", g.entry_check("BTC/USDT:USDT", "BUY")["risk_mult"] == 0.5)
    g, clock = _guard(btc=None, fg=None)
    g.refresh()
    check("guard: no data = no opinion (never blocks)", g.entry_check("BTC/USDT:USDT", "BUY")["ok"])


def test_bot_obeys_guard():
    mkt, ex = fresh()
    bot = add_bot()
    server._guard, _ = _guard(btc=_btc_series(-8.0, hours=4), fg={"score": 50, "label": "Neutral"})
    server._guard.refresh()
    SIGNAL["v"] = "BUY"; mkt.new_candle(100.0); tick()
    check("bot: shock pause blocks the entry", bot["open_side"] is None and (bot.get("last_error") or "").startswith("⚡"), bot.get("last_error"))
    mkt, ex = fresh()
    bot = add_bot()
    server._guard, _ = _guard(btc=_btc_series(0.1), fg={"score": 95, "label": "Extreme Greed"})
    server._guard.refresh()
    SIGNAL["v"] = "BUY"; mkt.new_candle(100.0); tick()
    sl = [o for o in ex.orders if o["kind"] == "sl"][0]["trigger"]
    loss = (100 - sl) * ex.position["contracts"]
    check("bot: extreme greed halves the size (≈0.5% risk)", 3.5 <= loss <= 5.0, loss)


def test_untested_strategy_blocked_live():
    mkt, ex = fresh()
    bot = add_bot()
    bot["strategy"], bot["timeframe"] = "rsi", "1h"
    SIGNAL["v"] = "BUY"; mkt.new_candle(100.0); tick()
    check("live: untested strategy cannot open", bot["open_side"] is None and "Live entries blocked" in (bot.get("last_error") or ""), bot.get("last_error"))
    check("live: tested profile allowed", server._live_profile_error("rsi_trend", "4h") is None)
    check("live: tested strategy on the wrong timeframe refused", server._live_profile_error("rsi_trend", "15m") is not None)
    demo = add_bot("d1", mode="demo", sym="D1/USDT:USDT")
    demo["strategy"], demo["timeframe"] = "rsi", "1h"
    tick("d1")
    check("demo: untested strategy still runs", demo["open_side"] == "BUY", demo.get("last_error"))


# ── Manual (Trade tab) orders through the risk manager ────────────────────────
def _plan(ex, side="buy", sl=98.0, tp=None, amount=0.0, sym="TEST/USDT:USDT", lev=10):
    return server._manual_order_plan(ex, "admin", sym, side, sl, tp, lev, amount)


def test_manual_order_rules():
    mkt, ex = fresh()
    mkt.price = 100.0
    for args, why in ((dict(sl=None), "required"), (dict(sl=101.0), "BELOW"), (dict(side="sell", sl=99.0), "ABOVE"),
                      (dict(sl=98.0, tp=99.0), "Take profit")):
        try:
            _plan(ex, **args)
            check(f"manual: refused ({why})", False)
        except server._Skip as e:
            check(f"manual: refused ({why})", why in str(e), str(e))
    p = _plan(ex, sl=98.0, tp=106.0)
    check("manual: size from the stop ≈ 1% risk", 8.0 <= p["risk_usd"] <= 10.01 and p["qty"] == p["max_qty"], p)
    check("manual: reward shown in R", abs(p["rr"] - 3.0) < 0.01, p["rr"])
    try:
        _plan(ex, sl=98.0, amount=p["max_qty"] * 3)
        check("manual: oversize refused", False)
    except server._Skip as e:
        check("manual: oversize refused", "Too big" in str(e), str(e))
    server._rm.set_manual_halt(True)
    try:
        _plan(ex)
        check("manual: kill switch blocks", False)
    except server._Skip as e:
        check("manual: kill switch blocks", "Kill switch" in str(e))
    server._rm.set_manual_halt(False)


def test_manual_order_places_protection():
    mkt, ex = fresh()
    mkt.price = 100.0
    p = _plan(ex, sl=98.0, tp=106.0)
    r = server._manual_order_execute(ex, "admin", p, "MANUAL")
    kinds = sorted(o["kind"] for o in ex.orders)
    check("manual: position + stop + target on Binance", ex.position and kinds == ["sl", "tp"], kinds)
    check("manual: counted by the risk manager", server._open_positions("live", ex, "admin") == 1)
    try:
        _plan(ex, sl=98.0)
        check("manual: second order on the same coin refused", False)
    except server._Skip as e:
        check("manual: second order on the same coin refused", "already have" in str(e), str(e))
    bot = add_bot()
    SIGNAL["v"] = "BUY"; mkt.new_candle(100.0); tick()
    check("bot: will not trade a coin held by hand", bot["open_side"] is None, bot.get("last_error"))
    mkt2, ex2 = fresh()
    mkt2.price = 100.0
    ex2.fail_stop = True
    try:
        server._manual_order_execute(ex2, "admin", _plan(ex2, sl=98.0), "MANUAL")
        check("manual: stop failure flattens", False)
    except RuntimeError as e:
        check("manual: stop failure flattens", ex2.position is None and "closed for safety" in str(e), str(e))


def test_manual_counts_toward_bot_limit():
    mkt, ex = fresh()
    mkt.price = 100.0
    server._rm.update_config({"max_positions": 1})
    server._manual_order_execute(ex, "admin", _plan(ex, sl=98.0), "MANUAL")
    bot = add_bot("b9", sym="OTHER/USDT:USDT")
    SIGNAL["v"] = "BUY"; mkt.new_candle(100.0); tick("b9")
    check("bot: hand-placed position uses up the slot", bot["open_side"] is None and "1/1 positions" in (bot.get("last_error") or ""), bot.get("last_error"))


def test_telegram_live_needs_stop_and_admin():
    mkt, ex = fresh()
    mkt.price = 100.0
    r = server._tg_execute_signal({"symbol": "TEST/USDT:USDT", "side": "buy", "sl": None, "leverage": 50}, {})
    check("telegram: live signal without stop refused", not r[0]["ok"] and "no stop" in r[0]["error"], r)
    r = server._tg_execute_signal({"symbol": "TEST/USDT:USDT", "side": "buy", "sl": 97.0, "tps": [103.0], "leverage": 50}, {})
    check("telegram: live signal sized by risk manager + protected", r[0]["ok"] and sorted(o["kind"] for o in ex.orders) == ["sl", "tp"], r)
    check("telegram: leverage capped at risk manager max", ex.leverage == 10, ex.leverage)
    # clients may READ the signal feed (the middleware checks their Telegram permission);
    # setup, tests, login codes and trading stay admin-only
    view_only = {p for p, perm in server._CLIENT_PERM_PATHS if perm == "telegram"}
    open_routes = []
    for route in server.app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/telegram"):
            deps = [d.call for d in route.dependant.dependencies]
            needs = server._get_current_user if path in view_only else server._require_admin
            if needs not in deps:
                open_routes.append(path)
    check("telegram: setup and trading need an admin login; the feed needs a login", not open_routes, open_routes)
    check("capital-protection endpoints removed", not any("capital-protection" in getattr(r, "path", "") for r in server.app.routes))


def test_accounts_have_separate_risk():
    mkt, ex = fresh()
    server._rm_users.clear()
    server._rm.update_config({"max_positions": 1})
    client_bot = add_bot("c1", mode="demo", sym="CL/USDT:USDT")
    client_bot["username"] = "client1"
    SIGNAL["v"] = "BUY"; mkt.new_candle(100.0); tick("c1")
    check("accounts: client bot trades from its own wallet", client_bot["open_side"] == "BUY", client_bot.get("last_error"))
    check("accounts: client position not counted for the owner", server._open_positions("demo", None, "admin") == 0)
    owner = add_bot("o1", mode="demo", sym="OW/USDT:USDT")
    tick("o1")
    check("accounts: owner can still open with max_positions=1", owner["open_side"] == "BUY", owner.get("last_error"))
    server._rm_for("client1").set_manual_halt(True)
    check("accounts: client kill switch leaves the owner alone", not server._rm.state["manual_halt"])
    check("accounts: owner wallet untouched by client trade", server._rm.state["demo_wallet"] == server._rm.config["demo_capital"])


# ── ALGO BOTS: AI picks the coin, user sets balance; SL/TP automatic ─────────
def test_bot_sizes_from_its_balance():
    mkt, ex = fresh()
    bot = add_bot(mode="demo")
    bot["capital_usd"] = 200.0
    SIGNAL["v"] = "BUY"; mkt.new_candle(100.0); tick()
    loss = (100 - bot["sl"]) * bot["open_amount"]
    check("balance: risk ≈ 1% of the bot's $200, not the $1000 wallet", 1.5 <= loss <= 2.0, loss)


def test_start_bot_ai_pair_and_auto_exits():
    mkt, ex = fresh()
    calls = []
    def fake_pick(strategy, tf, params, exclude=(), keep=None):
        calls.append((strategy, tf, params, set(exclude)))
        return {"symbol": "PICK/USDT:USDT", "why": "momentum 80", "edge": True}
    server._autopilot.pick_for_strategy = fake_pick
    req = server.CryptoBotReq(strategy="rsi_trend", timeframe="4h", leverage=50, capital_usd=150,
                              mode="demo", sl_atr=9.0, reward_r=9.0)
    r = server.crypto_algo_start(req, {"username": "admin"})
    bot = server._crypto_bots[r["bot_id"]]
    check("start: AI picked the coin", bot["symbol"] == "PICK/USDT:USDT" and bot["ai_pair"], bot["symbol"])
    check("start: tested stop/target forced (3 ATR, 3R)", bot["sl_atr"] == 3.0 and bot["reward_r"] == 3.0, (bot["sl_atr"], bot["reward_r"]))
    check("start: leverage capped by risk manager", bot["leverage"] == 10, bot["leverage"])
    check("start: balance stored", bot["capital_usd"] == 150)
    r2 = server.crypto_algo_start(server.CryptoBotReq(strategy="ema_trend", timeframe="4h", capital_usd=100, mode="demo"), {"username": "admin"})
    check("start: picker skips coins other bots hold", "PICK/USDT:USDT" in calls[-1][3])
    b2 = server._crypto_bots[r2["bot_id"]]
    check("start: each strategy gets its own tested stop/target (EMA trend 2 ATR / 3R)", b2["sl_atr"] == 2.0 and b2["reward_r"] == 3.0)
    gone = server.crypto_algo_start(server.CryptoBotReq(strategy="rsi", timeframe="1h", capital_usd=100, mode="demo"), {"username": "admin"})
    check("start: strategies that failed the research are gone", getattr(gone, "status_code", 200) == 400)
    bad = server.crypto_algo_start(server.CryptoBotReq(strategy="rsi_trend", timeframe="4h", capital_usd=5000, mode="demo"), {"username": "admin"})
    check("start: balance above the wallet refused", getattr(bad, "status_code", 200) == 400)
    bad = server.crypto_algo_start(server.CryptoBotReq(strategy="rsi_trend", timeframe="4h", capital_usd=0, mode="demo"), {"username": "admin"})
    check("start: balance required", getattr(bad, "status_code", 200) == 400)


def test_ai_repick_only_when_flat():
    mkt, ex = fresh()
    bot = add_bot(mode="demo", sym="OLD/USDT:USDT")
    bot["ai_pair"] = True
    server._autopilot.pick_for_strategy = lambda *a, **k: {"symbol": "NEW/USDT:USDT", "why": "better momentum"}
    bot["open_side"] = "BUY"
    server._ai_pair_repick(bot)
    check("repick: never moves a bot with an open position", bot["symbol"] == "OLD/USDT:USDT")
    bot["open_side"] = None
    msg = server._ai_pair_repick(bot)
    check("repick: flat bot follows the AI pick", bot["symbol"] == "NEW/USDT:USDT" and msg, msg)


def test_picker_ranks_and_keeps():
    import autopilot as apm
    edge = {"AAA/USDT:USDT": 0.4, "BBB/USDT:USDT": -0.2, "CCC/USDT:USDT": 0.1}
    def ohlcv(sym, tf, limit):
        rows, p = [], 100.0
        step = {"AAA/USDT:USDT": 1.004, "BBB/USDT:USDT": 1.006, "CCC/USDT:USDT": 1.002}[sym]
        for i in range(limit):
            p *= step
            rows.append([i, p, p * 1.01, p * 0.99, p, 1000 + i])
        return rows
    ap = apm.Autopilot({
        "tickers": lambda: {s: {"quoteVolume": 1e9} for s in edge},
        "ohlcv": ohlcv, "history": lambda s, tf, d: [],
        "backtest": lambda st, rows, s, tf, params: {"avg_r": edge[s], "total_trades": 20, "profit_factor": 1.3},
    }, path=os.path.join(WORK, "ap_pick.json"))
    pick = ap.pick_for_strategy("trend_breakout", "4h", {})
    check("picker: coin with the best backtest edge wins over pure momentum", pick["symbol"] == "AAA/USDT:USDT", pick)
    check("picker: hands the bot a coin list of only the coins with an edge, best first",
          pick["coins"] == ["AAA/USDT:USDT", "CCC/USDT:USDT"], pick.get("coins"))
    pick = ap.pick_for_strategy("trend_breakout", "4h", {}, exclude={"AAA/USDT:USDT"})
    check("picker: excluded coin skipped", pick["symbol"] == "CCC/USDT:USDT", pick["symbol"])
    pick = ap.pick_for_strategy("trend_breakout", "4h", {}, keep="CCC/USDT:USDT")
    check("picker: keeps the current coin while it still has an edge", pick["symbol"] == "CCC/USDT:USDT" and pick["kept"])
    pick = ap.pick_for_strategy("trend_breakout", "4h", {}, exclude={"AAA/USDT:USDT", "CCC/USDT:USDT"})
    check("picker: never picks a coin where the strategy loses money", pick["symbol"] is None and "made money" in pick["why"], pick)
    server._autopilot.pick_for_strategy = lambda *a, **k: {"symbol": None, "why": "none of the 3 coins tested made money"}
    bot = add_bot("stay", mode="demo", sym="OLD/USDT:USDT", ai_pair=True)
    check("repick: bot stays on its coin when no coin has an edge",
          server._ai_pair_repick(bot) is None and bot["symbol"] == "OLD/USDT:USDT" and "staying put" in bot["ai_pair_why"])


def test_ai_coin_list():
    mkt, ex = fresh()
    A, B, C = "AAA/USDT:USDT", "BBB/USDT:USDT", "CCC/USDT:USDT"
    hot = set()
    server._get_bot_signal = lambda bot, ohlcv: "BUY" if bot["symbol"] in hot else None
    try:
        bot = add_bot("s", mode="demo", sym=A)
        bot.update({"ai_pair": True, "ai_coins": [A, B, C]})
        mkt.new_candle(100.0); tick("s")
        check("coin list: no signal on any coin → bot waits and says so",
              bot["open_side"] is None and "scanned 3 coins" in (bot.get("ai_scan_note") or ""), bot.get("ai_scan_note"))
        hot.add(B)
        mkt.new_candle(100.0); tick("s")
        check("coin list: bot trades whichever coin on its list gives a signal",
              bot["symbol"] == B and bot["open_side"] == "BUY", (bot["symbol"], bot["open_side"]))
        server._autopilot.pick_for_strategy = lambda *a, **k: {"symbol": C, "coins": [C, A], "why": "fresh list"}
        server._ai_pair_repick(bot)
        check("coin list: AI refreshes the list during a trade but the trade stays on its coin",
              bot["ai_coins"] == [C, A] and bot["symbol"] == B and bot["open_side"] == "BUY")
        other = add_bot("t", mode="demo", sym=A)
        other.update({"ai_pair": True, "ai_coins": [B, C]})
        hot.update({C})
        mkt.new_candle(100.0); tick("t")
        check("coin list: a coin another bot is trading is skipped, the next signal is taken",
              other["symbol"] == C and other["open_side"] == "BUY", (other["symbol"], other["open_side"]))
    finally:
        server._get_bot_signal = lambda bot, ohlcv: SIGNAL["v"]


def test_stop_cap_and_hold_limit():
    import engine
    mkt, ex = fresh()
    SIGNAL["v"] = "BUY"
    mkt.new_candle(100.0)
    wild = add_bot("w", mode="demo", sym="WILD/USDT:USDT", max_stop_pct=1.0, max_hold_bars=0)
    tick("w")
    check("stop cap: a signal whose stop would be too far away is not traded",
          wild["open_side"] is None and "too wild" in (wild.get("last_error") or ""), wild.get("last_error"))
    slow = add_bot("h", mode="demo", sym="SLOW/USDT:USDT", max_stop_pct=0, max_hold_bars=3)
    tick("h")
    check("hold limit: trade opens normally", slow["open_side"] == "BUY")
    SIGNAL["v"] = None
    for _ in range(2):
        mkt.new_candle(100.0); tick("h")
    check("hold limit: still open before the limit", slow["open_side"] == "BUY")
    mkt.new_candle(100.0); tick("h")
    check("hold limit: closed once it has sat for the limit without stop or target",
          slow["open_side"] is None and slow["trades"][-1].get("exit_reason") == "time_exit", slow["trades"][-1:])
    rows = [[i * 3600000, 100, 101, 99, 100, 1000] for i in range(300)]
    flip = iter(["BUY"] + [None] * 1000)
    res = engine.backtest(lambda b, w: next(flip), {"sl_atr": 2.0, "reward_r": 3.0, "max_hold_bars": 10, "max_stop_pct": 0}, rows, 50)
    check("hold limit: the backtest closes a flat trade after the same number of candles",
          res["total_trades"] == 1 and res["trades"][0]["exit_reason"] == "time_exit", res["trades"][:1])
    res = engine.backtest(lambda b, w: "BUY", {"sl_atr": 2.0, "reward_r": 3.0, "max_stop_pct": 1.0, "max_hold_bars": 0}, rows, 50)
    check("stop cap: the backtest skips the same too-wide trades", res["total_trades"] == 0)


def test_stale_errors_and_pnl_clear():
    mkt, ex = fresh()
    SIGNAL["v"] = "BUY"
    mkt.new_candle(100.0)
    bot = add_bot("e", mode="demo", sym="ERR/USDT:USDT")
    tick("e")
    bot["last_error"] = "⚠ binanceusdm GET https://fapi.binance.com/fapi/v1/klines (timeout)"
    tick("e")
    check("a network error is cleared once data comes back", bot["open_side"] == "BUY" and not bot.get("last_error"), bot.get("last_error"))
    SIGNAL["v"] = "SELL"
    mkt.new_candle(100.0); tick("e")
    check("a closed trade leaves no stale open P/L behind", bot["trades"][0].get("exit_reason") == "signal_flip" and bot.get("unrealized_pnl") is None,
          (bot["trades"][0].get("exit_reason"), bot.get("unrealized_pnl")))


def test_bot_balances_split_the_wallet():
    mkt, ex = fresh()
    server._autopilot.pick_for_strategy = lambda *a, **k: {"symbol": f"P{time.time_ns()}/USDT:USDT", "why": "x"}
    start = lambda cap, mode="demo": server.crypto_algo_start(
        server.CryptoBotReq(strategy="trend_breakout", timeframe="4h", capital_usd=cap, mode=mode), {"username": "admin"})
    a = start(600)
    check("split: first bot takes $600 of the $1000 demo wallet", isinstance(a, dict) and a.get("success"))
    b = start(500)
    check("split: $500 more refused (only $400 free)", getattr(b, "status_code", 200) == 400 and "free $400" in b.body.decode())
    c = start(400)
    check("split: exactly the free $400 allowed", isinstance(c, dict) and c.get("success"))
    check("split: allocated = wallet", server._allocated("admin", "demo") == 1000.0)
    server._crypto_bots[a["bot_id"]]["status"] = "stopped"
    check("split: a stopped bot frees its share", server._allocated("admin", "demo") == 400.0)
    d = start(500)
    check("split: freed share can be reused", isinstance(d, dict) and d.get("success"))
    r = server.crypto_algo_resume(a["bot_id"], {"username": "admin"})
    check("split: resuming is refused when its share is gone", getattr(r, "status_code", 200) == 400)
    check("split: live wallet counted separately", server._allocation_error("admin", "live", 900) is None)


def test_picker_only_real_crypto():
    import autopilot as apm
    syms = ["TSLA/USDT:USDT", "XAU/USDT:USDT", "ETH/USDT:USDT", "PAXG/USDT:USDT"]
    kinds = {"TSLA/USDT:USDT": ("EQUITY", ["TradFi"]), "XAU/USDT:USDT": ("COMMODITY", ["TradFi"]),
             "ETH/USDT:USDT": ("COIN", ["Layer-1", "Crypto"]), "PAXG/USDT:USDT": ("COIN", ["RWA", "Crypto"])}
    def ohlcv(sym, tf, limit):
        p, rows = 100.0, []
        for i in range(limit):
            p *= 1.003
            rows.append([i, p, p * 1.01, p * 0.99, p, 1000])
        return rows
    ap = apm.Autopilot({
        "tickers": lambda: {s: {"quoteVolume": 1e9} for s in syms},
        "markets": lambda: {s: {"info": {"underlyingType": k[0], "underlyingSubType": k[1]}} for s, k in kinds.items()},
        "ohlcv": ohlcv, "history": lambda s, tf, d: [],
        "backtest": lambda st, rows, s, tf, params: {"avg_r": 0.9 if s != "ETH/USDT:USDT" else 0.1, "total_trades": 20},
    }, path=os.path.join(WORK, "ap_crypto.json"))
    universe = [s for s, _ in ap._universe(ap.config)]
    check("universe: stock / gold / RWA perps excluded", universe == ["ETH/USDT:USDT"], universe)
    check("picker: picks real crypto even if a stock backtests better", ap.pick_for_strategy("trend_breakout", "4h", {})["symbol"] == "ETH/USDT:USDT")


# ── Strategy Lab: custom (no-code) strategies ─────────────────────────────────
def test_custom_strategy_rules():
    import custom_strategy as cs
    for bad, why in (({}, "at least one"), ({"long": [{"left": {"type": "value", "value": 1}, "op": ">", "right": {"type": "close"}}]}, "left side"),
                     ({"long": [{"left": {"type": "ema", "period": 1}, "op": ">", "right": {"type": "close"}}]}, "period"),
                     ({"long": [{"left": {"type": "close"}, "op": "==", "right": {"type": "close"}}]}, "comparison"),
                     ({"long": [{"left": {"type": "evil"}, "op": ">", "right": {"type": "close"}}]}, "unknown indicator")):
        try:
            cs.validate(bad)
            check(f"custom: rejects bad definition ({why})", False)
        except cs.InvalidStrategy as e:
            check(f"custom: rejects bad definition ({why})", why in str(e), str(e))
    d = cs.validate({"long": [{"left": {"type": "close"}, "op": "crosses_above", "right": {"type": "donchian_high", "period": 5}}],
                     "short": [], "sl_atr": 99, "reward_r": 0.1})
    check("custom: stop/target clamped to safe ranges", d["sl_atr"] == 6.0 and d["reward_r"] == 0.5)
    flat = [[i, 100, 101, 99, 100, 10] for i in range(40)]
    check("custom: no signal in a flat market", cs.signal(d, flat) is None)
    brk = flat + [[40, 100, 106, 100, 105, 10]]
    check("custom: breakout above the previous 5-bar high → BUY", cs.signal(d, brk) == "BUY")
    check("custom: no repeat signal on the next bar (cross already happened)", cs.signal(d, brk + [[41, 105, 107, 104, 106, 10]]) is None)
    rsi_d = cs.validate({"short": [{"left": {"type": "rsi", "period": 14}, "op": ">", "right": {"type": "value", "value": 70}}]})
    up = [[i, 100 + i, 101 + i, 99 + i, 100 + i, 10] for i in range(60)]
    check("custom: RSI > 70 in a steady rally → SELL", cs.signal(rsi_d, up) == "SELL")
    for k, tpl in cs.TEMPLATES.items():
        cs.validate(tpl)
    check("custom: every template is valid", True)


def test_custom_strategy_runs_in_demo_only():
    import custom_strategy as cs
    mkt, ex = fresh()
    data = {"abc123": {"owner": "admin", "name": "Breakout", "def": cs.validate(cs.TEMPLATES["donchian_breakout"])}}
    server._custom_save_all(data)
    server._autopilot.pick_for_strategy = lambda *a, **k: {"symbol": "CUS/USDT:USDT", "why": "x"}
    r = server.crypto_algo_start(server.CryptoBotReq(strategy="custom:abc123", timeframe="4h", capital_usd=100, mode="demo"), {"username": "admin"})
    bot = server._crypto_bots[r["bot_id"]]
    check("custom: demo bot carries its rules and stop/target", bot["custom_def"]["sl_atr"] == 2.0 and bot["reward_r"] == 3.0, bot.get("reward_r"))
    flat = [[i, 100, 101, 99, 100, 10] for i in range(200)]
    check("custom: bot signal comes from the custom rules", _REAL_SIGNAL(bot, flat) is None
          and _REAL_SIGNAL(bot, flat + [[200, 100, 110, 100, 109, 10]]) == "BUY")
    live = server.crypto_algo_start(server.CryptoBotReq(strategy="custom:abc123", timeframe="4h", capital_usd=100, mode="live"), {"username": "admin"})
    check("custom: live refused", getattr(live, "status_code", 200) == 400 and "demo only" in live.body.decode())
    other = server.crypto_algo_start(server.CryptoBotReq(strategy="custom:abc123", timeframe="4h", capital_usd=50, mode="demo"), {"username": "someone"})
    check("custom: another user cannot use my strategy", getattr(other, "status_code", 200) == 400)


# ── Reports: one ledger for every trade ───────────────────────────────────────
def test_ledger_records_bot_trades():
    mkt, ex, bot, sl = test_open_places_stop_and_target()
    recs = server._ledger.all()
    check("ledger: bot entry recorded as open", len(recs) == 1 and recs[0]["status"] == "open" and recs[0]["side"] == "LONG", recs)
    SIGNAL["v"] = None
    mkt.move(sl - 0.5, low=sl - 0.5); ex.trigger_orders(); tick()
    r = server._ledger.all()[0]
    check("ledger: close booked with net P/L", r["status"] == "closed" and r["pnl"] < 0 and r["exit_reason"] == "stop_loss", r)
    check("ledger: R multiple ≈ -1", r["r"] is not None and -1.3 <= r["r"] <= -0.8, r["r"])
    check("ledger: source and account kept", r["source"] == "bot" and r["account"] == "admin" and r["mode"] == "live")


def test_ledger_tracks_manual_trades():
    mkt, ex = fresh()
    mkt.price = 100.0
    server._manual_order_execute(ex, "admin", server._manual_order_plan(ex, "admin", "TEST/USDT:USDT", "buy", 98.0, 106.0, 10), "MANUAL")
    check("ledger: manual order recorded", server._ledger.open_records(source="manual"))
    server._ledger_track_once()
    check("ledger: still open while Binance holds it", server._ledger.open_records(source="manual"))
    mkt.move(97.5, low=97.5); ex.trigger_orders()
    server._pos_cache.clear()
    server._ledger_track_once()
    r = [x for x in server._ledger.all() if x["source"] == "manual"][0]
    check("ledger: manual trade closed after the exchange stop fired", r["status"] == "closed" and r["exit_reason"] == "stop_loss", r)
    check("ledger: manual loss ≈ 1% incl. fees", -11.5 <= r["pnl"] <= -8.0, r["pnl"])
    check("ledger: leftover take profit cancelled", ex.orders == [])


def test_report_statistics():
    rows = []
    for i, (p, side) in enumerate([(10, "LONG"), (-5, "LONG"), (20, "SHORT"), (-5, "SHORT"), (-5, "LONG")]):
        rows.append({"id": str(i), "status": "closed", "pnl": p, "side": side, "r": p / 5, "symbol": "BTC/USDT:USDT",
                     "strategy": "x", "source": "bot", "mode": "demo", "open_time": f"2026-11-0{i+1} 10:00:00",
                     "close_time": f"2026-11-0{i+1} 14:00:00"})
    a = trade_ledger.analyse(rows)["summary"]
    check("report: net / win rate / PF", a["net_pnl"] == 15 and a["win_rate"] == 40.0 and a["profit_factor"] == 2.0, a)
    check("report: payoff, expectancy, avg R", a["payoff"] == 3.0 and a["expectancy"] == 3.0 and a["avg_r"] == 0.6, a)
    check("report: max drawdown from the P/L curve", a["max_drawdown"] == 10, a["max_drawdown"])
    check("report: long/short split", a["long"]["trades"] == 3 and a["short"]["pnl"] == 15, (a["long"], a["short"]))
    check("report: streaks and duration", a["max_loss_streak"] == 2 and a["avg_duration_min"] == 240.0, a)


def test_report_access_and_filters():
    mkt, ex = fresh()
    L = server._ledger
    L.add_closed("a1", account="admin", source="autopilot", mode="demo", symbol="BTC/USDT:USDT", side="LONG",
                 pnl=5, close_time=trade_ledger.now_utc())
    L.add_closed("c1", account="client1", source="bot", mode="live", symbol="ETH/USDT:USDT", side="SHORT",
                 pnl=-3, close_time=trade_ledger.now_utc())
    L.add_closed("old", account="admin", source="bot", mode="demo", symbol="SOL/USDT:USDT", side="LONG",
                 pnl=9, close_time="2020-01-01 00:00:00")
    server._telegram_demo_records = lambda: []
    admin = {"username": "admin", "role": "admin"}
    client = {"username": "client1", "role": "client"}
    r = server.reports(period="30d", account="me", current_user=admin)
    check("report: admin sees own trades in the period", r["summary"]["trades"] == 1 and r["summary"]["net_pnl"] == 5)
    r = server.reports(period="all", account="all", current_user=admin)
    check("report: admin 'all accounts' sees everyone, all time", r["summary"]["trades"] == 3)
    r = server.reports(period="all", account="all", current_user=client)
    check("report: a client only ever sees their own", r["summary"]["trades"] == 1 and r["trades"][0]["account"] == "client1")
    r = server.reports(period="all", source="autopilot", account="all", current_user=admin)
    check("report: source filter", r["summary"]["trades"] == 1 and r["trades"][0]["source"] == "autopilot")


def test_ledger_backfill_once():
    mkt, ex = fresh()
    b = add_bot(mode="demo")
    b["trades"] = [{"time": "2026-11-10 08:00", "mode": "demo", "signal": "BUY", "price": 100, "amount": 1,
                    "sl": 98, "tp": 106, "risk_usd": 2, "pnl": 6, "status": "closed", "exit_reason": "take_profit",
                    "exit_price": 106, "exit_time": "2026-11-10 20:00"}]
    server._ledger_backfill(); server._ledger_backfill()
    rows = server._ledger.all()
    check("backfill: past bot trades imported once", len(rows) == 1 and rows[0]["r"] == 3.0 and rows[0]["duration_min"] == 720.0, rows)


# ── Profile, security and permissions ─────────────────────────────────────────
def _mk_users():
    import secrets as _s
    from datetime import datetime as _dt, timedelta as _td
    def u(name, role, pw, **kw):
        salt = _s.token_hex(8)
        return {"username": name, "salt": salt, "password_hash": server._hash_pw(pw, salt), "display_name": name,
                "role": role, **kw}
    server._save_users({"users": [u("admin", "admin", "adminpass1"),
                                  u("cli", "client", "clientpass1", contact="t.me/cli",
                                    expiry=(_dt.now() + _td(days=10)).isoformat(), created=_dt.now().isoformat(),
                                    permissions={"live_trading": False, "max_bots": 1})]})
    server._auth_sessions.clear()
    return server._make_session("cli", "cli", "client"), server._make_session("cli", "cli", "client")


def test_profile_and_security():
    mkt, ex = fresh()
    tok1, tok2 = _mk_users()
    cli = server._auth_sessions[tok1]
    me = server.account_me(authorization="Bearer " + tok1, current_user=cli)
    check("profile: client sees own account, status and permissions",
          me["role"] == "client" and me["sessions"] == 2 and me["permissions"]["live_trading"] is False and me["days_left"] in (9, 10), me)
    bad = server.account_profile(server.AccountProfileReq(display_name="New Name", contact=""), current_user=cli)
    check("profile: client must keep a contact", getattr(bad, "status_code", 200) == 400)
    ok = server.account_profile(server.AccountProfileReq(display_name="New Name", contact="+880"), current_user=cli)
    check("profile: name saved and session badge updated", ok["success"] and server._auth_sessions[tok2]["display_name"] == "New Name")
    short = server.auth_change_password(server.AuthChangePasswordRequest(old_password="clientpass1", new_password="short"), authorization="Bearer " + tok1)
    check("security: short new password refused", getattr(short, "status_code", 200) == 400)
    wrong = server.auth_change_password(server.AuthChangePasswordRequest(old_password="nope", new_password="longenough1"), authorization="Bearer " + tok1)
    check("security: wrong current password refused", getattr(wrong, "status_code", 200) == 400)
    r = server.auth_change_password(server.AuthChangePasswordRequest(old_password="clientpass1", new_password="longenough1"), authorization="Bearer " + tok1)
    check("security: password changed and the other device signed out", r["ok"] and r["signed_out"] == 1 and tok2 not in server._auth_sessions and tok1 in server._auth_sessions)
    tok3 = server._make_session("cli", "New Name", "client")
    r = server.account_logout_others(authorization="Bearer " + tok1, current_user=cli)
    check("security: sign out other devices", r["signed_out"] == 1 and tok3 not in server._auth_sessions)


def test_client_permissions_enforced():
    mkt, ex = fresh()
    tok, _ = _mk_users()
    cli = server._auth_sessions[tok]
    server._autopilot.pick_for_strategy = lambda *a, **k: {"symbol": f"Q{time.time_ns()}/USDT:USDT", "why": "x"}
    server._active_ex["cli"] = {"binance": ex}
    live = server.crypto_algo_start(server.CryptoBotReq(strategy="rsi_trend", timeframe="4h", capital_usd=100, mode="live"), cli)
    check("perms: live bot refused when live trading is off", getattr(live, "status_code", 200) == 403)
    order = server.crypto_order(server.CryptoOrderReq(exchange="binance", symbol="TEST/USDT:USDT", side="buy", order_type="market", stop_loss=98), cli)
    check("perms: Trade-tab order refused when live trading is off", getattr(order, "status_code", 200) == 403)
    a = server.crypto_algo_start(server.CryptoBotReq(strategy="rsi_trend", timeframe="4h", capital_usd=100, mode="demo"), cli)
    b = server.crypto_algo_start(server.CryptoBotReq(strategy="rsi_trend", timeframe="4h", capital_usd=100, mode="demo"), cli)
    check("perms: max running bots enforced", isinstance(a, dict) and a.get("success") and getattr(b, "status_code", 200) == 400)
    rs = server.account_risk_save({"risk_pct": 50, "max_positions": 2}, cli)
    check("risk: client edits own rules, clamped to limits", rs["config"]["risk_pct"] == 3.0 and server._rm.config["max_positions"] == 3)


def test_payment_approval_extends_expiry():
    from datetime import datetime as _dt
    mkt, ex = fresh()
    _mk_users()
    before = _dt.fromisoformat(server._user_record("cli")["expiry"])
    server._save_payment_requests({"r1": {"id": "r1", "username": "cli", "days_requested": 30, "status": "pending",
                                          "submitted_at": _dt.now().isoformat(), "image_file": "x.jpg"}})
    server.admin_approve_payment("r1", {"username": "admin", "role": "admin"})
    after = _dt.fromisoformat(server._user_record("cli")["expiry"])
    check("billing: approval adds days on top of the days left", 29.9 <= (after - before).total_seconds() / 86400 <= 30.1, (before, after))


# ── Admin control panel ───────────────────────────────────────────────────────
def test_admin_panel():
    from datetime import datetime as _dt
    mkt, ex = fresh()
    _mk_users()
    adm = {"username": "admin", "role": "admin"}
    r = server.admin_create_client(server.AdminCreateClientReq(username="newbie", password="short", days=10), adm)
    check("admin: weak password refused on create", getattr(r, "status_code", 200) == 400)
    r = server.admin_create_client(server.AdminCreateClientReq(username="newbie", password="goodpass123", display_name="New B",
                                                               contact="+880", days=10, permissions={"live_trading": False}), adm)
    check("admin: client created", r["success"] and server._perms("newbie")["live_trading"] is False)
    dup = server.admin_create_client(server.AdminCreateClientReq(username="NEWBIE", password="goodpass123"), adm)
    check("admin: duplicate username refused", getattr(dup, "status_code", 200) == 400)
    before = _dt.fromisoformat(server._user_record("newbie")["expiry"])
    server.admin_client_access("newbie", server.AdminAccessReq(days=30, mode="extend"), adm)
    after = _dt.fromisoformat(server._user_record("newbie")["expiry"])
    check("admin: +30 days added on top", 29.9 <= (after - before).total_seconds() / 86400 <= 30.1)
    server.admin_client_access("newbie", server.AdminAccessReq(days=0, mode="set"), adm)
    check("admin: end access now", server._is_client_expired(server._user_record("newbie")))
    server.admin_client_access("newbie", server.AdminAccessReq(days=5, mode="set"), adm)
    r = server.admin_client_permissions("newbie", {"live_trading": True, "max_bots": 99, "evil": True}, adm)
    check("admin: permissions saved, max bots capped, unknown keys ignored",
          r["permissions"]["live_trading"] is True and r["permissions"]["max_bots"] == 50 and "evil" not in r["permissions"])
    tok = server._make_session("newbie", "New B", "client")
    r = server.admin_client_suspend("newbie", {"on": True}, adm)
    check("admin: suspend signs out and halts trading", r["signed_out"] == 1 and server._rm_for("newbie").state["manual_halt"])
    login = server.auth_login(server.AuthLoginRequest(username="newbie", password="goodpass123"))
    check("admin: suspended client cannot log in", getattr(login, "status_code", 200) == 403)
    server.admin_client_suspend("newbie", {"on": False}, adm)
    r = server.admin_client_reset_password("newbie", adm)
    login = server.auth_login(server.AuthLoginRequest(username="newbie", password=r["temporary_password"]))
    check("admin: reset password works and old one stops", isinstance(login, dict) and login.get("token")
          and getattr(server.auth_login(server.AuthLoginRequest(username="newbie", password="goodpass123")), "status_code", 200) == 401)
    b = add_bot("nb1", mode="demo", sym="NB/USDT:USDT"); b["username"] = "newbie"
    d = server.admin_client_delete("newbie", adm)
    check("admin: delete refused while a bot runs", getattr(d, "status_code", 200) == 400)
    server.admin_client_stop_bots("newbie", adm)
    d = server.admin_client_delete("newbie", adm)
    check("admin: client deleted after bots stopped", d.get("success") and server._user_record("newbie") is None and "nb1" not in server._crypto_bots)
    o = server.admin_overview(adm)
    check("admin: overview counts clients", o["clients"]["total"] == 1 and "pending" in o["payments"])
    for route in server.app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/admin/"):
            assert server._require_admin in [x.call for x in route.dependant.dependencies], path
    check("admin: every /api/admin endpoint is owner-only", True)


# ── AI Signal Hunter ──────────────────────────────────────────────────────────
def test_signal_hunter():
    import autopilot as apm
    bar = {"4h": 1_800_000_000_000, "1d": 1_799_971_200_000}
    step = {"4h": 14_400_000, "1d": 86_400_000}
    rows = lambda tf, w=1: [[bar[tf] - (299 - i) * step[tf], 100, 100 + w, 100 - w, 100 + i * 0.01, 1000] for i in range(300)]
    opened = []
    sig_on = {"AAA/USDT:USDT": ("rsi_trend", "BUY"), "BBB/USDT:USDT": ("ema_trend", "SELL"),
              "CCC/USDT:USDT": ("trend_breakout", "BUY"), "WILD/USDT:USDT": ("rsi_trend", "BUY")}
    edge = {"AAA/USDT:USDT": 0.4, "BBB/USDT:USDT": -0.2}
    ap = apm.Autopilot({
        "tickers": lambda: {s: {"quoteVolume": 1e9} for s in ("AAA/USDT:USDT", "BBB/USDT:USDT", "CCC/USDT:USDT", "WILD/USDT:USDT")},
        "ohlcv": lambda sym, tf, n: rows(tf, 20 if sym.startswith("WILD") else 1), "history": lambda s, tf, d: [],
        "backtest": lambda st, r, s, tf, p: {"avg_r": edge.get(s, 0.3), "total_trades": 20, "profit_factor": 1.5},
        "signal": lambda st, sym, tf, p, r: sig_on[sym][1] if sym in sig_on and sig_on[sym][0] == st else None,
        "open": lambda cands, cfg: (opened.extend(cands) or [f"opened {c['symbol']}" for c in cands], []),
    }, path=os.path.join(WORK, "hunt.json"))
    ap.config["enabled"] = True
    check("hunter: trades only the strategies that passed the 2-year research",
          set(ap.config["strategies"]) == {"rsi_trend", "ema_trend", "fibonacci_retracement", "trend_breakout"},
          ap.config["strategies"])
    ap.hunt(apply=True)
    check("hunter: fresh 4h and daily signals with an edge are traded",
          sorted((c["symbol"], c["tf"]) for c in opened) == [("AAA/USDT:USDT", "4h"), ("CCC/USDT:USDT", "1d")], opened)
    check("hunter: signal without an edge on that coin is skipped",
          any(x["symbol"] == "BBB/USDT:USDT" and not x["edge"]["ok"] for x in ap.state["signals"]))
    check("hunter: a coin far more volatile than the tested ones is skipped",
          any(x["symbol"] == "WILD/USDT:USDT" and not x["edge"]["ok"] and "per 4h candle" in x["why"] for x in ap.state["signals"])
          and not any(c["symbol"] == "WILD/USDT:USDT" for c in opened))
    first = len(ap.state["signals"])
    ap.hunt(apply=True)
    check("hunter: the same candle is never traded twice", len(opened) == 2, len(opened))
    check("hunter: a re-scan inside the same candle keeps that candle's signal list",
          len(ap.state["signals"]) == first and "no new candle" in ap.state["log"][-1]["msg"], ap.state["log"][-1])
    bar["4h"] += step["4h"]
    ap.hunt(apply=True)
    check("hunter: next 4h candle is traded, the daily candle that was already handled is not",
          len(opened) == 3 and opened[-1]["symbol"] == "AAA/USDT:USDT", [c["symbol"] for c in opened])
    check("hunter: signal counts only cover the candle that just closed (no old daily signals)",
          ap.state["signals"] and all(x["tf"] == "4h" for x in ap.state["signals"])
          and ap.state["log"][-2]["msg"].startswith("New 4h candle:"), [x["tf"] for x in ap.state["signals"]])
    bar["4h"] = int(time.time() * 1000) - 4 * 3600 * 1000 - 3 * 3600 * 1000     # closed 3h ago
    ap.state["hunted_bar"], ap.state["hunted"] = 0, {}
    ap.hunt(apply=True)
    check("hunter: a candle that closed hours ago is not chased",
          len(opened) == 4 and opened[-1]["tf"] == "1d" and "too late" in " ".join(x["msg"] for x in ap.state["log"][-4:]),
          [(c["symbol"], c["tf"]) for c in opened])


def test_hunter_bots_are_one_shot():
    mkt, ex = fresh()
    server._rm.update_config({"max_positions": 2})
    SIGNAL["v"] = "BUY"; mkt.new_candle(100.0)
    closed_bar = mkt.candles[-2][0]
    cands = [{"symbol": f"H{i}/USDT:USDT", "strategy": "trend_breakout", "tf": "4h", "side": "BUY", "bar": closed_bar,
              "params": {"sl_atr": 2.0, "reward_r": 3.0}, "why": "test", "rank": 50 - i, "momentum": 60} for i in range(3)]
    actions, active = server._autopilot_hunt_open(cands, {"mode": "demo"})
    hunters = [b for b in server._crypto_bots.values() if b.get("one_shot")]
    check("hunter: opens only as many as free slots", len(hunters) == 2 and "no free slot" in actions[-1], actions)
    for b in hunters:
        tick(b["id"])
    check("hunter: one-shot bots enter on their signal candle", all(b["open_side"] == "BUY" for b in hunters))
    b2 = hunters[1]
    mkt.new_candle(b2["open_entry_price"])
    tick(b2["id"])
    check("hunter: a bot holding a trade keeps managing it on the next candle",
          b2["status"] == "active" and b2["open_side"] == "BUY", (b2["status"], b2.get("last_error")))
    b = hunters[0]
    SIGNAL["v"] = None
    mkt.move(b["sl"] - 1, low=b["sl"] - 1)
    tick(b["id"])
    check("hunter: bot stops for good once its trade closes", b["open_side"] is None and b["status"] == "stopped" and b.get("finished"))
    late = dict(cands[2], symbol="LATE/USDT:USDT")
    server._autopilot_hunt_open([late], {"mode": "demo"})
    lb = next(x for x in server._crypto_bots.values() if x.get("symbol") == "LATE/USDT:USDT")
    SIGNAL["v"] = "BUY"; mkt.new_candle(100.0)
    tick(lb["id"])
    check("hunter: a signal that expired before the bot ran is not traded", lb["open_side"] is None and lb["status"] == "stopped")
    server._autopilot_hunt_open([], {"mode": "demo"})
    check("hunter: finished hunters are cleared (trade stays in the ledger)",
          b["id"] not in server._crypto_bots and any(r["bot_id"] == b["id"] for r in server._ledger.all()))


def test_one_click_start():
    mkt, ex = fresh()
    server._autopilot.config["enabled"] = False
    server._autopilot.scan_now = lambda: None
    r = server.autopilot_start(server.AutopilotStartReq(balance=2500, level="safe", mode="demo"), {"username": "admin"})
    check("start: one click sets balance, risk and slots", r.get("success") and server._rm.state["demo_wallet"] == 2500
          and server._rm.config["risk_pct"] == 0.5 and server._rm.config["max_positions"] == 5, r)
    check("start: AI is on in demo with the chosen level", server._autopilot.config["enabled"] and server._autopilot.config["mode"] == "demo"
          and server._autopilot.config["level"] == "safe")
    check("start: shows the expected profit for this balance", abs(r["expect"]["per_day"] - 0.53 * 2.5) < 0.01)
    bad = server.autopilot_start(server.AutopilotStartReq(balance=1000, level="yolo", mode="demo"), {"username": "admin"})
    check("start: unknown risk level refused", getattr(bad, "status_code", 200) == 400)
    bad = server.autopilot_start(server.AutopilotStartReq(balance=1000, level="balanced", mode="live"), {"username": "admin"})
    check("start: live needs explicit confirmation", getattr(bad, "status_code", 200) == 400)
    add_bot("open1", mode="demo")["open_side"] = "BUY"
    bad = server.autopilot_start(server.AutopilotStartReq(balance=900, level="balanced", mode="demo"), {"username": "admin"})
    check("start: under open demo trades the wallet is kept but the level still applies",
          bad.get("success") and server._rm.state["demo_wallet"] == 2500 and server._rm.config["max_positions"] == 8 and bad.get("note"), bad)
    server._autopilot.config["enabled"] = False


def test_trend_filtered_strategies():
    import extra_strategies as xs
    def series(closes, wiggle=0.004):
        return [[i, c, c * (1 + wiggle), c * (1 - wiggle), c, 1000] for i, c in enumerate(closes)]
    up = [100 * (1.003 ** i) for i in range(300)]
    dip = up[:-12] + [up[-13] * (1 - 0.006 * k) for k in range(1, 9)] + [up[-13] * 0.955 * (1 + 0.012 * k) for k in range(1, 5)]
    check("ema_trend: no signal without a fresh cross", xs.ema_trend(series(up)) is None)
    check("rsi_trend: pullback then RSI turn up in an uptrend → BUY", any(xs.rsi_trend(series(dip[:n])) == "BUY" for n in range(290, 301)))
    check("rsi_trend: never SELL in an uptrend", all(xs.rsi_trend(series(dip[:n])) != "SELL" for n in range(230, 301)))
    rows = series(up)
    lo20 = min(r[3] for r in rows[-21:-1])
    rows[-1] = [299, up[-2], up[-2] * 1.004, lo20 * 0.97, up[-2] * 1.003, 3000]
    check("smc_sweep: sweep below the 20-bar low that closes back up in an uptrend → BUY", xs.smc_sweep(rows) == "BUY")
    down = [100 * (0.997 ** i) for i in range(300)]
    rows = series(down)
    rows[-1] = [299, down[-2], down[-2] * 1.004, min(r[3] for r in rows[-21:-1]) * 0.97, down[-2] * 1.003, 3000]
    check("smc_sweep: the same candle in a downtrend is ignored", xs.smc_sweep(rows) is None)
    for name in xs.STRATEGIES:
        check(f"{name}: wired into the bots", server._get_bot_signal({"strategy": name}, series(up)) in (None, "BUY", "SELL")
              and server._bot_lookback_bars({"strategy": name}) == xs.lookback(name))

def test_tested_timeframe_and_hold_days():
    import engine
    check("hold limit: 120 candles on 4h (20 days) and on 1d",
          engine.hold_limit({"timeframe": "4h"}) == 120 and engine.hold_limit({"timeframe": "1d"}) == 120)
    check("hold limit: never under 20 days on short timeframes (15m = 1920 candles, 1h = 480)",
          engine.hold_limit({"timeframe": "15m"}) == 1920 and engine.hold_limit({"timeframe": "1h"}) == 480)
    check("hold limit: a bot's own setting still wins", engine.hold_limit({"timeframe": "15m", "max_hold_bars": 3}) == 3)
    mkt, ex = fresh(mode="demo")
    real_repick, picked = server._ai_pair_repick, []
    server._ai_pair_repick = lambda b: picked.append(b["id"])
    try:
        flat = add_bot("tf1", mode="demo", sym="FLAT/USDT:USDT")
        flat.update(timeframe="15m", ai_pair=True, ai_coins=["FLAT/USDT:USDT", "X/USDT:USDT"])
        moved = server._move_to_tested_timeframe(flat)
        check("a flat rsi_trend bot on 15m is moved to its tested 4h at once",
              moved and flat["timeframe"] == "4h" and flat["ai_coins"] == ["FLAT/USDT:USDT"]
              and flat["last_error"].startswith("🔁"), (flat["timeframe"], flat.get("last_error")))
        other = add_bot("tf2", mode="demo", sym="SMC/USDT:USDT")
        other.update(strategy="smc_sweep", timeframe="15m")
        check("an untested strategy keeps the timeframe the user chose",
              not server._move_to_tested_timeframe(other) and other["timeframe"] == "15m")
        SIGNAL["v"] = "BUY"
        mkt.new_candle(100.0)
        busy = add_bot("tf3", mode="demo", sym="BUSY/USDT:USDT")
        tick("tf3")
        busy.update(timeframe="15m", ai_pair=True)
        server._move_to_tested_timeframe(busy)
        check("a bot with an open trade keeps its timeframe until the trade closes",
              busy["open_side"] == "BUY" and busy["timeframe"] == "15m" and busy.get("next_timeframe") == "4h")
        server._close_position(busy, ex, 100.0, "signal_flip")
        import time as _t; _t.sleep(0.2)
        check("...and moves to 4h right after it closes, with a fresh AI coin pick",
              busy["timeframe"] == "4h" and not busy.get("next_timeframe") and "tf3" in picked,
              (busy["timeframe"], picked))
    finally:
        server._ai_pair_repick = real_repick
        SIGNAL["v"] = None


def test_client_autopilot():
    from datetime import datetime, timedelta
    mkt, ex = fresh(mode="demo")
    data = server._load_users()
    data["users"] = [u for u in data.get("users", []) if u["username"] != "cli"] + [{
        "username": "cli", "role": "client", "salt": "x", "password_hash": "x",
        "expiry": (datetime.now() + timedelta(days=30)).isoformat()}]
    server._save_users(data)
    server._ap_clients.clear()
    server._rm_users.pop("cli", None)
    check("client autopilot: off until the client turns it on", "cli" not in server._ap_followers())
    server._ap_seat("cli")["config"].update(enabled=True, mode="demo")
    check("client autopilot: an enabled client follows the market scan", "cli" in server._ap_followers())
    adm = add_bot("adm", mode="demo", sym="AAA/USDT:USDT")
    adm.update(autopilot=True, one_shot=True)
    go = [{"symbol": "AAA/USDT:USDT", "strategy": "rsi_trend", "tf": "4h", "side": "BUY",
           "params": {"sl_atr": 3.0, "reward_r": 3.0, "tp_atr": 0.0, "trailing_atr": 0.0},
           "bar": 1, "why": "test signal", "rank": 50, "momentum": 70}]
    server._ap_follow(["cli"], go, "Scanned 40 coins: 1 fresh signal(s), 1 with an edge", None)
    mine = [b for b in server._crypto_bots.values() if b.get("username") == "cli"]
    check("client autopilot: the signal becomes a trade bot on the CLIENT's account, next to the owner's",
          len(mine) == 1 and mine[0]["symbol"] == "AAA/USDT:USDT" and mine[0]["one_shot"] and "adm" in server._crypto_bots,
          [(b["id"], b.get("username")) for b in server._crypto_bots.values()])
    log = [x["msg"] for x in server._ap_seat("cli")["log"]]
    check("client autopilot: the client's own log shows the scan and the trade",
          any("Scanned" in m for m in log) and any("BUY AAA" in m for m in log), log)
    snap = server._ap_snapshot_for("cli")
    check("client autopilot: the client sees their own settings and picks, not the owner's",
          snap.get("client") and snap["config"]["enabled"] and len(snap["state"]["picks"]) == 1)
    mine[0]["status"] = "stopped"
    server._autopilot_hunt_open([], {"mode": "demo"})            # the owner's clean-up
    check("client autopilot: the owner's clean-up never removes a client's bots", mine[0]["id"] in server._crypto_bots)
    server._autopilot_hunt_open([], {"mode": "demo"}, owner="cli")
    check("...the client's own clean-up does", mine[0]["id"] not in server._crypto_bots)
    server._rm_for("cli").set_manual_halt(True)
    server._ap_follow(["cli"], go, "Scanned", None)
    check("client autopilot: the client's kill switch blocks their new trades",
          not any(b.get("username") == "cli" for b in server._crypto_bots.values()))
    server._rm_for("cli").set_manual_halt(False)
    data = server._load_users()
    next(u for u in data["users"] if u["username"] == "cli")["permissions"] = {"ai_autopilot": False, "dashboard": False}
    server._save_users(data)
    check("client autopilot: switching the permission off stops it following", "cli" not in server._ap_followers())
    r = server.autopilot_status({"username": "cli", "role": "client"})
    check("client autopilot: the page is refused without the permission", getattr(r, "status_code", 200) == 403)
    server._ap_clients.clear()


def test_symbol_lock_per_account():
    mkt, ex = fresh(mode="demo")
    SIGNAL["v"] = "BUY"
    mkt.new_candle(100.0)
    a = add_bot("own", mode="demo", sym="LCK/USDT:USDT")
    tick("own")
    c = add_bot("cli1", mode="demo", sym="LCK/USDT:USDT")
    c["username"] = "cli"
    tick("cli1")
    check("symbol lock is per account: a client can trade a coin the owner already holds",
          a["open_side"] == "BUY" and c["open_side"] == "BUY", (a["open_side"], c["open_side"], c.get("last_error")))
    b = add_bot("own2", mode="demo", sym="LCK/USDT:USDT")
    tick("own2")
    check("...but one account never opens the same coin twice", b["open_side"] is None and "Symbol lock" in (b.get("last_error") or ""))
    SIGNAL["v"] = None


def test_view_only_signups():
    from datetime import datetime, timedelta
    data = server._load_users()
    data["users"] = [u for u in data.get("users", []) if u["username"] != "newbie"] + [{
        "username": "newbie", "role": "client", "salt": "x", "password_hash": "x",
        "expiry": (datetime.now() + timedelta(days=7)).isoformat(), "permissions": {}}]
    server._save_users(data)
    g = server._client_gate
    check("view-only: a website sign-up can open pages", g("/api/autopilot/status", "GET", "newbie") is None)
    check("view-only: but cannot start bots, the autopilot or trades",
          all("view-only" in (g(pth, "POST", "newbie") or "") for pth in
              ("/api/crypto/algo/start", "/api/autopilot/start", "/api/crypto/order", "/api/indian/algo/start")))
    check("view-only: can still log out, edit the profile and send a payment proof",
          g("/api/auth/logout", "POST", "newbie") is None and g("/api/account/profile", "POST", "newbie") is None
          and g("/api/payment/submit", "POST", "newbie") is None)
    data = server._load_users()
    next(u for u in data["users"] if u["username"] == "newbie")["permissions"] = {"can_trade": True}
    server._save_users(data)
    check("view-only: full access from the admin unlocks actions", g("/api/crypto/algo/start", "POST", "newbie") is None)
    check("view-only: admin-only areas stay closed even with full access", g("/api/admin/overview", "GET", "newbie") == "Forbidden")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\nALL {len(PASS)} CHECKS PASSED")
