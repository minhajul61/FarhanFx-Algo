"""
Live-engine tests against a fake Binance — no network, no real orders.

    python tests/test_live_engine.py

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
    return mkt, ex


def add_bot(bid="b1", mode="live", sym="TEST/USDT:USDT", **kw):
    bot = server._strategy_bot_template("trend_breakout", sym, TF, {"sl_atr": 2.0, "reward_r": 3.0, **kw})
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\nALL {len(PASS)} CHECKS PASSED")
