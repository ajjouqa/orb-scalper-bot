"""
backtest_orb.py — Realistic backtest for ORBStrategy (Opening Range Breakout)

Usage:
    python scalper/backtest_orb.py
    python scalper/backtest_orb.py --equity 1000 --start 2020-01-01
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from datetime import datetime, timezone, date
from typing import List, Optional, Tuple

_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path: sys.path.insert(0, _THIS_DIR)

import numpy as np
import pandas as pd

from strategy_orb import ORBStrategy, Signal
from risk import calc_pnl, is_sl_hit, is_tp_hit

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)-8s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")

_DATA_DIR = os.path.join(_THIS_DIR, "data")
_RES_DIR  = os.path.join(_THIS_DIR, "results")
_PT       = 0.01   # 1 point = $0.01/oz
MAX_BARS_IN_TRADE = 96   # ~8 hours on M5


def _wilder_atr(df: pd.DataFrame, p: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/p, min_periods=p, adjust=False).mean()


def _load_csv(path: str, label: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path) as f:
        first = f.readline().strip().lower()
    has_header = any(k in first for k in ("open","close","high","low","date","time"))
    if has_header:
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        for cand in ("datetime","time","date","timestamp"):
            if cand in df.columns:
                df.rename(columns={cand:"time"}, inplace=True); break
        df["time"] = pd.to_datetime(df["time"], utc=True, infer_datetime_format=True)
    else:
        raw = pd.read_csv(path, header=None)
        nc = len(raw.columns)
        if nc >= 7:
            raw.columns = ["date","hhmm","open","high","low","close","volume"]+[f"x{i}" for i in range(nc-7)]
            df = raw[["date","hhmm","open","high","low","close","volume"]].copy()
            df["time"] = pd.to_datetime(df["date"]+" "+df["hhmm"].astype(str), format="%Y.%m.%d %H:%M", utc=True)
        else:
            raise ValueError(f"Unrecognised CSV format: {path}")
    df.set_index("time", inplace=True); df.sort_index(inplace=True)
    for c in ("open","high","low","close"): df[c] = pd.to_numeric(df[c], errors="coerce")
    if "volume" not in df.columns: df["volume"] = 0.0
    if "atr"    not in df.columns: df["atr"]    = _wilder_atr(df, 14)
    if "spread" not in df.columns: df["spread"] = 2.0
    df.dropna(subset=["open","high","low","close"], inplace=True)
    logger.info("Loaded %-4s: %d bars  [%s -> %s]", label, len(df), df.index[0].date(), df.index[-1].date())
    return df


def _session_spread(hour: int, minute: int, rng: np.random.Generator) -> float:
    hm = hour*60+minute
    if   420 <= hm < 435:  base = rng.uniform(20.0, 35.0)
    elif 435 <= hm < 660:  base = rng.uniform(5.0,  8.0)
    elif 780 <= hm < 795:  base = rng.uniform(12.0, 22.0)
    elif 795 <= hm < 1020: base = rng.uniform(6.0, 10.0)
    elif hm >= 1260:       base = rng.uniform(30.0, 45.0)
    else:                  base = rng.uniform(10.0, 15.0)
    return round(base * rng.uniform(0.85, 1.15), 2)


def _entry_fill(action: str, price: float, spread: float,
                hour: int, minute: int, atr: float, median_atr: float,
                rng: np.random.Generator) -> Tuple[float, float]:
    if spread > 22.0:
        return math.nan, 0.0
    hm = hour*60+minute
    if   420 <= hm < 435: slip = rng.uniform(2.0, 6.0)
    elif 780 <= hm < 795: slip = rng.uniform(1.5, 4.0)
    elif median_atr > 0 and atr > 3*median_atr: slip = rng.uniform(4.0, 12.0)
    else:                  slip = rng.uniform(0.3, 1.5)
    half = spread / 2.0
    fill = price + (half+slip)*_PT if action=="LONG" else price - (half+slip)*_PT
    return round(fill, 5), round(slip, 3)


def _exit_check(action, sl, tp, lo, hi, atr):
    sh = is_sl_hit(action, lo, hi, sl)
    th = is_tp_hit(action, lo, hi, tp)
    if sh and th: th = False
    if th: return False, True, tp, "TP"
    if sh:
        if action == "LONG":
            ep = lo if (sl-lo) > 2*atr else sl
            return True, False, ep, ("SL_GAP" if (sl-lo)>2*atr else "SL")
        else:
            ep = hi if (hi-sl) > 2*atr else sl
            return True, False, ep, ("SL_GAP" if (hi-sl)>2*atr else "SL")
    return False, False, 0.0, ""


def run_backtest(df_m5, df_h1, equity0=1000.0, start=None, end=None,
                 seed=42, commission=3.0):
    if start: df_m5 = df_m5[df_m5.index >= pd.Timestamp(start, tz="UTC")]
    if end:   df_m5 = df_m5[df_m5.index <= pd.Timestamp(end,   tz="UTC")]
    if len(df_m5) == 0: raise ValueError("No bars in range")

    rng        = np.random.default_rng(seed)
    median_atr = float(df_m5["atr"].median())
    strategy   = ORBStrategy()
    equity     = float(equity0)
    peak_eq    = float(equity0)

    df_h1_ri   = df_h1.reindex(df_m5.index, method="ffill")
    H1_WIN     = 60
    h1_history: List[dict] = []
    prev_h1_close = None

    trade_dir  = None
    t_entry    = t_sl = t_tp = t_lots = t_spread = t_slip = 0.0
    t_sig      = 0.0
    t_open_ts  = None
    t_bars     = 0

    cur_day    = None
    day_eq_start = float(equity0)
    day_halted = False

    trades       : List[dict] = []
    equity_curve : List[dict] = []
    total = len(df_m5)
    log_n = max(1, total // 20)

    logger.info("ORB backtest: %d bars, $%.0f equity", total, equity0)

    for i, (ts, row) in enumerate(df_m5.iterrows()):
        if i % log_n == 0:
            logger.info("  %d/%d (%.0f%%)  equity=$%.2f  trades=%d",
                        i, total, 100*i/total, equity, len(trades))

        bt = ts.to_pydatetime()
        if bt.tzinfo is None: bt = bt.replace(tzinfo=timezone.utc)

        m5 = {
            "time":   bt,
            "open":   float(row["open"]),
            "high":   float(row["high"]),
            "low":    float(row["low"]),
            "close":  float(row["close"]),
            "atr":    float(row.get("atr", 1.0)),
            "volume": float(row.get("volume", 0.0)),
            "spread": float(row.get("spread", 2.0)),
        }
        atr = m5["atr"]

        # Adaptive median ATR
        if i > 0 and i % 500 == 0:
            median_atr = float(df_m5["atr"].iloc[max(0,i-500):i].median())

        # H1 window
        h1c = float(df_h1_ri.iloc[i]["close"])
        if h1c != prev_h1_close:
            hr = df_h1_ri.iloc[i]
            h1_history.append({"close": h1c, "high": float(hr.get("high",h1c)),
                                "low": float(hr.get("low",h1c)), "atr": float(hr.get("atr",1.0))})
            if len(h1_history) > H1_WIN: h1_history.pop(0)
            prev_h1_close = h1c

        # Daily reset (let strategy.on_bar handle its own session reset via _day.date check)
        bd = bt.date()
        if cur_day != bd:
            cur_day = bd; day_eq_start = equity; day_halted = False

        # Manage open trade
        if trade_dir is not None:
            t_bars += 1
            sh, th, ep, lbl = _exit_check(trade_dir, t_sl, t_tp, m5["low"], m5["high"], atr)
            timed = not sh and not th and t_bars >= MAX_BARS_IN_TRADE
            if sh or th or timed:
                if timed: ep = m5["close"]; lbl = "TIMEOUT"
                gross = calc_pnl(trade_dir, t_entry, ep, t_lots)
                slip_usd   = t_slip  * t_lots * 100 * _PT
                spread_usd = t_spread* t_lots * 100 * _PT
                comm_usd   = commission * t_lots * 2
                net = gross - slip_usd - spread_usd - comm_usd
                equity += net
                if equity > peak_eq: peak_eq = equity
                strategy.record_trade(net)
                trades.append({
                    "open_time":  t_open_ts.isoformat() if t_open_ts else "",
                    "close_time": ts.isoformat(),
                    "direction":  trade_dir,
                    "entry_fill": round(t_entry,5),
                    "exit_fill":  round(ep,5),
                    "sl":  round(t_sl,5), "tp": round(t_tp,5),
                    "lots": round(t_lots,2),
                    "gross_pnl": round(gross,2), "net_pnl": round(net,2),
                    "spread_pts": round(t_spread,1), "slip_pts": round(t_slip,2),
                    "result": lbl, "hour": bt.hour,
                    "year": bt.year,
                })
                logger.debug("CLOSE %s %s entry=%.2f exit=%.2f net=$%.2f eq=$%.2f",
                             lbl, trade_dir, t_entry, ep, net, equity)
                trade_dir = None

        # Daily circuit breaker
        if day_eq_start > 0 and (day_eq_start-equity)/day_eq_start >= 0.02:
            day_halted = True

        # Signal
        if trade_dir is None and not day_halted:
            acc = {"equity": equity, "position": 0}
            sig: Signal = strategy.on_bar(m5, list(h1_history), account=acc)

            if sig.is_trade:
                spread = _session_spread(bt.hour, bt.minute, rng)
                fill, slip = _entry_fill(sig.action, sig.entry_price, spread,
                                         bt.hour, bt.minute, atr, median_atr, rng)
                if not math.isnan(fill):
                    sl_dist = abs(sig.entry_price - sig.sl_price)
                    tp_dist = abs(sig.tp_price    - sig.entry_price)
                    # $100 = 0.01 lot, capped at 0.50 lots max
                    lots = max(0.01, min(0.50, round(math.floor(equity / 100.0) * 0.01, 2)))
                    trade_dir  = sig.action
                    t_entry    = fill
                    t_sig      = sig.entry_price
                    t_sl       = fill - sl_dist if sig.action=="LONG" else fill + sl_dist
                    t_tp       = fill + tp_dist if sig.action=="LONG" else fill - tp_dist
                    t_lots     = lots
                    t_spread   = spread
                    t_slip     = slip
                    t_bars     = 0
                    t_open_ts  = ts
                    logger.debug("ENTRY %s %s fill=%.2f sl=%.2f tp=%.2f lots=%.2f",
                                 sig.action, sig.setup, fill, t_sl, t_tp, lots)

        equity_curve.append({"time": ts.isoformat(), "equity": round(equity,4)})

    # Force close
    if trade_dir is not None:
        lc = float(df_m5.iloc[-1]["close"])
        gross = calc_pnl(trade_dir, t_entry, lc, t_lots)
        net   = gross - t_spread*t_lots*100*_PT - commission*t_lots*2
        equity += net
        trades.append({"open_time": t_open_ts.isoformat() if t_open_ts else "",
                       "close_time": df_m5.index[-1].isoformat(),
                       "direction": trade_dir, "entry_fill": t_entry,
                       "exit_fill": lc, "gross_pnl": round(gross,2),
                       "net_pnl": round(net,2), "result": "OPEN@END",
                       "lots": t_lots, "year": df_m5.index[-1].year})

    return _stats(trades, equity_curve, equity0, equity, df_m5.index[0], df_m5.index[-1])


def _stats(trades, ec, eq0, eq_f, ts0, ts1):
    n = len(trades)
    if n == 0:
        logger.warning("No trades generated.")
        return {"n_trades": 0, "final_equity": round(eq_f,2)}

    wins = [t for t in trades if t.get("net_pnl",0) > 0]
    loss = [t for t in trades if t.get("net_pnl",0) <= 0]
    gw = sum(t["net_pnl"] for t in wins)
    gl = abs(sum(t["net_pnl"] for t in loss))
    pf = gw/gl if gl > 0 else float("inf")
    wr = len(wins)/n
    ret = (eq_f - eq0)/eq0*100

    eqs  = [e["equity"] for e in ec]
    pk   = eq0; mx = 0.0
    for e in eqs:
        if e > pk: pk = e
        d = (pk-e)/pk*100 if pk>0 else 0
        if d > mx: mx = d

    idx   = pd.to_datetime([e["time"] for e in ec], utc=True)
    eqs_s = pd.Series(eqs, index=idx)
    daily = eqs_s.resample("D").last().ffill().pct_change().dropna()
    sharpe = float(daily.mean()/daily.std()*math.sqrt(252)) if len(daily)>1 and daily.std()>0 else 0.0

    gross_t = sum(t.get("gross_pnl",0) for t in trades)
    net_t   = sum(t.get("net_pnl",0)   for t in trades)
    costs   = gross_t - net_t
    drag    = costs/abs(gross_t)*100 if abs(gross_t)>1e-9 else 0.0

    by_r: dict = {}
    for t in trades:
        r = t.get("result","?")
        if r not in by_r: by_r[r] = {"n":0,"wins":0}
        by_r[r]["n"] += 1
        if t.get("net_pnl",0)>0: by_r[r]["wins"] += 1

    by_yr: dict = {}
    for t in trades:
        y = t.get("year", 0)
        if y not in by_yr: by_yr[y] = {"n":0,"wins":0,"net":0.0}
        by_yr[y]["n"] += 1
        if t.get("net_pnl",0)>0: by_yr[y]["wins"] += 1
        by_yr[y]["net"] += t.get("net_pnl",0)

    s = {
        "period": f"{ts0.date()} -> {ts1.date()}",
        "n_trades": n, "win_rate": round(wr*100,1),
        "profit_factor": round(pf,2), "total_return": round(ret,2),
        "initial_equity": eq0, "final_equity": round(eq_f,2),
        "max_drawdown": round(mx,2), "sharpe": round(sharpe,2),
        "gross_pnl": round(gross_t,2), "net_pnl": round(net_t,2),
        "total_costs": round(costs,2), "cost_drag_pct": round(drag,1),
        "avg_trade_pnl": round(net_t/n,2),
        "by_result": by_r, "by_year": by_yr,
    }
    _print_tearsheet(s)
    os.makedirs(_RES_DIR, exist_ok=True)
    pd.DataFrame(trades).to_csv(os.path.join(_RES_DIR,"orb_trades.csv"), index=False)
    pd.DataFrame(ec).to_csv(os.path.join(_RES_DIR,"orb_equity.csv"), index=False)
    logger.info("Saved to %s", _RES_DIR)
    return s


def _print_tearsheet(s):
    SEP = "="*58
    rs  = ("+" if s["total_return"]>=0 else "") + f"{s['total_return']:.1f}%"
    print(SEP)
    print("  ORB Scalper  --  REALISTIC Backtest")
    print("  (spread + slippage + commission + gap-through SL)")
    print(SEP)
    print(f"  Period         : {s['period']}")
    print(f"  Initial equity : ${s['initial_equity']:.2f}")
    print(f"  Final equity   : ${s['final_equity']:.2f}")
    print(f"  Total return   : {rs}")
    print()
    print(f"  Trades         : {s['n_trades']}")
    print(f"  Win rate       : {s['win_rate']:.1f}%")
    print(f"  Profit factor  : {s['profit_factor']:.2f}")
    print(f"  Avg trade PnL  : ${s['avg_trade_pnl']:.2f}")
    print()
    print(f"  Max drawdown   : {s['max_drawdown']:.1f}%")
    print(f"  Sharpe ratio   : {s['sharpe']:.2f}")
    print()
    print(f"  Gross PnL      : ${s['gross_pnl']:.2f}")
    print(f"  Total costs    : ${s['total_costs']:.2f}")
    print(f"  Net PnL        : ${s['net_pnl']:.2f}")
    print(f"  Cost drag      : {s['cost_drag_pct']:.1f}%")
    print()
    print("  Result breakdown:")
    for r, v in sorted(s["by_result"].items()):
        wr = v["wins"]/v["n"]*100 if v["n"]>0 else 0
        print(f"    {r:<12}: {v['n']:>4} trades  {wr:>5.1f}% WR")
    print()
    print("  Per-year breakdown:")
    for y in sorted(s["by_year"]):
        v  = s["by_year"][y]
        wr = v["wins"]/v["n"]*100 if v["n"]>0 else 0
        print(f"    {y}: {v['n']:>4} trades  {wr:>5.1f}% WR  net=${v['net']:>8.2f}")
    print(SEP)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--m5",    default=os.path.join(_DATA_DIR,"XAUUSD_M5.csv"))
    p.add_argument("--h1",    default=os.path.join(_DATA_DIR,"XAUUSD_H1.csv"))
    p.add_argument("--equity",     type=float, default=1000.0)
    p.add_argument("--start",      default="2020-01-01")
    p.add_argument("--end",        default=None)
    p.add_argument("--commission", type=float, default=3.0)
    p.add_argument("--seed",       type=int,   default=42)
    args = p.parse_args()
    df_m5 = _load_csv(args.m5, "M5")
    df_h1 = _load_csv(args.h1, "H1")
    run_backtest(df_m5, df_h1, equity0=args.equity, start=args.start,
                 end=args.end, seed=args.seed, commission=args.commission)

if __name__ == "__main__":
    main()
