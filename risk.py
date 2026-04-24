"""
risk.py — Position sizing and trade management for the scalping bot.

Provides pure functions — no state, no file I/O.
All price/PnL values are in USD for XAUUSD standard lots.
"""

from __future__ import annotations

# ── XAUUSD contract spec ──────────────────────────────────────────────────────
LOT_OZ     = 100.0   # 1 standard lot = 100 oz gold
POINT_SIZE = 0.01    # 1 point = $0.01 per oz
MIN_LOT    = 0.01
MAX_LOT    = 5.0


def calc_lot_size(equity: float, risk_fraction: float, sl_points: float) -> float:
    """
    Compute lot size that risks `risk_fraction` of equity for a given SL distance.

    Parameters
    ----------
    equity        : account equity in USD
    risk_fraction : e.g. 0.01 for 1%
    sl_points     : SL distance in XAUUSD points (price units * 100)

    Returns
    -------
    Lot size clipped to [MIN_LOT, MAX_LOT], rounded to 0.01.
    """
    if equity <= 0 or risk_fraction <= 0 or sl_points <= 0:
        return MIN_LOT

    risk_usd    = equity * risk_fraction
    usd_per_lot = sl_points * POINT_SIZE * LOT_OZ   # dollars risked per lot
    if usd_per_lot <= 0:
        return MIN_LOT

    lot = risk_usd / usd_per_lot
    lot = max(MIN_LOT, min(MAX_LOT, lot))
    return round(lot, 2)


def calc_pnl(direction: str, entry: float, exit_price: float, lot_size: float) -> float:
    """
    Calculate realised PnL for a completed trade.

    Parameters
    ----------
    direction  : 'LONG' or 'SHORT'
    entry      : entry price (USD/oz)
    exit_price : exit price (USD/oz)
    lot_size   : number of standard lots

    Returns
    -------
    PnL in USD (negative for losses).
    """
    if direction == 'LONG':
        return (exit_price - entry) * lot_size * LOT_OZ
    elif direction == 'SHORT':
        return (entry - exit_price) * lot_size * LOT_OZ
    else:
        raise ValueError(f"direction must be 'LONG' or 'SHORT', got {direction!r}")


def trailing_stop(
    direction:    str,
    entry:        float,
    current_price: float,
    original_sl:  float,
    atr:          float,
    trail_atr:    float = 1.0,
) -> float:
    """
    Compute a trailing stop price.

    Activates once the trade is at least +1 R in profit (i.e. the trade has
    moved at least the original SL distance in the trade's favour).
    Once active, trails `trail_atr` * ATR behind current price.
    The stop is never moved against the trade.

    Parameters
    ----------
    direction    : 'LONG' or 'SHORT'
    entry        : trade entry price
    current_price: current market price
    original_sl  : original stop loss price
    atr          : current ATR value
    trail_atr    : ATR multiple for trailing distance (default 1.0)

    Returns
    -------
    New stop-loss price (may be equal to original_sl if trail not yet active).
    """
    sl_dist = abs(entry - original_sl)

    if direction == 'LONG':
        # Check +1R
        if current_price < entry + sl_dist:
            return original_sl   # not yet in profit enough to trail
        new_sl = current_price - trail_atr * atr
        # Never move stop below original
        return max(original_sl, new_sl)

    elif direction == 'SHORT':
        # Check +1R
        if current_price > entry - sl_dist:
            return original_sl
        new_sl = current_price + trail_atr * atr
        # Never move stop above original
        return min(original_sl, new_sl)

    else:
        raise ValueError(f"direction must be 'LONG' or 'SHORT', got {direction!r}")


def is_sl_hit(direction: str, bar_low: float, bar_high: float, sl_price: float) -> bool:
    """
    Check whether a bar's range touched or crossed the stop-loss level.

    Parameters
    ----------
    direction : 'LONG' or 'SHORT'
    bar_low   : bar's low price
    bar_high  : bar's high price
    sl_price  : current stop-loss price

    Returns
    -------
    True if the stop was hit during this bar.
    """
    if direction == 'LONG':
        return bar_low <= sl_price
    elif direction == 'SHORT':
        return bar_high >= sl_price
    else:
        raise ValueError(f"direction must be 'LONG' or 'SHORT', got {direction!r}")


def is_tp_hit(direction: str, bar_low: float, bar_high: float, tp_price: float) -> bool:
    """
    Check whether a bar's range reached the take-profit level.

    Parameters
    ----------
    direction : 'LONG' or 'SHORT'
    bar_low   : bar's low price
    bar_high  : bar's high price
    tp_price  : take-profit price

    Returns
    -------
    True if the target was hit during this bar.
    """
    if direction == 'LONG':
        return bar_high >= tp_price
    elif direction == 'SHORT':
        return bar_low <= tp_price
    else:
        raise ValueError(f"direction must be 'LONG' or 'SHORT', got {direction!r}")
