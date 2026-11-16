"""
Trend-filtered strategies (EMA, RSI, SMC liquidity sweep, price action).

The plain versions of these ideas trade against the trend about half the
time and lost money after fees in the 1-year backtest. These versions only
trade in the direction of the higher trend (EMA 200) and only on the candle
the setup completes, so they can be judged on the same footing as the
tested strategies. Every function reads CLOSED candles only.
"""
from __future__ import annotations

from custom_strategy import _atr_series, _ema, _rsi_series


def _cols(ohlcv):
    return ([x[1] for x in ohlcv], [x[2] for x in ohlcv], [x[3] for x in ohlcv],
            [x[4] for x in ohlcv], [x[5] for x in ohlcv])


def ema_trend(ohlcv):
    """EMA 20 crosses EMA 50 in the direction of EMA 200."""
    o, h, l, c, v = _cols(ohlcv)
    if len(c) < 220:
        return None
    e20, e50, e200 = _ema(c, 20), _ema(c, 50), _ema(c, 200)
    if c[-1] > e200[-1] and e20[-2] <= e50[-2] and e20[-1] > e50[-1]:
        return "BUY"
    if c[-1] < e200[-1] and e20[-2] >= e50[-2] and e20[-1] < e50[-1]:
        return "SELL"
    return None


def rsi_trend(ohlcv):
    """Pullback in a trend: price above EMA 200 and RSI(14) turns back up
    through 40 (sell: below EMA 200, RSI turns down through 60)."""
    o, h, l, c, v = _cols(ohlcv)
    if len(c) < 220:
        return None
    e200 = _ema(c, 200)
    r = _rsi_series(c, 14)
    if r[-2] is None:
        return None
    if c[-1] > e200[-1] and r[-2] < 40 <= r[-1]:
        return "BUY"
    if c[-1] < e200[-1] and r[-2] > 60 >= r[-1]:
        return "SELL"
    return None


def smc_sweep(ohlcv):
    """SMC liquidity sweep with the trend: the candle takes out the lowest low
    of the previous 20 candles (sell-side liquidity) but closes back above it,
    while price is above EMA 200 → BUY. Mirror for SELL."""
    o, h, l, c, v = _cols(ohlcv)
    if len(c) < 220:
        return None
    e200 = _ema(c, 200)
    atr = _atr_series(h, l, c, 14)[-1]
    if not atr:
        return None
    lo20, hi20 = min(l[-21:-1]), max(h[-21:-1])
    rng = h[-1] - l[-1]
    if rng <= 0:
        return None
    if c[-1] > e200[-1] and l[-1] < lo20 < c[-1] and (c[-1] - l[-1]) / rng >= 0.6 and rng >= 0.8 * atr:
        return "BUY"
    if c[-1] < e200[-1] and h[-1] > hi20 > c[-1] and (h[-1] - c[-1]) / rng >= 0.6 and rng >= 0.8 * atr:
        return "SELL"
    return None


def pa_engulf(ohlcv):
    """Price action: in an uptrend (EMA 50 above EMA 200, price above EMA 200)
    a bullish engulfing candle after a pullback to the EMA 50 → BUY. Mirror
    for SELL. The engulfing body must be at least half an ATR."""
    o, h, l, c, v = _cols(ohlcv)
    if len(c) < 220:
        return None
    e50, e200 = _ema(c, 50), _ema(c, 200)
    atr = _atr_series(h, l, c, 14)[-1]
    if not atr:
        return None
    body = abs(c[-1] - o[-1])
    bull = c[-1] > o[-1] and c[-2] < o[-2] and c[-1] >= o[-2] and o[-1] <= c[-2]
    bear = c[-1] < o[-1] and c[-2] > o[-2] and c[-1] <= o[-2] and o[-1] >= c[-2]
    touched_lo = min(l[-3:]) <= e50[-1] * 1.005
    touched_hi = max(h[-3:]) >= e50[-1] * 0.995
    if bull and body >= 0.5 * atr and e50[-1] > e200[-1] and c[-1] > e200[-1] and touched_lo:
        return "BUY"
    if bear and body >= 0.5 * atr and e50[-1] < e200[-1] and c[-1] < e200[-1] and touched_hi:
        return "SELL"
    return None


def macd_trend(ohlcv):
    """MACD histogram crosses zero with the EMA 200 trend and ADX-free filter:
    histogram must also be beyond 0 by 0.05 ATR to skip flat crosses."""
    o, h, l, c, v = _cols(ohlcv)
    if len(c) < 220:
        return None
    e200 = _ema(c, 200)
    f, s = _ema(c, 12), _ema(c, 26)
    line = [a - b for a, b in zip(f, s) if a is not None and b is not None]
    sig = _ema(line, 9)
    if len(sig) < 2 or sig[-2] is None:
        return None
    h1, h0 = line[-1] - sig[-1], line[-2] - sig[-2]
    atr = _atr_series(h, l, c, 14)[-1] or 0
    if c[-1] > e200[-1] and h0 <= 0 < h1 and h1 > 0.05 * atr:
        return "BUY"
    if c[-1] < e200[-1] and h0 >= 0 > h1 and -h1 > 0.05 * atr:
        return "SELL"
    return None


STRATEGIES = {
    "ema_trend": ema_trend,
    "rsi_trend": rsi_trend,
    "smc_sweep": smc_sweep,
    "pa_engulf": pa_engulf,
    "macd_trend": macd_trend,
}
LABELS = {
    "ema_trend": "EMA 20/50 cross with the EMA 200 trend",
    "rsi_trend": "RSI pullback in the EMA 200 trend",
    "smc_sweep": "SMC liquidity sweep with the trend",
    "pa_engulf": "Price action: engulfing at EMA 50 in the trend",
    "macd_trend": "MACD zero cross with the EMA 200 trend",
}
LOOKBACK = 260


def signal(name, ohlcv):
    fn = STRATEGIES.get(name)
    return fn(ohlcv) if fn else None
