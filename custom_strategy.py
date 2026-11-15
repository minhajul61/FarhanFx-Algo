"""
Custom (no-code) strategies for the Strategy Lab.

A strategy is plain JSON, so it can be saved, backtested with the same engine
as the built-in strategies and run by a demo bot:

    {
      "long":  [ {"left": {"type": "ema", "period": 20}, "op": "crosses_above",
                  "right": {"type": "ema", "period": 50}} ],
      "short": [ {"left": {"type": "rsi", "period": 14}, "op": ">", "right": {"type": "value", "value": 70}} ],
      "sl_atr": 2.0,          # stop loss distance in ATR(14)
      "reward_r": 2.0         # take profit = reward_r x stop distance
    }

All conditions on one side must be true (AND) on the last CLOSED candle.
Long wins if both sides fire. Empty side = never trades that direction.
"""
from __future__ import annotations

INDICATORS = {
    # type: (label, needs_period, default_period)
    "close":         ("Close price", False, None),
    "open":          ("Open price", False, None),
    "high":          ("High", False, None),
    "low":           ("Low", False, None),
    "ema":           ("EMA", True, 20),
    "sma":           ("SMA", True, 50),
    "rsi":           ("RSI", True, 14),
    "macd_hist":     ("MACD histogram (12,26,9)", False, None),
    "macd_line":     ("MACD line (12,26)", False, None),
    "macd_signal":   ("MACD signal (9)", False, None),
    "bb_upper":      ("Bollinger upper (2σ)", True, 20),
    "bb_mid":        ("Bollinger middle", True, 20),
    "bb_lower":      ("Bollinger lower (2σ)", True, 20),
    "donchian_high": ("Highest high of previous N", True, 20),
    "donchian_low":  ("Lowest low of previous N", True, 20),
    "atr":           ("ATR", True, 14),
    "adx":           ("ADX", True, 14),
    "volume":        ("Volume", False, None),
    "volume_sma":    ("Volume average", True, 20),
    "value":         ("Number", False, None),
}
OPS = {">": "is above", "<": "is below", "crosses_above": "crosses above", "crosses_below": "crosses below"}
MAX_CONDITIONS = 6
PERIOD_RANGE = (2, 400)


class InvalidStrategy(ValueError):
    pass


def _clean_ind(ind, where):
    if not isinstance(ind, dict) or ind.get("type") not in INDICATORS:
        raise InvalidStrategy(f"{where}: unknown indicator {ind!r}")
    t = ind["type"]
    out = {"type": t}
    if t == "value":
        try:
            out["value"] = float(ind.get("value"))
        except (TypeError, ValueError):
            raise InvalidStrategy(f"{where}: number missing")
    elif INDICATORS[t][1]:
        try:
            p = int(ind.get("period") or INDICATORS[t][2])
        except (TypeError, ValueError):
            raise InvalidStrategy(f"{where}: bad period")
        if not PERIOD_RANGE[0] <= p <= PERIOD_RANGE[1]:
            raise InvalidStrategy(f"{where}: period must be {PERIOD_RANGE[0]}-{PERIOD_RANGE[1]}")
        out["period"] = p
    return out


def validate(defn: dict) -> dict:
    """Return a cleaned copy or raise InvalidStrategy with a readable reason."""
    if not isinstance(defn, dict):
        raise InvalidStrategy("strategy must be an object")
    clean = {"long": [], "short": []}
    for side in ("long", "short"):
        conds = defn.get(side) or []
        if len(conds) > MAX_CONDITIONS:
            raise InvalidStrategy(f"{side}: at most {MAX_CONDITIONS} conditions")
        for i, c in enumerate(conds, 1):
            where = f"{side.upper()} condition {i}"
            if not isinstance(c, dict) or c.get("op") not in OPS:
                raise InvalidStrategy(f"{where}: unknown comparison")
            left = _clean_ind(c.get("left"), where)
            right = _clean_ind(c.get("right"), where)
            if left["type"] == "value":
                raise InvalidStrategy(f"{where}: the left side must be an indicator, not a number")
            clean[side].append({"left": left, "op": c["op"], "right": right})
    if not clean["long"] and not clean["short"]:
        raise InvalidStrategy("add at least one LONG or SHORT condition")
    try:
        clean["sl_atr"] = min(6.0, max(0.5, float(defn.get("sl_atr", 2.0))))
        clean["reward_r"] = min(10.0, max(0.5, float(defn.get("reward_r", 2.0))))
    except (TypeError, ValueError):
        raise InvalidStrategy("stop loss / target must be numbers")
    return clean


def lookback(defn: dict) -> int:
    """Bars of history a signal needs (EMA/RSI warm-up included)."""
    longest = 26
    for side in ("long", "short"):
        for c in defn.get(side, []):
            for ind in (c["left"], c["right"]):
                longest = max(longest, int(ind.get("period") or 0))
    return min(1200, max(150, longest * 3 + 20))


