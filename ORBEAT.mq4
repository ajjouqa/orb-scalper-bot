//+------------------------------------------------------------------+
//|  ORBEAT.mq4 — ORB Strategy MT4 Bridge                           |
//|                                                                  |
//|  Setup:                                                          |
//|    1. Copy to MT4/MQL4/Experts/                                  |
//|    2. Attach to XAUUSD M5 chart                                  |
//|    3. Enable "Allow DLL imports" and "Allow live trading"        |
//|    4. Run: python scalper/bot_orb.py                             |
//+------------------------------------------------------------------+

#property copyright "ORB Scalper"
#property version   "1.00"
#property strict

input int    MagicNumber   = 20250101;
input int    Slippage      = 5;        // points
input bool   EnableTrading = true;
input bool   VerboseLog    = false;

string DATA_FILE      = "orb_data.json";
string SIGNAL_FILE    = "orb_signal.json";
string HEARTBEAT_FILE = "orb_heartbeat.json";

datetime LastBarTime    = 0;
datetime LastSignalTime = 0;
int      TickCount      = 0;
datetime LastHBCheck    = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   if(Symbol() != "XAUUSD")  { Alert("Attach to XAUUSD!"); return INIT_FAILED; }
   if(Period()  != PERIOD_M5) { Alert("Attach to M5!");     return INIT_FAILED; }
   Print("ORBEAT ready. Magic=", MagicNumber, " Trading=", EnableTrading);
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   TickCount++;

   // Write bar data on every new M5 bar
   datetime curBar = iTime(Symbol(), PERIOD_M5, 0);
   if(curBar != LastBarTime)
     {
      LastBarTime = curBar;
      WriteData();
     }

   // Read signal every 5 ticks (~1-2 sec)
   if(TickCount % 5 == 0)
      ReadSignal();

   // Heartbeat check every 90 sec
   if(TimeCurrent() - LastHBCheck > 90)
     {
      CheckHeartbeat();
      LastHBCheck = TimeCurrent();
     }
  }

//+------------------------------------------------------------------+
void WriteData()
  {
   // Last completed M5 bar (index 1)
   double m5_o = iOpen (Symbol(), PERIOD_M5, 1);
   double m5_h = iHigh (Symbol(), PERIOD_M5, 1);
   double m5_l = iLow  (Symbol(), PERIOD_M5, 1);
   double m5_c = iClose(Symbol(), PERIOD_M5, 1);
   long   m5_v = iVolume(Symbol(), PERIOD_M5, 1);
   double atr  = iATR(Symbol(), PERIOD_M5, 14, 1);

   // Last 6 H1 bars for trend
   string h1_arr = "[";
   for(int i = 60; i >= 1; i--)
     {
      if(i < 60) h1_arr += ",";
      h1_arr += "{";
      h1_arr += "\"open\":"  + DoubleToStr(iOpen (Symbol(), PERIOD_H1, i), 2) + ",";
      h1_arr += "\"high\":"  + DoubleToStr(iHigh (Symbol(), PERIOD_H1, i), 2) + ",";
      h1_arr += "\"low\":"   + DoubleToStr(iLow  (Symbol(), PERIOD_H1, i), 2) + ",";
      h1_arr += "\"close\":" + DoubleToStr(iClose(Symbol(), PERIOD_H1, i), 2) + ",";
      h1_arr += "\"atr\":"   + DoubleToStr(iATR(Symbol(), PERIOD_H1, 14, i), 5);
      h1_arr += "}";
     }
   h1_arr += "]";

   double spread = (Ask - Bid) / Point;
   double equity = AccountEquity();
   double balance= AccountBalance();

   int    pos    = 0;
   double p_lots = 0, p_sl = 0, p_tp = 0, p_pnl = 0;
   for(int i = 0; i < OrdersTotal(); i++)
     {
      if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         if(OrderSymbol() == Symbol() && OrderMagicNumber() == MagicNumber)
           {
            pos    = (OrderType() == OP_BUY) ? 1 : -1;
            p_lots = OrderLots();
            p_sl   = OrderStopLoss();
            p_tp   = OrderTakeProfit();
            p_pnl  = OrderProfit() + OrderSwap() + OrderCommission();
            break;
           }
     }

   string ts = TimeToStr(iTime(Symbol(), PERIOD_M5, 1), TIME_DATE | TIME_MINUTES);

   string j = "{";
   j += "\"bar_time\":\"" + ts + "\",";
   j += "\"m5\":{\"open\":"  + DoubleToStr(m5_o,2) +
              ",\"high\":"   + DoubleToStr(m5_h,2) +
              ",\"low\":"    + DoubleToStr(m5_l,2) +
              ",\"close\":"  + DoubleToStr(m5_c,2) +
              ",\"volume\":" + IntegerToString((int)m5_v) +
              ",\"atr\":"    + DoubleToStr(atr,5) +
              ",\"spread\":" + DoubleToStr(spread,1) + "},";
   j += "\"h1_bars\":" + h1_arr + ",";
   j += "\"equity\":"   + DoubleToStr(equity, 2) + ",";
   j += "\"balance\":"  + DoubleToStr(balance,2) + ",";
   j += "\"position\":" + IntegerToString(pos)    + ",";
   j += "\"pos_lots\":" + DoubleToStr(p_lots,2)   + ",";
   j += "\"pos_sl\":"   + DoubleToStr(p_sl,2)     + ",";
   j += "\"pos_tp\":"   + DoubleToStr(p_tp,2)     + ",";
   j += "\"daily_pnl\":" + DoubleToStr(p_pnl,2);
   j += "}";

   string tmp = DATA_FILE + ".tmp";
   int fh = FileOpen(tmp, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE) { Print("ORBEAT: cannot write data: ", GetLastError()); return; }
   FileWriteString(fh, j);
   FileClose(fh);
   if(FileIsExist(DATA_FILE)) FileDelete(DATA_FILE);
   FileMove(tmp, 0, DATA_FILE, 0);

   if(VerboseLog) Print("ORBEAT: data written bar=", ts, " spread=", spread);
  }

