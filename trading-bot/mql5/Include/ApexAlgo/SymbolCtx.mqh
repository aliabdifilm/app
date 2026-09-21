//+------------------------------------------------------------------+
//|                                                    SymbolCtx.mqh |
//|  ApexAlgo - per-symbol state: broker metadata + indicator handles |
//+------------------------------------------------------------------+

#ifndef APEX_SYMBOLCTX_MQH
#define APEX_SYMBOLCTX_MQH

#include "Defs.mqh"

//+------------------------------------------------------------------+
//| CSymbolCtx                                                       |
//|                                                                  |
//| One instance per traded symbol. Indicator handles are created     |
//| once in Init() and reused: creating them inside OnTick() is the   |
//| classic MQL5 mistake that silently exhausts terminal resources.   |
//+------------------------------------------------------------------+
class CSymbolCtx
  {
public:
   string            symbol;
   bool              valid;

   //--- broker metadata, cached at init and refreshed on demand
   int               digits;
   double            point;
   double            tickSize;
   double            tickValue;      // per 1.0 lot, per tickSize of price move
   double            contractSize;
   double            volMin;
   double            volMax;
   double            volStep;
   int               stopsLevel;     // in points
   int               freezeLevel;    // in points
   string            ccyBase;
   string            ccyProfit;
   ENUM_ORDER_TYPE_FILLING filling;

   //--- timeframes
   ENUM_TIMEFRAMES   tfSignal;
   ENUM_TIMEFRAMES   tfBias;

   //--- indicator handles (signal timeframe)
   int               hAtr;
   int               hAdx;
   int               hRsi;
   int               hBands;
   int               hEmaFast;
   int               hEmaSlow;
   //--- indicator handles (bias / higher timeframe)
   int               hBiasEmaFast;
   int               hBiasEmaSlow;
   int               hBiasAtr;

   //--- bookkeeping
   datetime          lastBarTime;    // last processed signal bar
   datetime          lastTradeTime;  // last entry on this symbol
   int               consecutiveErrors;

                     CSymbolCtx(void) { Clear(); }
                    ~CSymbolCtx(void) { Release(); }

   void              Clear(void)
     {
      symbol=""; valid=false;
      digits=5; point=0.00001; tickSize=0.00001; tickValue=1.0; contractSize=100000.0;
      volMin=0.01; volMax=100.0; volStep=0.01;
      stopsLevel=0; freezeLevel=0;
      ccyBase=""; ccyProfit="";
      filling=ORDER_FILLING_IOC;
      tfSignal=PERIOD_M15; tfBias=PERIOD_H4;
      hAtr=INVALID_HANDLE; hAdx=INVALID_HANDLE; hRsi=INVALID_HANDLE;
      hBands=INVALID_HANDLE; hEmaFast=INVALID_HANDLE; hEmaSlow=INVALID_HANDLE;
      hBiasEmaFast=INVALID_HANDLE; hBiasEmaSlow=INVALID_HANDLE; hBiasAtr=INVALID_HANDLE;
      lastBarTime=0; lastTradeTime=0; consecutiveErrors=0;
     }

   //+---------------------------------------------------------------+
   //| Pick a filling mode the symbol actually supports.             |
   //| Hard-coding FOK is the #1 cause of retcode 10030.             |
   //+---------------------------------------------------------------+
   static ENUM_ORDER_TYPE_FILLING DetectFilling(const string sym)
     {
      long modes=0;
      if(!SymbolInfoInteger(sym,SYMBOL_FILLING_MODE,modes))
         return ORDER_FILLING_RETURN;

      ENUM_SYMBOL_TRADE_EXECUTION exec=
         (ENUM_SYMBOL_TRADE_EXECUTION)SymbolInfoInteger(sym,SYMBOL_TRADE_EXEMODE);

      // Market / Exchange execution never accepts RETURN.
      if((modes&SYMBOL_FILLING_IOC)!=0) return ORDER_FILLING_IOC;
      if((modes&SYMBOL_FILLING_FOK)!=0) return ORDER_FILLING_FOK;

      if(exec==SYMBOL_TRADE_EXECUTION_MARKET || exec==SYMBOL_TRADE_EXECUTION_EXCHANGE)
         return ORDER_FILLING_IOC;   // best effort; broker reported nothing usable

      return ORDER_FILLING_RETURN;
     }

   //+---------------------------------------------------------------+
   //| Refresh cached broker metadata (spreads/levels can change).   |
   //+---------------------------------------------------------------+
   bool              RefreshMeta(void)
     {
      if(!SymbolInfoInteger(symbol,SYMBOL_SELECT))
        {
         if(!SymbolSelect(symbol,true)) return false;
        }

      digits       = (int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
      point        = SymbolInfoDouble(symbol,SYMBOL_POINT);
      tickSize     = SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_SIZE);
      contractSize = SymbolInfoDouble(symbol,SYMBOL_TRADE_CONTRACT_SIZE);
      volMin       = SymbolInfoDouble(symbol,SYMBOL_VOLUME_MIN);
      volMax       = SymbolInfoDouble(symbol,SYMBOL_VOLUME_MAX);
      volStep      = SymbolInfoDouble(symbol,SYMBOL_VOLUME_STEP);
      stopsLevel   = (int)SymbolInfoInteger(symbol,SYMBOL_TRADE_STOPS_LEVEL);
      freezeLevel  = (int)SymbolInfoInteger(symbol,SYMBOL_TRADE_FREEZE_LEVEL);
      ccyBase      = SymbolInfoString(symbol,SYMBOL_CURRENCY_BASE);
      ccyProfit    = SymbolInfoString(symbol,SYMBOL_CURRENCY_PROFIT);

      // Prefer the "loss" tick value: that is what a stop-loss actually costs.
      tickValue = SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_VALUE_LOSS);
      if(tickValue<=0.0) tickValue=SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_VALUE);

      if(point<=0.0)    point=MathPow(10,-digits);
      if(tickSize<=0.0) tickSize=point;
      if(volStep<=0.0)  volStep=0.01;

      filling=DetectFilling(symbol);
      return (tickValue>0.0 && tickSize>0.0);
     }

   //+---------------------------------------------------------------+
   //| Create every indicator handle we will ever need.              |
   //+---------------------------------------------------------------+
   bool              Init(const string sym,
                          const ENUM_TIMEFRAMES sigTf,
                          const ENUM_TIMEFRAMES biasTf,
                          const int atrPeriod,
                          const int adxPeriod,
                          const int rsiPeriod,
                          const int bbPeriod,
                          const double bbDev,
                          const int emaFastPeriod,
                          const int emaSlowPeriod,
                          const int biasFastPeriod,
                          const int biasSlowPeriod)
     {
      Release();
      symbol=sym;
      tfSignal=sigTf;
      tfBias=biasTf;

      if(!SymbolSelect(symbol,true))
        {
         valid=false;
         return false;
        }
      if(!RefreshMeta())
        {
         valid=false;
         return false;
        }

      hAtr        = iATR(symbol,tfSignal,atrPeriod);
      hAdx        = iADX(symbol,tfSignal,adxPeriod);
      hRsi        = iRSI(symbol,tfSignal,rsiPeriod,PRICE_CLOSE);
      hBands      = iBands(symbol,tfSignal,bbPeriod,0,bbDev,PRICE_CLOSE);
      hEmaFast    = iMA(symbol,tfSignal,emaFastPeriod,0,MODE_EMA,PRICE_CLOSE);
      hEmaSlow    = iMA(symbol,tfSignal,emaSlowPeriod,0,MODE_EMA,PRICE_CLOSE);
      hBiasEmaFast= iMA(symbol,tfBias,biasFastPeriod,0,MODE_EMA,PRICE_CLOSE);
      hBiasEmaSlow= iMA(symbol,tfBias,biasSlowPeriod,0,MODE_EMA,PRICE_CLOSE);
      hBiasAtr    = iATR(symbol,tfBias,atrPeriod);

      valid = (hAtr!=INVALID_HANDLE && hAdx!=INVALID_HANDLE && hRsi!=INVALID_HANDLE &&
               hBands!=INVALID_HANDLE && hEmaFast!=INVALID_HANDLE && hEmaSlow!=INVALID_HANDLE &&
               hBiasEmaFast!=INVALID_HANDLE && hBiasEmaSlow!=INVALID_HANDLE &&
               hBiasAtr!=INVALID_HANDLE);
      return valid;
     }

   void              Release(void)
     {
      if(hAtr!=INVALID_HANDLE)         { IndicatorRelease(hAtr);         hAtr=INVALID_HANDLE; }
      if(hAdx!=INVALID_HANDLE)         { IndicatorRelease(hAdx);         hAdx=INVALID_HANDLE; }
      if(hRsi!=INVALID_HANDLE)         { IndicatorRelease(hRsi);         hRsi=INVALID_HANDLE; }
      if(hBands!=INVALID_HANDLE)       { IndicatorRelease(hBands);       hBands=INVALID_HANDLE; }
      if(hEmaFast!=INVALID_HANDLE)     { IndicatorRelease(hEmaFast);     hEmaFast=INVALID_HANDLE; }
      if(hEmaSlow!=INVALID_HANDLE)     { IndicatorRelease(hEmaSlow);     hEmaSlow=INVALID_HANDLE; }
      if(hBiasEmaFast!=INVALID_HANDLE) { IndicatorRelease(hBiasEmaFast); hBiasEmaFast=INVALID_HANDLE; }
      if(hBiasEmaSlow!=INVALID_HANDLE) { IndicatorRelease(hBiasEmaSlow); hBiasEmaSlow=INVALID_HANDLE; }
      if(hBiasAtr!=INVALID_HANDLE)     { IndicatorRelease(hBiasAtr);     hBiasAtr=INVALID_HANDLE; }
      valid=false;
     }

   //+---------------------------------------------------------------+
   //| Helpers                                                       |
   //+---------------------------------------------------------------+
   double            NormalizePrice(const double price) const
     {
      if(tickSize<=0.0) return NormalizeDouble(price,digits);
      return NormalizeDouble(MathRound(price/tickSize)*tickSize,digits);
     }

   double            NormalizeVolume(const double vol) const
     {
      double v=vol;
      if(volStep>0.0) v=MathFloor(v/volStep+1e-9)*volStep;
      if(v<volMin) v=volMin;
      if(v>volMax) v=volMax;
      // volStep can be 0.01 -> 2 decimals; derive digits from the step
      int vdig=0;
      double s=volStep;
      while(s<1.0 && vdig<8) { s*=10.0; vdig++; }
      return NormalizeDouble(v,vdig);
     }

   //--- current spread in price units
   double            SpreadPrice(void) const
     {
      double ask=SymbolInfoDouble(symbol,SYMBOL_ASK);
      double bid=SymbolInfoDouble(symbol,SYMBOL_BID);
      return MathMax(0.0,ask-bid);
     }

   //--- minimum distance a stop must keep from price, in price units
   double            MinStopDistance(void) const
     {
      return (double)stopsLevel*point;
     }

   //--- true once per new bar on the signal timeframe
   bool              IsNewBar(void)
     {
      datetime t=(datetime)SeriesInfoInteger(symbol,tfSignal,SERIES_LASTBAR_DATE);
      if(t==0) return false;
      if(t==lastBarTime) return false;
      lastBarTime=t;
      return true;
     }

   //--- have we loaded enough history to compute everything?
   bool              HasHistory(const int needBars) const
     {
      long bars=SeriesInfoInteger(symbol,tfSignal,SERIES_BARS_COUNT);
      long bbars=SeriesInfoInteger(symbol,tfBias,SERIES_BARS_COUNT);
      return (bars>=needBars && bbars>=50);
     }
  };

#endif // APEX_SYMBOLCTX_MQH