# ── indicator series (whole window; we only read the last two values) ────────
def _ema(xs, n):
    if len(xs) < n:
        return [None] * len(xs)
    out = [None] * (n - 1)
    e = sum(xs[:n]) / n
    out.append(e)
    k = 2 / (n + 1)
    for x in xs[n:]:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def _sma_last2(xs, n):
    if len(xs) < n + 1:
        return None, (sum(xs[-n:]) / n if len(xs) >= n else None)
    return sum(xs[-n - 1:-1]) / n, sum(xs[-n:]) / n


def _std_last2(xs, n):
    def sd(w):
        m = sum(w) / len(w)
        return (sum((v - m) ** 2 for v in w) / len(w)) ** 0.5
    if len(xs) < n + 1:
        return None, None
    return sd(xs[-n - 1:-1]), sd(xs[-n:])


def _rsi_series(xs, n):
    if len(xs) < n + 2:
        return [None] * len(xs)
    out = [None] * n
    gains = [max(0.0, xs[i] - xs[i - 1]) for i in range(1, n + 1)]
    losses = [max(0.0, xs[i - 1] - xs[i]) for i in range(1, n + 1)]
    ag, al = sum(gains) / n, sum(losses) / n
    out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    for i in range(n + 1, len(xs)):
        d = xs[i] - xs[i - 1]
        ag = (ag * (n - 1) + max(0.0, d)) / n
        al = (al * (n - 1) + max(0.0, -d)) / n
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def _atr_series(h, l, c, n):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    if len(tr) < n:
        return [None] * len(tr)
    out = [None] * (n - 1)
    a = sum(tr[:n]) / n
    out.append(a)
    for x in tr[n:]:
        a = (a * (n - 1) + x) / n
        out.append(a)
    return out


def _adx_series_last2(h, l, c, n):
    if len(c) < 2 * n + 2:
        return None, None
    pdm, mdm, tr = [], [], []
    for i in range(1, len(c)):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))

    def smooth(x):
        s = sum(x[:n])
        out = [s]
        for v in x[n:]:
            s = s - s / n + v
            out.append(s)
        return out
    st, sp, sm = smooth(tr), smooth(pdm), smooth(mdm)
    dx = []
    for a, b, t in zip(sp, sm, st):
        pdi, mdi = (100 * a / t, 100 * b / t) if t > 0 else (0, 0)
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
    if len(dx) < n + 1:
        return None, None
    adx = sum(dx[:n]) / n
    vals = [adx]
    for v in dx[n:]:
        adx = (adx * (n - 1) + v) / n
        vals.append(adx)
    return vals[-2], vals[-1]


def _last2(ind, o, h, l, c, v, cache):
    """(previous, current) value of an indicator on the closed candles."""
    t = ind["type"]
    key = (t, ind.get("period"), ind.get("value"))
    if key in cache:
        return cache[key]
    p = ind.get("period")
    if t == "value":
        r = (ind["value"], ind["value"])
    elif t in ("close", "open", "high", "low", "volume"):
        s = {"close": c, "open": o, "high": h, "low": l, "volume": v}[t]
        r = (s[-2], s[-1]) if len(s) >= 2 else (None, None)
    elif t == "ema":
        s = _ema(c, p)
        r = (s[-2], s[-1]) if len(s) >= 2 else (None, None)
    elif t == "sma":
        r = _sma_last2(c, p)
    elif t == "volume_sma":
        r = _sma_last2(v, p)
    elif t == "rsi":
        s = _rsi_series(c, p)
        r = (s[-2], s[-1]) if len(s) >= 2 else (None, None)
    elif t in ("macd_hist", "macd_line", "macd_signal"):
        f, sl = _ema(c, 12), _ema(c, 26)
        line = [a - b for a, b in zip(f, sl) if a is not None and b is not None]
        sig = _ema(line, 9) if len(line) >= 9 else []
        if len(sig) < 2 or sig[-2] is None:
            r = (None, None)
        elif t == "macd_line":
            r = (line[-2], line[-1])
        elif t == "macd_signal":
            r = (sig[-2], sig[-1])
        else:
            r = (line[-2] - sig[-2], line[-1] - sig[-1])
    elif t in ("bb_upper", "bb_mid", "bb_lower"):
        m = _sma_last2(c, p)
        sd = _std_last2(c, p)
        if None in m or None in sd:
            r = (None, None)
        else:
            k = {"bb_upper": 2.0, "bb_mid": 0.0, "bb_lower": -2.0}[t]
            r = (m[0] + k * sd[0], m[1] + k * sd[1])
    elif t == "donchian_high":
        r = (max(h[-p - 2:-2]), max(h[-p - 1:-1])) if len(h) >= p + 2 else (None, None)
    elif t == "donchian_low":
        r = (min(l[-p - 2:-2]), min(l[-p - 1:-1])) if len(l) >= p + 2 else (None, None)
    elif t == "atr":
        s = _atr_series(h, l, c, p)
        r = (s[-2], s[-1]) if len(s) >= 2 else (None, None)
    elif t == "adx":
        r = _adx_series_last2(h, l, c, p)
    else:
        r = (None, None)
    cache[key] = r
    return r


