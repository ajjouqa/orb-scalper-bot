"""
download_dukascopy.py — Download real XAUUSD tick data from Dukascopy

Downloads hourly .bi5 tick files, decodes them, aggregates into M5 OHLCV bars,
and saves as data/XAUUSD_M5_real.csv ready for backtest_orb.py.

Usage:
    python download_dukascopy.py --start 2022-01-01 --end 2023-12-31
    python download_dukascopy.py --start 2023-01-01 --end 2024-12-31 --workers 4

Requirements:
    pip install requests tqdm
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
import lzma

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Dukascopy URL template
# XAUUSD instrument code on Dukascopy
INSTRUMENT   = "XAUUSD"
DUKAS_URL    = "https://datafeed.dukascopy.com/datafeed/{instrument}/{year}/{month:02d}/{day:02d}/{hour:02d}h_ticks.bi5"

# bi5 record format: ms_offset (4B), ask (4B), bid (4B), ask_vol (4B), bid_vol (4B)
# prices are integers: divide by point size
TICK_FMT     = ">IIIff"
TICK_SIZE    = struct.calcsize(TICK_FMT)   # 20 bytes
POINT_SIZE   = 0.001    # XAUUSD: 3 decimal places on Dukascopy (price / 1000 = USD)

_DATA_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CACHE_DIR   = os.path.join(_DATA_DIR, "_duka_cache")


def _url(dt: datetime) -> str:
    # Dukascopy months are 0-indexed
    return DUKAS_URL.format(
        instrument=INSTRUMENT,
        year=dt.year,
        month=dt.month - 1,
        day=dt.day,
        hour=dt.hour,
    )


def _decode_bi5(data: bytes, hour_dt: datetime) -> List[Tuple[datetime, float, float, float, float]]:
    """Decode a .bi5 blob into list of (timestamp, open, high, low, close) — actually ticks."""
    try:
        raw = lzma.decompress(data)
    except Exception as e:
        logger.debug("lzma decompress failed: %s", e)
        return []

    ticks = []
    for i in range(0, len(raw) - TICK_SIZE + 1, TICK_SIZE):
        ms, ask, bid, ask_vol, bid_vol = struct.unpack_from(TICK_FMT, raw, i)
        mid = (ask + bid) / 2.0 * POINT_SIZE
        ts  = hour_dt + timedelta(milliseconds=int(ms))
        ticks.append((ts, mid))
    return ticks


def _download_hour(dt: datetime, session: requests.Session, retries: int = 3) -> List[Tuple]:
    """Download and decode one hour of ticks. Returns [] on failure."""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(_CACHE_DIR, f"{dt.strftime('%Y%m%d_%H')}.pkl")

    if os.path.exists(cache_file):
        try:
            return pd.read_pickle(cache_file)
        except Exception:
            pass

    url = _url(dt)
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 404:
                return []   # no data for this hour (weekend/holiday)
            if r.status_code != 200:
                logger.debug("HTTP %d for %s", r.status_code, url)
                time.sleep(1)
                continue
            ticks = _decode_bi5(r.content, dt)
            if ticks:
                pd.to_pickle(ticks, cache_file)
            return ticks
        except Exception as e:
            logger.debug("Attempt %d failed: %s", attempt + 1, e)
            time.sleep(2 ** attempt)
    return []


def _ticks_to_m5(all_ticks: List[Tuple]) -> pd.DataFrame:
    """Aggregate tick list [(datetime, mid_price), ...] into M5 OHLCV bars."""
    if not all_ticks:
        return pd.DataFrame()

    df = pd.DataFrame(all_ticks, columns=["time", "price"])
    df.set_index("time", inplace=True)
    df.index = pd.DatetimeIndex(df.index, tz="UTC")

    ohlc = df["price"].resample("5min").ohlc()
    ohlc["volume"] = df["price"].resample("5min").count()
    ohlc.dropna(inplace=True)
    return ohlc


def _wilder_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l,
                    (h - c.shift()).abs(),
                    (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def download(
    start:   str = "2022-01-01",
    end:     str = "2023-12-31",
    workers: int = 4,
    out:     Optional[str] = None,
) -> str:
    """
    Download XAUUSD tick data for the date range and save as M5 CSV.
    Returns path to saved CSV.
    """
    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt   = datetime.strptime(end,   "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt   = end_dt.replace(hour=23)

    # Build list of all hours to download
    hours = []
    cur = start_dt
    while cur <= end_dt:
        if cur.weekday() not in (5, 6):  # skip Saturday (5) and most of Sunday (6)
            hours.append(cur)
        cur += timedelta(hours=1)

    logger.info("Downloading %d hours of XAUUSD tick data (%s -> %s) ...",
                len(hours), start, end)
    logger.info("This may take several minutes. Data is cached locally in %s", _CACHE_DIR)

    all_ticks: List[Tuple] = []
    completed = 0

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    def _job(dt):
        return _download_hour(dt, session)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_job, h): h for h in hours}
        for fut in as_completed(futures):
            ticks = fut.result()
            all_ticks.extend(ticks)
            completed += 1
            if completed % 100 == 0 or completed == len(hours):
                logger.info("  Downloaded %d/%d hours, %d ticks so far",
                            completed, len(hours), len(all_ticks))

    if not all_ticks:
        logger.error("No ticks downloaded. Check internet connection.")
        return ""

    logger.info("Total ticks: %d — aggregating to M5 ...", len(all_ticks))
    df = _ticks_to_m5(all_ticks)

    if df.empty:
        logger.error("Aggregation produced empty DataFrame.")
        return ""

    # Add ATR
    df["atr"]    = _wilder_atr(df, 14)
    df["spread"] = 2.0   # will be overridden by realistic simulator

    # Save
    os.makedirs(_DATA_DIR, exist_ok=True)
    if out is None:
        out = os.path.join(_DATA_DIR, f"XAUUSD_M5_real_{start[:4]}_{end[:4]}.csv")

    df.to_csv(out)
    logger.info("Saved %d M5 bars to %s", len(df), out)
    logger.info("Date range: %s -> %s", df.index[0].date(), df.index[-1].date())
    return out


def main():
    p = argparse.ArgumentParser(
        description="Download real XAUUSD M5 data from Dukascopy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--start",   default="2022-01-01", help="Start date YYYY-MM-DD")
    p.add_argument("--end",     default="2023-12-31", help="End date YYYY-MM-DD")
    p.add_argument("--workers", type=int, default=4,  help="Parallel download threads")
    p.add_argument("--out",     default=None,         help="Output CSV path (optional)")
    args = p.parse_args()

    path = download(args.start, args.end, args.workers, args.out)
    if path:
        print(f"\nDownload complete: {path}")
        print(f"Run backtest with:")
        print(f"  python scalper/backtest_orb.py --m5 {path} --equity 1000 --start {args.start} --end {args.end}")


if __name__ == "__main__":
    main()
