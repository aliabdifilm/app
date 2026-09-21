//+------------------------------------------------------------------+
//|                                                   MarketView.mqh |
//|  ApexAlgo - one immutable snapshot of everything we know about a |
//|             symbol at the close of the last completed bar.       |
//|                                                                  |
//| Decisions are taken on bar index 1 (the last CLOSED bar), never   |
//| on the forming bar. This is what makes backtest and live results  |
//| comparable: a signal that reads the live bar repaints and every   |
//| backtest built on it is fiction.                                  |
//+------------------------------------------------------------------+

#ifndef APEX_MARKETVIEW_MQH
#define APEX_MARKETVIEW_MQH

#include "Defs.mqh"
#include "SymbolCtx.mqh"

#define APEX_LOOKBACK 260   // bars pulled per refresh

//+------------------------------------------------------------------+
//| SMarketView                                                      |
//+------------------------------------------------------------------+
struct SMarketView
  {
   bool              ok;
   datetime          barTime;

   //--- price series, index 0 = last CLOSED bar (shifted by one)
   double            close[];
   double            high[];
   double            low[];
   double            open[];

   //--- indicators on the signal timeframe, index 0 = last closed bar
   double            atr[];
   double            adx[];
   double            diPlus[];
   double            diMinus[];
   double            rsi[];
   double            bbUpper[];
   double            bbMid[];
   double            bbLower[];
   double            emaFast[];
   double            emaSlow[];

   //--- bias timeframe
   double            biasClose[];
   double            biasEmaFast[];
   double            biasEmaSlow[];
   double            biasAtr;

   //--- derived scalars
   double            atrNow;
   double            atrAverage;
   double            atrPercentile;   // 0..1 rank of atrNow within lookback
   double            efficiencyRatio; // Kaufman ER
   double            donchianHigh;
   double            donchianLow;
   double            bbWidthPct;      // (upper-lower)/mid
   double            bbWidthPercentile;
   double            lastPrice;
  };

//+------------------------------------------------------------------+
//| CMarketView - fills an SMarketView from a CSymbolCtx             |
//+------------------------------------------------------------------+
class CMarketView
  {
private:
   int               m_donchianPeriod;
   int               m_erPeriod;
   int               m_atrAvgPeriod;

   static bool       Pull(const int handle,const int buffer,const int count,double &dst[])
     {
      ArraySetAsSeries(dst,true);
      // start at 1 -> skip the still-forming bar
      int copied=CopyBuffer(handle,buffer,1,count,dst);
      return (copied==count);
     }

public:
                     CMarketView(void)
     {
      m_donchianPeriod=20;
      m_erPeriod=20;
      m_atrAvgPeriod=100;
     }

   void              Config(const int donchianPeriod,const int erPeriod,const int atrAvgPeriod)
     {
      m_donchianPeriod=donchianPeriod;
      m_erPeriod=erPeriod;
      m_atrAvgPeriod=atrAvgPeriod;
     }

   int               RequiredBars(void) const
     {
      int need=m_atrAvgPeriod+m_donchianPeriod+m_erPeriod+30;
      return MathMax(need,APEX_LOOKBACK);
     }

   //+---------------------------------------------------------------+
   //| Build the snapshot. Returns false if any series is incomplete, |
   //| which is normal right after a terminal restart.                |
   //+---------------------------------------------------------------+
   bool              Build(CSymbolCtx &ctx,SMarketView &v)
     {
      v.ok=false;
      int n=APEX_LOOKBACK;

      ArraySetAsSeries(v.close,true);
      ArraySetAsSeries(v.high,true);
      ArraySetAsSeries(v.low,true);
      ArraySetAsSeries(v.open,true);

      if(CopyClose(ctx.symbol,ctx.tfSignal,1,n,v.close)!=n) return false;
      if(CopyHigh (ctx.symbol,ctx.tfSignal,1,n,v.high) !=n) return false;
      if(CopyLow  (ctx.symbol,ctx.tfSignal,1,n,v.low)  !=n) return false;
      if(CopyOpen (ctx.symbol,ctx.tfSignal,1,n,v.open) !=n) return false;

      if(!Pull(ctx.hAtr     ,0,n,v.atr))      return false;
      if(!Pull(ctx.hAdx     ,0,n,v.adx))      return false;
      if(!Pull(ctx.hAdx     ,1,n,v.diPlus))   return false;
      if(!Pull(ctx.hAdx     ,2,n,v.diMinus))  return false;
      if(!Pull(ctx.hRsi     ,0,n,v.rsi))      return false;
      if(!Pull(ctx.hBands   ,1,n,v.bbUpper))  return false;
      if(!Pull(ctx.hBands   ,0,n,v.bbMid))    return false;
      if(!Pull(ctx.hBands   ,2,n,v.bbLower))  return false;
      if(!Pull(ctx.hEmaFast ,0,n,v.emaFast))  return false;
      if(!Pull(ctx.hEmaSlow ,0,n,v.emaSlow))  return false;

      int bn=60;
      ArraySetAsSeries(v.biasClose,true);
      if(CopyClose(ctx.symbol,ctx.tfBias,1,bn,v.biasClose)!=bn) return false;
      if(!Pull(ctx.hBiasEmaFast,0,bn,v.biasEmaFast)) return false;
      if(!Pull(ctx.hBiasEmaSlow,0,bn,v.biasEmaSlow)) return false;

      double batr[];
      ArraySetAsSeries(batr,true);
      if(CopyBuffer(ctx.hBiasAtr,0,1,3,batr)!=3) return false;
      v.biasAtr=batr[0];

      //--- derived values -----------------------------------------
      v.barTime=(datetime)SeriesInfoInteger(ctx.symbol,ctx.tfSignal,SERIES_LASTBAR_DATE);
      v.atrNow=v.atr[0];
      if(v.atrNow<=0.0) return false;

      int avgN=MathMin(m_atrAvgPeriod,n);
      double acc=0.0;
      for(int i=0;i<avgN;i++) acc+=v.atr[i];
      v.atrAverage=(avgN>0)?acc/avgN:v.atrNow;
      v.atrPercentile=ApexPercentileRank(v.atr,avgN,v.atrNow);

      v.efficiencyRatio=ApexEfficiencyRatio(v.close,m_erPeriod);

      //--- Donchian channel of the completed bars (excludes current)
      double hh=-DBL_MAX, ll=DBL_MAX;
      int dn=MathMin(m_donchianPeriod,n-1);
      for(int i=1;i<=dn;i++)
        {
         if(v.high[i]>hh) hh=v.high[i];
         if(v.low[i] <ll) ll=v.low[i];
        }
      v.donchianHigh=hh;
      v.donchianLow=ll;

      //--- Bollinger width percentile: the squeeze / expansion gauge
      double widths[];
      int wn=MathMin(m_atrAvgPeriod,n);
      ArrayResize(widths,wn);
      for(int i=0;i<wn;i++)
        {
         double mid=v.bbMid[i];
         widths[i]=(mid!=0.0)?((v.bbUpper[i]-v.bbLower[i])/MathAbs(mid)):0.0;
        }
      v.bbWidthPct=widths[0];
      v.bbWidthPercentile=ApexPercentileRank(widths,wn,v.bbWidthPct);

      MqlTick tick;
      if(SymbolInfoTick(ctx.symbol,tick))
         v.lastPrice=(tick.bid+tick.ask)/2.0;
      else
         v.lastPrice=v.close[0];

      v.ok=true;
      return true;
     }
  };

#endif // APEX_MARKETVIEW_MQH
