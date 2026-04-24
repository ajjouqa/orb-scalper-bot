"""
bot_orb.py — Live ORB Trading Bot

Reads M5 bar + H1 data written by ORBEAT.mq4, runs ORBStrategy,
and writes trade signals back for the EA to execute.

Usage:
    python scalper/bot_orb.py --data-dir "C:/Users/YOU/AppData/Roaming/MetaTrader 4/MQL4/Files"
    python scalper/bot_orb.py --data-dir "C:/MT4/MQL4/Files" --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path: sys.path.insert(0, _THIS_DIR)

from strategy_orb import ORBStrategy, Signal

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DATA_FILE      = "orb_data.json"
SIGNAL_FILE    = "orb_signal.json"
HEARTBEAT_FILE = "orb_heartbeat.json"

MAX_LOT        = 0.50   # hard cap
LOT_PER_100    = 0.01   # $100 equity = 0.01 lot

_running = True


def _sigint(sig, frame):
    global _running
    logger.info("Shutdown requested.")
    _running = False


def _read_data(data_dir: str) -> Optional[dict]:
    path = os.path.join(data_dir, DATA_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to read data file: %s", e)
        return None


def _write_signal(data_dir: str, sig: Signal, lots: float,
                  equity: float, dry_run: bool) -> None:
    payload = {
        "action":    sig.action,
        "lot_size":  round(lots, 2),
        "sl_price":  sig.sl_price,
        "tp_price":  sig.tp_price,
        "comment":   sig.reason[:60],
        "timestamp": datetime.now(timezone.utc).strftime("%Y.%m.%d %H:%M"),
        "equity":    round(equity, 2),
        "setup":     sig.setup,
    }
    if dry_run:
        logger.info("[DRY-RUN] Signal: %s", payload)
        return
    path = os.path.join(data_dir, SIGNAL_FILE)
    tmp  = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)
    logger.info("Signal written: %s lots=%.2f sl=%.2f tp=%.2f",
                sig.action, lots, sig.sl_price, sig.tp_price)


def _write_heartbeat(data_dir: str) -> None:
    path = os.path.join(data_dir, HEARTBEAT_FILE)
    tmp  = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ts": datetime.now(timezone.utc).isoformat(), "alive": True}, f)
    os.replace(tmp, path)


def _parse_bar_time(ts_str: str) -> Optional[datetime]:
    for fmt in ("%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _calc_lots(equity: float) -> float:
    lots = math.floor(equity / 100.0) * LOT_PER_100
    return max(0.01, min(MAX_LOT, round(lots, 2)))


def run(data_dir: str, dry_run: bool = False, poll_sec: float = 2.0) -> None:
    global _running
    signal.signal(signal.SIGINT, _sigint)

    logger.info("ORB Bot started  data_dir=%s  dry_run=%s", data_dir, dry_run)
    if dry_run:
        logger.info("DRY-RUN mode — no real orders will be placed")

    strategy      = ORBStrategy()
    last_bar_time = None
    heartbeat_t   = 0.0

    while _running:
        now = time.time()

        # Heartbeat every 30 sec
        if now - heartbeat_t >= 30:
            try:
                _write_heartbeat(data_dir)
            except Exception as e:
                logger.warning("Heartbeat write failed: %s", e)
            heartbeat_t = now

        # Read data file
        data = _read_data(data_dir)
        if data is None:
            logger.debug("No data file yet — waiting for EA...")
            time.sleep(poll_sec)
            continue

        # Parse bar timestamp
        bar_ts = _parse_bar_time(data.get("bar_time", ""))
        if bar_ts is None:
            time.sleep(poll_sec)
            continue

        # Only process each bar once
        if bar_ts == last_bar_time:
            time.sleep(poll_sec)
            continue
        last_bar_time = bar_ts

        # Extract M5 bar
        m5_raw = data.get("m5", {})
        m5_bar = {
            "time":   bar_ts,
            "open":   float(m5_raw.get("open",   0)),
            "high":   float(m5_raw.get("high",   0)),
            "low":    float(m5_raw.get("low",    0)),
            "close":  float(m5_raw.get("close",  0)),
            "atr":    float(m5_raw.get("atr",    1.0)),
            "volume": float(m5_raw.get("volume", 0)),
            "spread": float(m5_raw.get("spread", 5.0)),
        }

        # Extract H1 bars
        h1_raw: List[dict] = data.get("h1_bars", [])
        h1_bars = [
            {
                "time":  bar_ts,
                "open":  float(b.get("open",  b.get("close", 0))),
                "high":  float(b.get("high",  b.get("close", 0))),
                "low":   float(b.get("low",   b.get("close", 0))),
                "close": float(b.get("close", 0)),
                "atr":   float(b.get("atr",   1.0)),
            }
            for b in h1_raw
        ]

        # Extract H4 bars (optional — EA may or may not send them)
        h4_raw: List[dict] = data.get("h4_bars", [])
        h4_bars = [
            {
                "time":  bar_ts,
                "open":  float(b.get("open",  b.get("close", 0))),
                "high":  float(b.get("high",  b.get("close", 0))),
                "low":   float(b.get("low",   b.get("close", 0))),
                "close": float(b.get("close", 0)),
                "atr":   float(b.get("atr",   1.0)),
            }
            for b in h4_raw
        ] or None

        equity   = float(data.get("equity",   10000.0))
        position = int(data.get("position",   0))

        account = {
            "equity":   equity,
            "position": position,
        }

        logger.info("Bar %s | %02d:%02d | equity=$%.2f | pos=%d | spread=%.1f",
                    bar_ts.strftime("%Y-%m-%d"),
                    bar_ts.hour, bar_ts.minute,
                    equity, position,
                    m5_bar["spread"])

        # Run strategy
        try:
            sig: Signal = strategy.on_bar(m5_bar, h1_bars, h4_bars=h4_bars, account=account)
        except Exception as e:
            logger.error("Strategy error: %s", e, exc_info=True)
            time.sleep(poll_sec)
            continue

        # Log what the strategy sees
        if sig.is_trade:
            lots = _calc_lots(equity)
            logger.info("SIGNAL %s | setup=%s | entry=%.2f sl=%.2f tp=%.2f | lots=%.2f | %s",
                        sig.action, sig.setup,
                        sig.entry_price, sig.sl_price, sig.tp_price,
                        lots, sig.reason)
            _write_signal(data_dir, sig, lots, equity, dry_run)
            strategy.record_trade(0.0)  # placeholder; real PnL comes from EA
        else:
            logger.debug("HOLD | %s", sig.reason)

        time.sleep(poll_sec)

    logger.info("Bot stopped.")


def main():
    p = argparse.ArgumentParser(
        description="ORB Live Trading Bot",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--data-dir",
        default=os.path.join(
            os.path.expanduser("~"),
            "AppData", "Roaming", "MetaTrader 4", "MQL4", "Files"
        ),
        help="Path to MT4 MQL4/Files folder",
    )
    p.add_argument("--dry-run",  action="store_true",
                   help="Print signals but do not write signal file")
    p.add_argument("--poll",     type=float, default=2.0,
                   help="Seconds between data file checks")
    p.add_argument("--verbose",  action="store_true",
                   help="Show HOLD reasons too")
    args = p.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not os.path.isdir(args.data_dir):
        logger.error("data-dir does not exist: %s", args.data_dir)
        logger.error("Pass the correct path with --data-dir")
        sys.exit(1)

    run(args.data_dir, dry_run=args.dry_run, poll_sec=args.poll)


if __name__ == "__main__":
    main()
