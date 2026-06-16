//+------------------------------------------------------------------+
//| TrendContinuation.mq5                                             |
//| Ported from the FarhanFX Algo Python strategy that backtested     |
//| 63.6% win rate, Profit Factor 2.25 on XAUUSDc H1 (33 trades,       |
//| 10-month real broker history) — the best of 18 strategies tested. |
//|                                                                    |
//| Entry: price above/below EMA200 (trend), Supertrend aligned,      |
//| pullback to EMA50 (within NearEMA50_ATRMult x ATR), RSI in a      |
//| momentum zone (not overbought/oversold), confirming candle,       |
//| D1+H4 trend agreement, London/NY session only.                    |
//| Exit: ATR-based SL/TP — 1.5x ATR stop, 3.0x ATR target (2:1 R:R). |
//+------------------------------------------------------------------+
#property copyright "FarhanFX"
#property version   "1.00"
#property strict

//==================== SYMBOL / TIMEFRAME ====================
input string          TradingSymbol    = "XAUUSDc";
input ENUM_TIMEFRAMES Timeframe        = PERIOD_H1;

//==================== TREND FILTERS ====================
input int    EMA50_Period      = 50;
input int    EMA200_Period     = 200;
input int    Supertrend_Period = 10;
input double Supertrend_Mult   = 3.0;
input int    RSI_Period        = 14;
input int    ATR_Period        = 14;
input double MinimumATR        = 3.0;     // skip signal if ATR below this

//==================== ENTRY CONDITIONS ====================
input double NearEMA50_ATRMult = 0.8;     // price must be within this x ATR of EMA50
input double RSI_Buy_Min       = 45;
input double RSI_Buy_Max       = 65;
input double RSI_Sell_Min      = 35;
input double RSI_Sell_Max      = 55;

//==================== MULTI-TF TREND AGREEMENT ====================
input bool   RequireD1Trend     = true;
input bool   RequireH4Trend     = true;

//==================== SESSION FILTER (UTC) ====================
input bool   EnableSessionFilter = true;
input int    London_StartHour    = 7;
input int    London_EndHour      = 11;
input int    NY_StartHour        = 12;
input int    NY_EndHour          = 16;

//==================== RISK MANAGEMENT ====================
input double LotSize        = 0.01;
input double SL_ATRMult     = 1.5;
input double TP_ATRMult     = 3.0;
input int    MaxSlippage    = 10;
input int    MaxOpenTrades  = 2;

//==================== IDENTITY ====================
input long   MagicNumber    = 444777;

//==================== STATE / HANDLES ====================
string   g_symbol;
int      g_emaH50, g_emaH200, g_rsiH, g_atrH;
int      g_emaD1_50, g_emaD1_200, g_emaH4_50, g_emaH4_200;
datetime g_lastBarTime = 0;

//+------------------------------------------------------------------+
int OnInit()
{
   g_symbol = (TradingSymbol == "" ? _Symbol : TradingSymbol);
   if(!SymbolSelect(g_symbol, true))
   {
      Print("TrendContinuation: failed to select symbol ", g_symbol);
      return INIT_FAILED;
   }

   g_emaH50  = iMA(g_symbol, Timeframe, EMA50_Period,  0, MODE_EMA, PRICE_CLOSE);
   g_emaH200 = iMA(g_symbol, Timeframe, EMA200_Period, 0, MODE_EMA, PRICE_CLOSE);
   g_rsiH    = iRSI(g_symbol, Timeframe, RSI_Period, PRICE_CLOSE);
   g_atrH    = iATR(g_symbol, Timeframe, ATR_Period);

   g_emaD1_50  = iMA(g_symbol, PERIOD_D1, EMA50_Period,  0, MODE_EMA, PRICE_CLOSE);
   g_emaD1_200 = iMA(g_symbol, PERIOD_D1, EMA200_Period, 0, MODE_EMA, PRICE_CLOSE);
   g_emaH4_50  = iMA(g_symbol, PERIOD_H4, EMA50_Period,  0, MODE_EMA, PRICE_CLOSE);
   g_emaH4_200 = iMA(g_symbol, PERIOD_H4, EMA200_Period, 0, MODE_EMA, PRICE_CLOSE);

   if(g_emaH50==INVALID_HANDLE || g_emaH200==INVALID_HANDLE || g_rsiH==INVALID_HANDLE || g_atrH==INVALID_HANDLE ||
      g_emaD1_50==INVALID_HANDLE || g_emaD1_200==INVALID_HANDLE || g_emaH4_50==INVALID_HANDLE || g_emaH4_200==INVALID_HANDLE)
   {
      Print("TrendContinuation: indicator handle creation failed");
      return INIT_FAILED;
   }

   g_lastBarTime = iTime(g_symbol, Timeframe, 0);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   IndicatorRelease(g_emaH50);  IndicatorRelease(g_emaH200);
   IndicatorRelease(g_rsiH);    IndicatorRelease(g_atrH);
   IndicatorRelease(g_emaD1_50); IndicatorRelease(g_emaD1_200);
   IndicatorRelease(g_emaH4_50); IndicatorRelease(g_emaH4_200);
}

