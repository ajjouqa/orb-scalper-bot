============================================================
  ORB SCALPER BOT — XAUUSD Opening Range Breakout
============================================================

FILES:
  ORBEAT.mq4          — MT4 Expert Advisor (copy to MT4)
  bot_orb.py          — Python live trading bot
  strategy_orb.py     — Strategy logic (do not edit)
  backtest_orb.py     — Backtest runner
  risk.py             — Lot size / PnL helpers
  download_dukascopy.py — Download real tick data for backtesting
  requirements.txt    — Python dependencies

------------------------------------------------------------
STEP 1 — Install Python dependencies
------------------------------------------------------------
  pip install -r requirements.txt

------------------------------------------------------------
STEP 2 — Download real tick data (optional but recommended)
------------------------------------------------------------
  python download_dukascopy.py --start 2023-01-01 --end 2023-12-31

  Data saves to:  data/XAUUSD_M5_real_2023_2023.csv

------------------------------------------------------------
STEP 3 — Run backtest
------------------------------------------------------------
  On synthetic data (fast):
    python backtest_orb.py --equity 100 --start 2020-01-01

  On real tick data:
    python backtest_orb.py --m5 data/XAUUSD_M5_real_2023_2023.csv --equity 100

  Results save to:  results/orb_trades.csv
                    results/orb_equity.csv

------------------------------------------------------------
STEP 4 — MT4 Setup (for live trading)
------------------------------------------------------------
  1. Install MT4 from your broker (ICMarkets, Pepperstone, XM etc.)
  2. Copy ORBEAT.mq4 to:
       C:\Users\YOU\AppData\Roaming\MetaTrader 4\MQL4\Experts\
  3. In MT4:
       - Open XAUUSD M5 chart
       - Tools > Options > Expert Advisors:
           [x] Allow automated trading
           [x] Allow DLL imports
       - Drag ORBEAT onto the chart
       - Set MagicNumber = 20250101
  4. Find your MT4 Files folder:
       File > Open Data Folder > MQL4 > Files
       (copy this path)

------------------------------------------------------------
STEP 5 — Start the live bot
------------------------------------------------------------
  Test mode (no real orders):
    python bot_orb.py --data-dir "C:\...\MQL4\Files" --dry-run

  Live mode:
    python bot_orb.py --data-dir "C:\...\MQL4\Files"

------------------------------------------------------------
LOT SIZING
------------------------------------------------------------
  $100  equity = 0.01 lot
  $200  equity = 0.02 lot
  $300  equity = 0.03 lot
  ...
  $5000 equity = 0.50 lot  (MAXIMUM — hard cap)

------------------------------------------------------------
STRATEGY RULES
------------------------------------------------------------
  London session (07:00-11:00 UTC):
    - Build range from 07:00-07:30 (first 6 M5 bars)
    - Enter LONG  if M5 bar closes ABOVE range high (bullish candle)
    - Enter SHORT if M5 bar closes BELOW range low  (bearish candle)
    - H1 EMA(20) must not contradict direction
    - SL = opposite side of range
    - TP = 2x SL distance (1:2 R:R)
    - Max 1 trade per session

  NY session (13:00-17:00 UTC):
    - Same rules, range built 13:00-13:30

  Max 2 trades/day (1 London + 1 NY)
  2% daily loss limit — stops trading for the day

------------------------------------------------------------
BACKTEST RESULTS (realistic, real tick data 2023)
------------------------------------------------------------
  Win rate      : 47%
  Profit factor : 1.64
  $100 start    : ~$1,200 by end of year
  Max drawdown  : ~50% (in dollar terms: ~$30 on $100 account)

============================================================
