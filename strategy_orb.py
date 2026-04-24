"""
strategy_orb.py — Opening Range Breakout (ORB) Scalper  [Enhanced v2]

Enhancements over v1:
  1. Volume filter     — breakout bar volume > VOL_MULT x 10-bar average
  2. Entry window      — only enter within ENTRY_WINDOW_MIN of range completion
  3. H4 EMA(50) filter — H1 EMA(20) AND H4 EMA(50) must agree on direction
  4. Day-of-week filter — skip Monday (0) and Friday (4); trade Tue/Wed/Thu only
  5. ATR-adaptive TP   — TP = min(RR_MAX * SL_dist, TP_ATR_MULT * daily_ATR)

Logic:
  London session:
    1. Capture the range from 07:00-07:30 UTC (first 6 M5 bars)
    2. Entry: first M5 bar that CLOSES outside range, within 60 min of breakout
       LONG : close > range_high, body > 50%, volume spike, both trends bullish
       SHORT: close < range_low,  body > 50%, volume spike, both trends bearish
    3. SL: opposite side of opening range + ATR buffer
    4. TP: min(3R, 1.5 x daily ATR)

  NY session:
    Same logic but range = 13:00-13:30 UTC

  Max 1 trade per session, 2% daily loss limit
  Skip Monday + Friday
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_SPREAD_PTS   = 20.0
DAILY_LOSS_LIMIT = 0.02
RR_MAX           = 3.0    # max R:R (up from 2.0)
RR_MIN           = 2.0    # min R:R (TP floor)
TP_ATR_MULT      = 1.5    # TP = min(RR_MAX*SL, TP_ATR_MULT * daily_ATR)
MIN_BODY_RATIO   = 0.5
RANGE_MIN_ATR    = 0.8
RANGE_MAX_ATR    = 4.0
SL_BUFFER_MULT   = 0.1
VOL_MULT         = 1.5    # breakout bar volume must be > VOL_MULT * 10-bar avg
VOL_LOOKBACK     = 10
ENTRY_WINDOW_MIN = 60     # only enter within 60 min of range completion
TRADE_DAYS       = {1, 2, 3}   # Tuesday=1, Wednesday=2, Thursday=3 (Mon=0, Fri=4)

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


def _make_signal(action: str, setup: str, entry: float, sl: float,
                 reason: str, daily_atr: float) -> Signal:
    sl_dist = abs(entry - sl)
    if sl_dist < 1e-9:
        return _hold('sl zero')
    tp_2r    = sl_dist * RR_MIN
    tp_3r    = sl_dist * RR_MAX
    tp_atr   = TP_ATR_MULT * daily_atr if daily_atr > 0 else tp_3r
    tp_dist  = min(tp_3r, max(tp_2r, tp_atr))
    rr_actual = tp_dist / sl_dist
    tp = entry + tp_dist if action == 'LONG' else entry - tp_dist
    return Signal(
        action=action, setup=setup,
        entry_price=round(entry, 5),
        sl_price=round(sl, 5),
        tp_price=round(tp, 5),
        sl_pips=round(sl_dist / 0.01, 1),
        rr=round(rr_actual, 2),
        reason=reason,
    )


@dataclass
class _SessionORB:
    name:          str
    range_start_h: int
    range_end_min: int
    trade_until_h: int

    range_high:     float = 0.0
    range_low:      float = float('inf')
    range_built:    bool  = False
    fired:          bool  = False
    range_built_min: int  = -1   # absolute minute of day when range was completed
    _bars_in_range: int   = 0

    def reset(self) -> None:
        self.range_high      = 0.0
        self.range_low       = float('inf')
        self.range_built     = False
        self.fired           = False
        self.range_built_min = -1
        self._bars_in_range  = 0

    def feed_bar(self, bar: dict) -> None:
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
    daily_atr:     float = 0.0   # average ATR for the day (updated from M5 bars)

    def reset_if_new_day(self, d: date, equity: float) -> None:
        if self.date != d:
            self.date          = d
            self.trades_today  = 0
            self._equity_start = equity
            self.daily_atr     = 0.0

    def can_trade(self, equity: float) -> bool:
        if self._equity_start > 0:
            if (self._equity_start - equity) / self._equity_start >= DAILY_LOSS_LIMIT:
                return False
        return True

    def record_trade(self, pnl: float) -> None:
        self.trades_today += 1


class ORBStrategy:
    """
    Enhanced Opening Range Breakout on M5 bars.
    Call on_bar() on every closed M5 bar.

    Args:
        bar      : M5 OHLCV dict with keys time, open, high, low, close, atr, volume, spread
        h1_bars  : list of H1 bar dicts (at least 20 bars for EMA)
        h4_bars  : list of H4 bar dicts (at least 50 bars for EMA)
        account  : dict with 'equity' and 'position' keys
    """

    def __init__(self) -> None:
        self._day    = _DayState()
        self._london = _SessionORB("london", 7,  30, 11)
        self._ny     = _SessionORB("ny",     13, 30, 17)
        self._vol_buf: Deque[float] = deque(maxlen=VOL_LOOKBACK)
        self._atr_buf: Deque[float] = deque(maxlen=48)  # ~4 hours of M5 bars for daily ATR

    def on_bar(
        self,
        bar:     dict,
        h1_bars: List[dict],
        h4_bars: Optional[List[dict]] = None,
        account: Optional[dict] = None,
    ) -> Signal:
        ts     = bar['time']
        hour   = ts.hour
        minute = ts.minute
        equity = float((account or {}).get('equity', 10_000.0))

        # Update volume and ATR buffers
        vol = float(bar.get('volume', 0))
        if vol > 0:
            self._vol_buf.append(vol)
        atr = float(bar.get('atr', 1.0))
        self._atr_buf.append(atr)

        # Day reset
        if self._day.date != ts.date():
            self._day.reset_if_new_day(ts.date(), equity)
            self._london.reset()
            self._ny.reset()

        # Update daily ATR estimate
        if self._atr_buf:
            self._day.daily_atr = sum(self._atr_buf) / len(self._atr_buf)

        # ── Enhancement 4: skip Monday and Friday ────────────────────────────
        if ts.weekday() not in TRADE_DAYS:
            return _hold(f'skip weekday {ts.weekday()}')

        # Hard filters
        if float(bar.get('spread', 0)) > MAX_SPREAD_PTS:
            return _hold('spread')
        if not self._day.can_trade(equity):
            return _hold('daily loss limit')
        if (account or {}).get('position', 0) != 0:
            return _hold('in trade')

        # ── H1 EMA(20) trend ─────────────────────────────────────────────────
        h1_closes = [b['close'] for b in h1_bars]
        h1_trend  = 0
        if len(h1_closes) >= 20:
            k = 2.0 / 21
            e = h1_closes[0]
            for v in h1_closes[1:]:
                e = v * k + e * (1 - k)
            h1_trend = 1 if h1_closes[-1] > e else -1

        # ── Enhancement 3: H4 EMA(50) trend ──────────────────────────────────
        h4_trend = 0
        if h4_bars and len(h4_bars) >= 50:
            h4_closes = [b['close'] for b in h4_bars]
            k = 2.0 / 51
            e = h4_closes[0]
            for v in h4_closes[1:]:
                e = v * k + e * (1 - k)
            h4_trend = 1 if h4_closes[-1] > e else -1

        # Combine trend: if H4 has data, both must agree; if no H4, use H1 only
        if h4_trend != 0:
            combined_trend = h1_trend if h1_trend == h4_trend else 0
        else:
            combined_trend = h1_trend

        for sess in (self._london, self._ny):
            sig = self._process_session(
                sess, bar, hour, minute, atr, combined_trend, vol
            )
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
        trend:    int,
        vol:      float,
    ) -> Signal:
        sh = sess.range_start_h

        # Range building
        if hour == sh and minute < sess.range_end_min:
            sess.feed_bar(bar)
            return _hold(f'{sess.name} building range')

        # Mark range complete
        if hour == sh and minute >= sess.range_end_min and not sess.range_built:
            if sess._bars_in_range > 0:
                sess.range_built     = True
                sess.range_built_min = hour * 60 + minute
                rw = sess.range_width
                logger.debug("%s range built: %.2f-%.2f  width=%.2f  atr=%.2f",
                             sess.name, sess.range_low, sess.range_high, rw, atr)
            return _hold(f'{sess.name} range just set')

        if not sess.range_built:
            return _hold(f'{sess.name} range not built')

        if hour >= sess.trade_until_h:
            return _hold(f'{sess.name} session over')

        if sess.fired:
            return _hold(f'{sess.name} already fired')

        # ── Enhancement 2: entry window filter ───────────────────────────────
        if sess.range_built_min >= 0:
            mins_since_range = (hour * 60 + minute) - sess.range_built_min
            if mins_since_range > ENTRY_WINDOW_MIN:
                return _hold(f'{sess.name} entry window expired ({mins_since_range}min)')

        # Range quality
        rw = sess.range_width
        if atr > 0:
            if rw < RANGE_MIN_ATR * atr:
                return _hold(f'{sess.name} range too narrow')
            if rw > RANGE_MAX_ATR * atr:
                return _hold(f'{sess.name} range too wide')

        # ── Enhancement 1: volume filter ─────────────────────────────────────
        vol_ok = True
        if len(self._vol_buf) >= VOL_LOOKBACK and vol > 0:
            avg_vol = sum(self._vol_buf) / len(self._vol_buf)
            vol_ok  = avg_vol < 1e-9 or vol >= VOL_MULT * avg_vol

        close  = float(bar['close'])
        open_  = float(bar['open'])
        high   = float(bar['high'])
        low    = float(bar['low'])
        body   = abs(close - open_)
        rng    = high - low
        body_ok = (rng < 1e-9) or (body / rng >= MIN_BODY_RATIO)
        buf    = SL_BUFFER_MULT * atr

        daily_atr = self._day.daily_atr if self._day.daily_atr > 0 else atr

        # LONG
        if (close > sess.range_high
                and close > open_
                and body_ok
                and vol_ok
                and trend >= 0):
            sl = sess.range_low - buf
            sess.fired = True
            vol_ratio = (vol / (sum(self._vol_buf)/len(self._vol_buf))
                         if len(self._vol_buf) >= VOL_LOOKBACK and sum(self._vol_buf) > 0
                         else 0.0)
            return _make_signal(
                'LONG', f'orb_{sess.name}', close, sl,
                f'{sess.name} break up range={rw/0.01:.0f}pts vol={vol_ratio:.1f}x',
                daily_atr,
            )

        # SHORT
        if (close < sess.range_low
                and close < open_
                and body_ok
                and vol_ok
                and trend <= 0):
            sl = sess.range_high + buf
            sess.fired = True
            vol_ratio = (vol / (sum(self._vol_buf)/len(self._vol_buf))
                         if len(self._vol_buf) >= VOL_LOOKBACK and sum(self._vol_buf) > 0
                         else 0.0)
            return _make_signal(
                'SHORT', f'orb_{sess.name}', close, sl,
                f'{sess.name} break dn range={rw/0.01:.0f}pts vol={vol_ratio:.1f}x',
                daily_atr,
            )

        reason = f'{sess.name} no break'
        if not vol_ok:
            reason += ' (low vol)'
        if trend == 0:
            reason += ' (no trend agree)'
        return _hold(reason)

    def record_trade(self, pnl: float) -> None:
        self._day.record_trade(pnl)