//+------------------------------------------------------------------+
double GetBuf(int handle, int shift)
{
   double buf[];
   ArraySetAsSeries(buf, true);
   if(CopyBuffer(handle, 0, shift, 1, buf) <= 0) return EMPTY_VALUE;
   return buf[0];
}

//+------------------------------------------------------------------+
//| Manual Supertrend (not a built-in MQL5 indicator) — mirrors the   |
//| exact recursive logic from the Python backtest, bar for bar.      |
//| Returns direction at `shift` (1 = bullish, -1 = bearish, 0 = n/a) |
//+------------------------------------------------------------------+
int SupertrendDirection(string symbol, ENUM_TIMEFRAMES tf, int period, double mult, int shift)
{
   int bars = 150;
   double hiS[], loS[], clS[];
   ArraySetAsSeries(hiS, true); ArraySetAsSeries(loS, true); ArraySetAsSeries(clS, true);
   int got_h = CopyHigh(symbol, tf, shift, bars, hiS);
   int got_l = CopyLow(symbol, tf, shift, bars, loS);
   int got_c = CopyClose(symbol, tf, shift, bars, clS);
   int got = MathMin(got_h, MathMin(got_l, got_c));
   if(got < 50) return 0;
   bars = got;

   // reverse to chronological order (oldest first) for the recursive calc
   double hi[], lo[], cl[];
   ArrayResize(hi, bars); ArrayResize(lo, bars); ArrayResize(cl, bars);
   for(int i = 0; i < bars; i++)
   {
      hi[i] = hiS[bars-1-i];
      lo[i] = loS[bars-1-i];
      cl[i] = clS[bars-1-i];
   }

   // ATR (Wilder), matching the Python atr_s() backfill behavior
   double tr[];
   ArrayResize(tr, bars);
   tr[0] = hi[0] - lo[0];
   for(int i = 1; i < bars; i++)
      tr[i] = MathMax(hi[i]-lo[i], MathMax(MathAbs(hi[i]-cl[i-1]), MathAbs(lo[i]-cl[i-1])));

   int n = MathMin(period, bars);
   double atrv[];
   ArrayResize(atrv, bars);
   double sumFirst = 0;
   for(int i = 0; i < n; i++) sumFirst += tr[i];
   double aPrev = sumFirst / n;
   for(int i = 0; i < n; i++) atrv[i] = aPrev;
   for(int i = n; i < bars; i++)
   {
      aPrev = (aPrev*(n-1) + tr[i]) / n;
      atrv[i] = aPrev;
   }

   // Supertrend bands + direction flip logic
   double up[], dn[];
   int    trend[];
   ArrayResize(up, bars); ArrayResize(dn, bars); ArrayResize(trend, bars);
   up[0] = 0; dn[0] = 0; trend[0] = 1;

   for(int i = 1; i < bars; i++)
   {
      double mid = (hi[i]+lo[i]) / 2.0;
      double bu  = mid - mult*atrv[i];
      double bd  = mid + mult*atrv[i];

      up[i] = (bu > up[i-1] || cl[i-1] < up[i-1]) ? bu : up[i-1];
      dn[i] = (bd < dn[i-1] || cl[i-1] > dn[i-1]) ? bd : dn[i-1];

      if(trend[i-1] == -1 && cl[i] > dn[i])      trend[i] = 1;
      else if(trend[i-1] == 1 && cl[i] < up[i])  trend[i] = -1;
      else                                          trend[i] = trend[i-1];
   }

   return trend[bars-1];
}

//+------------------------------------------------------------------+
bool PassesSessionFilter()
{
   if(!EnableSessionFilter) return true;
   MqlDateTime t;
   TimeToStruct(TimeGMT(), t);
   bool london = (t.hour >= London_StartHour && t.hour < London_EndHour);
   bool ny     = (t.hour >= NY_StartHour && t.hour < NY_EndHour);
   return london || ny;
}

//+------------------------------------------------------------------+
int CountOpenPositions()
{
   int cnt = 0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_symbol) continue;
      if((long)PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      cnt++;
   }
   return cnt;
}