def _check(cond, o, h, l, c, v, cache) -> bool:
    lp, lc = _last2(cond["left"], o, h, l, c, v, cache)
    rp, rc = _last2(cond["right"], o, h, l, c, v, cache)
    if lc is None or rc is None:
        return False
    op = cond["op"]
    if op == ">":
        return lc > rc
    if op == "<":
        return lc < rc
    if lp is None or rp is None:
        return False
    if op == "crosses_above":
        return lp <= rp and lc > rc
    if op == "crosses_below":
        return lp >= rp and lc < rc
    return False


def signal(defn: dict, ohlcv: list):
    """BUY / SELL / None on the last candle of `ohlcv` (closed candles only)."""
    if len(ohlcv) < 30:
        return None
    o = [x[1] for x in ohlcv]
    h = [x[2] for x in ohlcv]
    l = [x[3] for x in ohlcv]
    c = [x[4] for x in ohlcv]
    v = [x[5] for x in ohlcv]
    cache = {}
    if defn.get("long") and all(_check(k, o, h, l, c, v, cache) for k in defn["long"]):
        return "BUY"
    if defn.get("short") and all(_check(k, o, h, l, c, v, cache) for k in defn["short"]):
        return "SELL"
    return None


def describe(defn: dict) -> str:
    def ind(i):
        if i["type"] == "value":
            return f"{i['value']:g}"
        lab = INDICATORS[i["type"]][0]
        return f"{lab}({i['period']})" if i.get("period") else lab

    def side(conds):
        return " AND ".join(f"{ind(c['left'])} {OPS[c['op']]} {ind(c['right'])}" for c in conds) or "—"
    return (f"LONG when {side(defn.get('long', []))}; SHORT when {side(defn.get('short', []))}; "
            f"stop {defn.get('sl_atr', 2):g} ATR, target {defn.get('reward_r', 2):g}R")


TEMPLATES = {
    "ema_cross": {
        "name": "EMA 20/50 crossover",
        "long": [{"left": {"type": "ema", "period": 20}, "op": "crosses_above", "right": {"type": "ema", "period": 50}}],
        "short": [{"left": {"type": "ema", "period": 20}, "op": "crosses_below", "right": {"type": "ema", "period": 50}}],
        "sl_atr": 2.0, "reward_r": 2.0,
    },
    "rsi_trend_pullback": {
        "name": "RSI pullback in trend",
        "long": [{"left": {"type": "close"}, "op": ">", "right": {"type": "ema", "period": 200}},
                 {"left": {"type": "rsi", "period": 14}, "op": "crosses_above", "right": {"type": "value", "value": 35}}],
        "short": [{"left": {"type": "close"}, "op": "<", "right": {"type": "ema", "period": 200}},
                  {"left": {"type": "rsi", "period": 14}, "op": "crosses_below", "right": {"type": "value", "value": 65}}],
        "sl_atr": 1.5, "reward_r": 2.0,
    },
    "donchian_breakout": {
        "name": "20-bar breakout with trend",
        "long": [{"left": {"type": "close"}, "op": ">", "right": {"type": "donchian_high", "period": 20}},
                 {"left": {"type": "close"}, "op": ">", "right": {"type": "ema", "period": 100}}],
        "short": [{"left": {"type": "close"}, "op": "<", "right": {"type": "donchian_low", "period": 20}},
                  {"left": {"type": "close"}, "op": "<", "right": {"type": "ema", "period": 100}}],
        "sl_atr": 2.0, "reward_r": 3.0,
    },
    "bb_bounce": {
        "name": "Bollinger band bounce",
        "long": [{"left": {"type": "close"}, "op": "crosses_above", "right": {"type": "bb_lower", "period": 20}}],
        "short": [{"left": {"type": "close"}, "op": "crosses_below", "right": {"type": "bb_upper", "period": 20}}],
        "sl_atr": 1.5, "reward_r": 1.5,
    },
    "macd_adx": {
        "name": "MACD cross in strong trend",
        "long": [{"left": {"type": "macd_hist"}, "op": "crosses_above", "right": {"type": "value", "value": 0}},
                 {"left": {"type": "adx", "period": 14}, "op": ">", "right": {"type": "value", "value": 25}}],
        "short": [{"left": {"type": "macd_hist"}, "op": "crosses_below", "right": {"type": "value", "value": 0}},
                  {"left": {"type": "adx", "period": 14}, "op": ">", "right": {"type": "value", "value": 25}}],
        "sl_atr": 2.0, "reward_r": 2.0,
    },
}
