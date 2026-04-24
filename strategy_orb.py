"""
strategy_orb.py — Opening Range Breakout (ORB) Scalper

Logic:
  London session:
    1. Capture the range from 07:00-07:30 UTC (first 6 M5 bars)
    2. Entry: first M5 bar that CLOSES outside the range
       LONG : close > range_high, body > 50% of bar range, close > open
       SHORT: close < range_low,  body > 50% of bar range, close < open
    3. Confirmation: H1 EMA(20) must agree with direction
    4. SL: opposite side of opening range (range_low for long, range_high for short)
    5. TP: 2x SL distance (1:2 R:R)

  NY session:
    Same logic but range = 13:00-13:30 UTC

  Filters:
    - Range must be at least 1x ATR(14) wide (real move, not dead market)
    - Range must be at most 4x ATR(14) wide (ignore gap opens)
    - Spread < 20 pts at entry
    - Max 1 trade per session (London + NY = max 2/day)
    - 2% daily loss limit
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_SPREAD_PTS   = 20.0
DAILY_LOSS_LIMIT = 0.02
RR_RATIO         = 2.0
MIN_BODY_RATIO   = 0.5    # bar body must be > 50% of its range
RANGE_MIN_ATR    = 0.8    # opening range must be >= 0.8x ATR
RANGE_MAX_ATR    = 4.0    # opening range must be <= 4x ATR (no gap insanity)
SL_BUFFER_MULT   = 0.1    # SL = range edge + 0.1x ATR buffer

# Session range-build windows (UTC hours, inclusive start)
SESSIONS = {
    "london": {"range_start": 7,  "range_end_min": 30, "trade_until": 11},
    "ny":     {"range_start": 13, "range_end_min": 30, "trade_until": 17},
}


@dataclass
class Signal:
    action:      str
    setup:       str
    entry_price: float
    sl_price:    float
    tp_price:    float
    sl_pips:     float
    rr:          float
    reason:      str

    @property
    def is_trade(self) -> bool:
        return self.action in ('LONG', 'SHORT')


def _hold(reason: str = '') -> Signal:
    return Signal('HOLD', 'hold', 0, 0, 0, 0, 0, reason)


def _make_signal(action: str, setup: str, entry: float, sl: float, reason: str) -> Signal:
    sl_dist = abs(entry - sl)
    if sl_dist < 1e-9:
        return _hold('sl zero')
    tp = entry + sl_dist * RR_RATIO if action == 'LONG' else entry - sl_dist * RR_RATIO
    return Signal(
        action=action, setup=setup,
        entry_price=round(entry, 5),
        sl_price=round(sl, 5),
        tp_price=round(tp, 5),
        sl_pips=round(sl_dist / 0.01, 1),
        rr=RR_RATIO,
        reason=reason,
    )


@dataclass
class _SessionORB:
    """Tracks opening range build and trade state for one session."""
    name:          str
    range_start_h: int   # hour range building starts
    range_end_min: int   # minute within start-hour when range is complete (30 = 07:30)
    trade_until_h: int   # last hour allowed to enter

    range_high:    float = 0.0
    range_low:     float = float('inf')
    range_built:   bool  = False
    fired:         bool  = False   # one trade per session
    _bars_in_range: int  = 0

    def reset(self) -> None:
        self.range_high    = 0.0
        self.range_low     = float('inf')
        self.range_built   = False
        self.fired         = False
        self._bars_in_range = 0

    def feed_bar(self, bar: dict) -> None:
        """Feed an M5 bar during the range-build window."""
        if self.range_built:
            return
        self.range_high = max(self.range_high, float(bar['high']))
        self.range_low  = min(self.range_low,  float(bar['low']))
        self._bars_in_range += 1

    @property
    def range_width(self) -> float:
        if self.range_high <= self.range_low:
            return 0.0
        return self.range_high - self.range_low


@dataclass
class _DayState:
    date:          Optional[date] = None
    trades_today:  int   = 0
    _equity_start: float = 0.0

    def reset_if_new_day(self, d: date, equity: float) -> None:
        if self.date != d:
            self.date          = d
            self.trades_today  = 0
            self._equity_start = equity

    def can_trade(self, equity: float) -> bool:
        if self._equity_start > 0:
            if (self._equity_start - equity) / self._equity_start >= DAILY_LOSS_LIMIT:
                return False
        return True

    def record_trade(self, pnl: float) -> None:
        self.trades_today += 1


class ORBStrategy:
    """
    Opening Range Breakout on M5 bars.
    Call on_bar() on every closed M5 bar.
    Pass h1_bars for trend confirmation.
    """

    def __init__(self) -> None:
        self._day    = _DayState()
        self._london = _SessionORB("london", 7,  30, 11)
        self._ny     = _SessionORB("ny",     13, 30, 17)

    def on_bar(
        self,
        bar:     dict,
        h1_bars: List[dict],
        account: Optional[dict] = None,
    ) -> Signal:
        ts     = bar['time']
        hour   = ts.hour
        minute = ts.minute
        equity = float((account or {}).get('equity', 10_000.0))

        # Day reset
        if self._day.date != ts.date():
            self._day.reset_if_new_day(ts.date(), equity)
            self._london.reset()
            self._ny.reset()

        # Hard filters
        if float(bar.get('spread', 0)) > MAX_SPREAD_PTS:
            return _hold('spread')
        if not self._day.can_trade(equity):
            return _hold('daily loss limit')
        if (account or {}).get('position', 0) != 0:
            return _hold('in trade')

        # H1 EMA(20) trend
        h1_closes = [b['close'] for b in h1_bars]
        h1_trend  = 0
        if len(h1_closes) >= 20:
            k   = 2.0 / 21
            e   = h1_closes[0]
            for v in h1_closes[1:]:
                e = v * k + e * (1 - k)
            h1_trend = 1 if h1_closes[-1] > e else -1

        atr = float(bar.get('atr', 1.0))

        for sess in (self._london, self._ny):
            sig = self._process_session(sess, bar, hour, minute, atr, h1_trend)
            if sig.is_trade:
                return sig

        return _hold('no setup')

    def _process_session(
        self,
        sess:     _SessionORB,
        bar:      dict,
        hour:     int,
        minute:   int,
        atr:      float,
        h1_trend: int,
    ) -> Signal:
        sh = sess.range_start_h

        # ── Range building: from session start until range_end_min ───────────
        if hour == sh and minute < sess.range_end_min:
            sess.feed_bar(bar)
            return _hold(f'{sess.name} building range')

        # ── Mark range complete at first bar after range_end_min ─────────────
        if hour == sh and minute >= sess.range_end_min and not sess.range_built:
            if sess._bars_in_range > 0:
                sess.range_built = True
                rw = sess.range_width
                logger.debug("%s range built: %.2f-%.2f  width=%.2f  atr=%.2f",
                             sess.name, sess.range_low, sess.range_high, rw, atr)
            return _hold(f'{sess.name} range just set')

        # ── Outside range window entirely ─────────────────────────────────────
        if not sess.range_built:
            return _hold(f'{sess.name} range not built')

        if hour >= sess.trade_until_h:
            return _hold(f'{sess.name} session over')

        if sess.fired:
            return _hold(f'{sess.name} already fired')

        # ── Validate range quality ────────────────────────────────────────────
        rw = sess.range_width
        if atr > 0:
            if rw < RANGE_MIN_ATR * atr:
                return _hold(f'{sess.name} range too narrow')
            if rw > RANGE_MAX_ATR * atr:
                return _hold(f'{sess.name} range too wide')

        # ── Breakout detection ────────────────────────────────────────────────
        close  = float(bar['close'])
        open_  = float(bar['open'])
        high   = float(bar['high'])
        low    = float(bar['low'])
        body   = abs(close - open_)
        rng    = high - low
        body_ok = (rng < 1e-9) or (body / rng >= MIN_BODY_RATIO)
        buf    = SL_BUFFER_MULT * atr

        # LONG breakout: close above range_high, bullish candle, H1 not bearish
        if (close > sess.range_high
                and close > open_
                and body_ok
                and h1_trend >= 0):       # allow neutral (0) or bull H1
            sl = sess.range_low - buf     # SL below range low
            sess.fired = True
            return _make_signal('LONG', f'orb_{sess.name}', close, sl,
                f'{sess.name} break up range={rw/0.01:.0f}pts atr={atr/0.01:.0f}pts')

        # SHORT breakout: close below range_low, bearish candle, H1 not bullish
        if (close < sess.range_low
                and close < open_
                and body_ok
                and h1_trend <= 0):
            sl = sess.range_high + buf
            sess.fired = True
            return _make_signal('SHORT', f'orb_{sess.name}', close, sl,
                f'{sess.name} break dn range={rw/0.01:.0f}pts atr={atr/0.01:.0f}pts')

        return _hold(f'{sess.name} no break')

    def record_trade(self, pnl: float) -> None:
        self._day.record_trade(pnl)