//+------------------------------------------------------------------+
void OpenTrade(ENUM_ORDER_TYPE type, double atrv)
{
   double price = (type == ORDER_TYPE_BUY)
                  ? SymbolInfoDouble(g_symbol, SYMBOL_ASK)
                  : SymbolInfoDouble(g_symbol, SYMBOL_BID);
   double slDist = atrv * SL_ATRMult;
   double tpDist = atrv * TP_ATRMult;
   double sl = (type == ORDER_TYPE_BUY) ? price - slDist : price + slDist;
   double tp = (type == ORDER_TYPE_BUY) ? price + tpDist : price - tpDist;

   int digits = (int)SymbolInfoInteger(g_symbol, SYMBOL_DIGITS);
   sl = NormalizeDouble(sl, digits);
   tp = NormalizeDouble(tp, digits);

   MqlTradeRequest req; MqlTradeResult res;
   ZeroMemory(req); ZeroMemory(res);
   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = g_symbol;
   req.volume       = LotSize;
   req.type         = type;
   req.price        = price;
   req.sl           = sl;
   req.tp           = tp;
   req.deviation    = MaxSlippage;
   req.magic        = MagicNumber;
   req.type_filling = ORDER_FILLING_FOK;

   if(!OrderSend(req, res))
      Print("TrendContinuation: order failed err=", GetLastError());
   else
      Print("TrendContinuation: ", (type==ORDER_TYPE_BUY?"BUY":"SELL"),
            " @", price, " SL=", sl, " TP=", tp);
}

//+------------------------------------------------------------------+
//| Acts once per new closed bar — mirrors the backtest exactly,      |
//| which only ever saw fully-closed historical bars.                 |
//+------------------------------------------------------------------+
void OnTick()
{
   datetime curBar = iTime(g_symbol, Timeframe, 0);
   if(curBar == g_lastBarTime) return;
   g_lastBarTime = curBar;

   if(CountOpenPositions() >= MaxOpenTrades) return;

   double ema50  = GetBuf(g_emaH50, 1);
   double ema200 = GetBuf(g_emaH200, 1);
   double rsi    = GetBuf(g_rsiH, 1);
   double atrv   = GetBuf(g_atrH, 1);
   if(ema50==EMPTY_VALUE || ema200==EMPTY_VALUE || rsi==EMPTY_VALUE || atrv==EMPTY_VALUE) return;
   if(atrv < MinimumATR) return;

   int st = SupertrendDirection(g_symbol, Timeframe, Supertrend_Period, Supertrend_Mult, 1);
   if(st == 0) return;

   double price = iClose(g_symbol, Timeframe, 1);
   double openP = iOpen(g_symbol, Timeframe, 1);
   bool nearEMA50  = MathAbs(price - ema50) < atrv * NearEMA50_ATRMult;
   bool bullCandle = price > openP;
   bool bearCandle = price < openP;

   bool d1Bull = true, d1Bear = true, h4Bull = true, h4Bear = true;
   if(RequireD1Trend)
   {
      double d1c    = iClose(g_symbol, PERIOD_D1, 1);
      double d1e50  = GetBuf(g_emaD1_50, 1);
      double d1e200 = GetBuf(g_emaD1_200, 1);
      if(d1c==EMPTY_VALUE || d1e50==EMPTY_VALUE || d1e200==EMPTY_VALUE) return;
      d1Bull = (d1c > d1e50 && d1e50 > d1e200);
      d1Bear = (d1c < d1e50 && d1e50 < d1e200);
   }
   if(RequireH4Trend)
   {
      double h4c    = iClose(g_symbol, PERIOD_H4, 1);
      double h4e50  = GetBuf(g_emaH4_50, 1);
      double h4e200 = GetBuf(g_emaH4_200, 1);
      if(h4c==EMPTY_VALUE || h4e50==EMPTY_VALUE || h4e200==EMPTY_VALUE) return;
      h4Bull = (h4c > h4e50 && h4e50 > h4e200);
      h4Bear = (h4c < h4e50 && h4e50 < h4e200);
   }

   bool sessionOk = PassesSessionFilter();

   bool buySignal  = (price > ema200 && st==1  && nearEMA50 && bullCandle &&
                       rsi > RSI_Buy_Min  && rsi < RSI_Buy_Max  &&
                       d1Bull && h4Bull && sessionOk);
   bool sellSignal = (price < ema200 && st==-1 && nearEMA50 && bearCandle &&
                       rsi > RSI_Sell_Min && rsi < RSI_Sell_Max &&
                       d1Bear && h4Bear && sessionOk);

   if(buySignal)       OpenTrade(ORDER_TYPE_BUY,  atrv);
   else if(sellSignal) OpenTrade(ORDER_TYPE_SELL, atrv);
}
//+------------------------------------------------------------------+