//+------------------------------------------------------------------+
void ReadSignal()
  {
   if(!FileIsExist(SIGNAL_FILE)) return;

   int fh = FileOpen(SIGNAL_FILE, FILE_READ | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE) return;
   string raw = "";
   while(!FileIsEnding(fh)) raw += FileReadString(fh);
   FileClose(fh);

   if(StringLen(raw) < 10) return;

   string action   = JStr(raw, "action");
   string ts_str   = JStr(raw, "timestamp");
   double lots     = JDbl(raw, "lot_size");
   double sl_price = JDbl(raw, "sl_price");
   double tp_price = JDbl(raw, "tp_price");
   string comment  = JStr(raw, "comment");

   if(action == "" || action == "HOLD") return;

   datetime sig_time = StringToTime(ts_str);
   if(sig_time <= LastSignalTime) return;
   LastSignalTime = sig_time;

   Print("ORBEAT: signal  action=", action, " lots=", lots,
         " sl=", sl_price, " tp=", tp_price);

   if(!EnableTrading) { Print("ORBEAT: trading disabled."); return; }

   if     (action == "LONG")  DoBuy (lots, sl_price, tp_price, comment);
   else if(action == "SHORT") DoSell(lots, sl_price, tp_price, comment);
   else if(action == "CLOSE") CloseAll();
  }

//+------------------------------------------------------------------+
void DoBuy(double lots, double sl, double tp, string cmt)
  {
   if(HasTrade()) { Print("ORBEAT: already in trade, skip BUY"); return; }
   lots = NormLots(lots);
   double px = Ask;
   if(sl <= 0 || sl >= px) sl = px - 300 * Point;
   if(tp <= px)            tp = px + 600 * Point;
   int t = OrderSend(Symbol(), OP_BUY, lots, px, Slippage, sl, tp, cmt, MagicNumber, 0, clrGreen);
   if(t < 0) Print("ORBEAT: BUY failed err=", GetLastError());
   else       Print("ORBEAT: BUY #", t, " px=", px, " sl=", sl, " tp=", tp, " lots=", lots);
  }

void DoSell(double lots, double sl, double tp, string cmt)
  {
   if(HasTrade()) { Print("ORBEAT: already in trade, skip SELL"); return; }
   lots = NormLots(lots);
   double px = Bid;
   if(sl <= 0 || sl <= px) sl = px + 300 * Point;
   if(tp >= px)            tp = px - 600 * Point;
   int t = OrderSend(Symbol(), OP_SELL, lots, px, Slippage, sl, tp, cmt, MagicNumber, 0, clrRed);
   if(t < 0) Print("ORBEAT: SELL failed err=", GetLastError());
   else       Print("ORBEAT: SELL #", t, " px=", px, " sl=", sl, " tp=", tp, " lots=", lots);
  }

void CloseAll()
  {
   for(int i = OrdersTotal()-1; i >= 0; i--)
      if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         if(OrderSymbol()==Symbol() && OrderMagicNumber()==MagicNumber)
           {
            double px = (OrderType()==OP_BUY) ? Bid : Ask;
            if(!OrderClose(OrderTicket(), OrderLots(), px, Slippage, clrYellow))
               Print("ORBEAT: close failed err=", GetLastError());
           }
  }

//+------------------------------------------------------------------+
void CheckHeartbeat()
  {
   if(!FileIsExist(HEARTBEAT_FILE))
     { Print("ORBEAT: WARNING no heartbeat — bot running?"); return; }
   datetime mod = (datetime)FileGetInteger(HEARTBEAT_FILE, FILE_MODIFY_DATE);
   if(TimeCurrent() - mod > 120)
      Print("ORBEAT: WARNING heartbeat stale ", (int)(TimeCurrent()-mod), "s");
  }

bool HasTrade()
  {
   for(int i=0;i<OrdersTotal();i++)
      if(OrderSelect(i,SELECT_BY_POS,MODE_TRADES))
         if(OrderSymbol()==Symbol() && OrderMagicNumber()==MagicNumber)
            return true;
   return false;
  }

double NormLots(double lots)
  {
   double mn = MarketInfo(Symbol(),MODE_MINLOT);
   double mx = MarketInfo(Symbol(),MODE_MAXLOT);
   double st = MarketInfo(Symbol(),MODE_LOTSTEP);
   if(lots<=0) lots=mn;
   lots=MathMax(lots,mn); lots=MathMin(lots,mx);
   return NormalizeDouble(MathRound(lots/st)*st, 2);
  }

string JStr(const string j, const string k)
  {
   string s="\""+k+"\":\""; int p=StringFind(j,s); if(p<0) return "";
   p+=StringLen(s); int e=StringFind(j,"\"",p); if(e<0) return "";
   return StringSubstr(j,p,e-p);
  }

double JDbl(const string j, const string k)
  {
   string s="\""+k+"\":"; int p=StringFind(j,s); if(p<0) return 0;
   p+=StringLen(s); string v="";
   for(int i=p;i<StringLen(j);i++)
     { string c=StringSubstr(j,i,1); if(c==","||c=="}"||c=="]") break; v+=c; }
   return StringToDouble(v);
  }
//+------------------------------------------------------------------+
