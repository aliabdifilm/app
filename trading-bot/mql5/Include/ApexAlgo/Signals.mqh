//+------------------------------------------------------------------+
//|                                                      Signals.mqh |
//|  ApexAlgo - regime classifier + weighted strategy ensemble       |
//|                                                                  |
//| WHY AN ENSEMBLE                                                   |
//| A single indicator rule is a single bet on a single market        |
//| behaviour. Markets alternate between trending and mean-reverting  |
//| phases, and a rule that prints money in one phase bleeds in the   |
//| other. This engine therefore does two things in order:            |
//|                                                                   |
//|   1. Classify the current regime (trend / range / neutral).       |
//|   2. Run only the strategy family that matches that regime, and   |
//|      require several independent sub-signals to agree before      |
//|      committing capital.                                          |
//|                                                                   |
//| The score is deliberately continuous (-1..+1) rather than a       |
//| boolean: conviction feeds position sizing, so a marginal setup    |
//| gets a smaller position instead of the same one as a perfect one. |
//+------------------------------------------------------------------+

#ifndef APEX_SIGNALS_MQH
#define APEX_SIGNALS_MQH

#include "Defs.mqh"
#include "SymbolCtx.mqh"
#include "MarketView.mqh"
#include "Logger.mqh"

//+------------------------------------------------------------------+
//| CSignalEngine                                                    |
//+------------------------------------------------------------------+
class CSignalEngine
  {
private:
   CApexLogger      *m_log;

   //--- regime thresholds
   double            m_adxTrendMin;
   double            m_adxRangeMax;
   double            m_erTrendMin;
   double            m_erRangeMax;

   //--- entry thresholds
   double            m_minScore;
   double            m_rsiOversold;
   double            m_rsiOverbought;
   int               m_pullbackLookback;

   //--- stop / target geometry
   double            m_atrStopTrend;
   double            m_atrStopRange;
   double            m_rrTrend;
   double            m_rrRange;

   //--- behaviour
   ENUM_APEX_STRATMODE m_mode;
   bool              m_requireBiasAlignment;

   //--- ensemble weights
   double            m_wBias, m_wBreakout, m_wPullback, m_wMomentum;
   double            m_wRevert, m_wRsi, m_wBiasRange;

public:
                     CSignalEngine(void)
     {
      m_log=NULL;
      m_adxTrendMin=23.0;
      m_adxRangeMax=18.0;
      m_erTrendMin=0.30;
      m_erRangeMax=0.22;
      m_minScore=0.45;
      m_rsiOversold=30.0;
      m_rsiOverbought=70.0;
      m_pullbackLookback=5;
      m_atrStopTrend=2.0;
      m_atrStopRange=1.5;
      m_rrTrend=2.0;
      m_rrRange=1.2;
      m_mode=APEX_STRAT_AUTO;
      m_requireBiasAlignment=true;
      m_wBias=0.20; m_wBreakout=0.35; m_wPullback=0.25; m_wMomentum=0.20;
      m_wRevert=0.55; m_wRsi=0.30; m_wBiasRange=0.15;
     }

   void              SetLogger(CApexLogger *l) { m_log=l; }

   void              ConfigRegime(const double adxTrendMin,const double adxRangeMax,
                                  const double erTrendMin,const double erRangeMax)
     {
      m_adxTrendMin=adxTrendMin;
      m_adxRangeMax=adxRangeMax;
      m_erTrendMin=erTrendMin;
      m_erRangeMax=erRangeMax;
     }

   void              ConfigEntry(const double minScore,const double rsiOversold,
                                 const double rsiOverbought,const int pullbackLookback)
     {
      m_minScore=minScore;
      m_rsiOversold=rsiOversold;
      m_rsiOverbought=rsiOverbought;
      m_pullbackLookback=pullbackLookback;
     }

   void              ConfigGeometry(const double atrStopTrend,const double atrStopRange,
                                    const double rrTrend,const double rrRange)
     {
      m_atrStopTrend=atrStopTrend;
      m_atrStopRange=atrStopRange;
      m_rrTrend=rrTrend;
      m_rrRange=rrRange;
     }

   void              ConfigMode(const ENUM_APEX_STRATMODE mode,const bool requireBias)
     {
      m_mode=mode;
      m_requireBiasAlignment=requireBias;
     }

   //+---------------------------------------------------------------+
   //| Regime classification                                          |
   //|                                                                |
   //| ADX alone is noisy and lags. Combining it with Kaufman's        |
   //| efficiency ratio (how much net travel per unit of path length)  |
   //| gives a far more stable read, and the Bollinger-width           |
   //| percentile catches squeezes that neither of them sees.          |
   //+---------------------------------------------------------------+
   ENUM_APEX_REGIME  Classify(SMarketView &v,double &strength)
     {
      strength=0.0;
      if(!v.ok) return APEX_REGIME_UNKNOWN;

      double adx=v.adx[0];
      double er =v.efficiencyRatio;

      bool trendy = (adx>=m_adxTrendMin && er>=m_erTrendMin);
      bool rangey = (adx<=m_adxRangeMax && er<=m_erRangeMax);

      if(trendy && !rangey)
        {
         double a=ApexClamp((adx-m_adxTrendMin)/25.0,0.0,1.0);
         double e=ApexClamp((er-m_erTrendMin)/0.40,0.0,1.0);
         strength=ApexClamp(0.5*a+0.5*e,0.0,1.0);
         return APEX_REGIME_TREND;
        }

      if(rangey && !trendy)
        {
         double a=ApexClamp((m_adxRangeMax-adx)/m_adxRangeMax,0.0,1.0);
         double e=ApexClamp((m_erRangeMax-er)/MathMax(m_erRangeMax,0.0001),0.0,1.0);
         strength=ApexClamp(0.5*a+0.5*e,0.0,1.0);
         return APEX_REGIME_RANGE;
        }

      strength=0.0;
      return APEX_REGIME_NEUTRAL;
     }

   //+---------------------------------------------------------------+
   //| Sub-signal: higher timeframe bias                              |
   //+---------------------------------------------------------------+
   double            ScoreBias(SMarketView &v)
     {
      double c   = v.biasClose[0];
      double fast= v.biasEmaFast[0];
      double slow= v.biasEmaSlow[0];

      double s=0.0;
      if(fast>slow) s+=0.5; else if(fast<slow) s-=0.5;
      if(c>slow)    s+=0.5; else if(c<slow)    s-=0.5;

      // slope of the slow EMA adds persistence information
      if(ArraySize(v.biasEmaSlow)>3)
        {
         double slope=v.biasEmaSlow[0]-v.biasEmaSlow[3];
         if(slope>0.0)      s=ApexClamp(s+0.15,-1.0,1.0);
         else if(slope<0.0) s=ApexClamp(s-0.15,-1.0,1.0);
        }
      return ApexClamp(s,-1.0,1.0);
     }

   //+---------------------------------------------------------------+
   //| Sub-signal: Donchian breakout with volatility confirmation     |
   //+---------------------------------------------------------------+
   double            ScoreBreakout(SMarketView &v)
     {
      double c=v.close[0];
      double s=0.0;

      if(c>v.donchianHigh)      s=1.0;
      else if(c<v.donchianLow)  s=-1.0;
      else
        {
         // near-miss breakouts still carry information
         double range=v.donchianHigh-v.donchianLow;
         if(range>0.0)
           {
            double pos=(c-v.donchianLow)/range;      // 0..1
            s=ApexClamp((pos-0.5)*1.2,-0.6,0.6);
           }
        }

      // a breakout on collapsing volatility is usually a trap
      double expansion=(v.atrAverage>0.0)?(v.atrNow/v.atrAverage):1.0;
      double confirm=ApexClamp(expansion,0.6,1.6)/1.6;   // 0.375 .. 1.0
      return ApexClamp(s*confirm,-1.0,1.0);
     }

   //+---------------------------------------------------------------+
   //| Sub-signal: pullback continuation inside an established trend  |
   //+---------------------------------------------------------------+
   double            ScorePullback(SMarketView &v)
     {
      double fast=v.emaFast[0];
      double slow=v.emaSlow[0];
      if(fast==slow) return 0.0;

      bool up=(fast>slow);
      int look=MathMin(m_pullbackLookback,ArraySize(v.close)-1);
      if(look<2) return 0.0;

      if(up)
        {
         // did price recently trade into the fast EMA and close back above it?
         bool touched=false;
         for(int i=1;i<=look;i++)
            if(v.low[i]<=v.emaFast[i]) { touched=true; break; }
         if(touched && v.close[0]>fast)
           {
            double depth=MathAbs(v.close[0]-fast)/MathMax(v.atrNow,1e-10);
            double quality=ApexClamp(1.2-depth,0.3,1.0); // closer to EMA = better entry
            return quality;
           }
         return 0.0;
        }

      bool touchedDn=false;
      for(int i=1;i<=look;i++)
         if(v.high[i]>=v.emaFast[i]) { touchedDn=true; break; }
      if(touchedDn && v.close[0]<fast)
        {
         double depth=MathAbs(fast-v.close[0])/MathMax(v.atrNow,1e-10);
         double quality=ApexClamp(1.2-depth,0.3,1.0);
         return -quality;
        }
      return 0.0;
     }

   //+---------------------------------------------------------------+
   //| Sub-signal: directional momentum from the ADX DI pair          |
   //+---------------------------------------------------------------+
   double            ScoreMomentum(SMarketView &v)
     {
      double dp=v.diPlus[0];
      double dm=v.diMinus[0];
      double sum=dp+dm;
      if(sum<=0.0) return 0.0;
      return ApexClamp((dp-dm)/sum,-1.0,1.0);
     }

   //+---------------------------------------------------------------+
   //| Sub-signal: Bollinger mean reversion                           |
   //+---------------------------------------------------------------+
   double            ScoreRevert(SMarketView &v)
     {
      double c=v.close[0];
      double up=v.bbUpper[0];
      double lo=v.bbLower[0];
      double mid=v.bbMid[0];
      double halfWidth=(up-lo)/2.0;
      if(halfWidth<=0.0) return 0.0;

      // how many half-widths away from the mean are we?
      double z=(c-mid)/halfWidth;

      // stretched below the band -> buy; above -> sell
      double s=-ApexClamp(z,-2.0,2.0)/2.0;

      // only pay attention once price is genuinely outside the band
      if(MathAbs(z)<0.85) s*=0.35;

      // a reversion entry against an exploding range is a knife catch
      if(v.atrAverage>0.0 && v.atrNow>1.8*v.atrAverage) s*=0.4;

      return ApexClamp(s,-1.0,1.0);
     }

   //+---------------------------------------------------------------+
   //| Sub-signal: RSI exhaustion turning back                        |
   //+---------------------------------------------------------------+
   double            ScoreRsi(SMarketView &v)
     {
      if(ArraySize(v.rsi)<3) return 0.0;
      double r0=v.rsi[0];
      double r1=v.rsi[1];

      if(r1<=m_rsiOversold && r0>r1)
        {
         double depth=ApexClamp((m_rsiOversold-r1)/m_rsiOversold,0.0,1.0);
         return ApexClamp(0.5+0.5*depth,0.0,1.0);
        }
      if(r1>=m_rsiOverbought && r0<r1)
        {
         double depth=ApexClamp((r1-m_rsiOverbought)/MathMax(100.0-m_rsiOverbought,1.0),0.0,1.0);
         return -ApexClamp(0.5+0.5*depth,0.0,1.0);
        }
      return 0.0;
     }

   //+---------------------------------------------------------------+
   //| Build the final, tradable signal.                              |
   //+---------------------------------------------------------------+
   bool              Evaluate(CSymbolCtx &ctx,SMarketView &v,ApexSignal &sig)
     {
      sig.dir=APEX_DIR_NONE;
      sig.score=0.0;
      sig.confidence=0.0;
      sig.regime=APEX_REGIME_UNKNOWN;
      sig.source="";
      sig.note="";
      sig.atr=0.0;
      sig.stopDistance=0.0;
      sig.targetDistance=0.0;

      if(!v.ok) { sig.note="market view unavailable"; return false; }

      double regimeStrength=0.0;
      ENUM_APEX_REGIME regime=Classify(v,regimeStrength);
      sig.regime=regime;
      sig.atr=v.atrNow;

      //--- honour the manual strategy override -------------------
      if(m_mode==APEX_STRAT_TREND_ONLY && regime!=APEX_REGIME_TREND)
        { sig.note="mode=trend only, regime="+ApexRegimeToString(regime); return false; }
      if(m_mode==APEX_STRAT_RANGE_ONLY && regime!=APEX_REGIME_RANGE)
        { sig.note="mode=range only, regime="+ApexRegimeToString(regime); return false; }
      if(regime==APEX_REGIME_NEUTRAL || regime==APEX_REGIME_UNKNOWN)
        { sig.note="regime "+ApexRegimeToString(regime)+" - standing aside"; return false; }

      double bias=ScoreBias(v);
      double total=0.0;
      double atrStopMult=m_atrStopTrend;
      double rr=m_rrTrend;

      if(regime==APEX_REGIME_TREND)
        {
         double br=ScoreBreakout(v);
         double pb=ScorePullback(v);
         double mo=ScoreMomentum(v);

         total = m_wBias*bias + m_wBreakout*br + m_wPullback*pb + m_wMomentum*mo;
         sig.source="TREND(breakout+pullback+momentum)";
         sig.note=StringFormat("adx=%.1f er=%.2f bias=%.2f brk=%.2f pull=%.2f mom=%.2f",
                               v.adx[0],v.efficiencyRatio,bias,br,pb,mo);

         // In a trend regime, fighting the higher timeframe is the single
         // most expensive thing a retail system can do.
         if(m_requireBiasAlignment)
           {
            if(total>0.0 && bias< -0.10) { sig.note+=" | blocked: bias short"; return false; }
            if(total<0.0 && bias>  0.10) { sig.note+=" | blocked: bias long";  return false; }
           }

         atrStopMult=m_atrStopTrend;
         rr=m_rrTrend;
        }
      else // APEX_REGIME_RANGE
        {
         double rv=ScoreRevert(v);
         double rs=ScoreRsi(v);

         total = m_wRevert*rv + m_wRsi*rs + m_wBiasRange*bias;
         sig.source="RANGE(bollinger+rsi)";
         sig.note=StringFormat("adx=%.1f er=%.2f rev=%.2f rsi=%.2f bias=%.2f",
                               v.adx[0],v.efficiencyRatio,rv,rs,bias);

         // Never mean-revert into a squeeze that is about to break out.
         if(v.bbWidthPercentile<0.10)
           { sig.note+=" | blocked: volatility squeeze"; return false; }

         atrStopMult=m_atrStopRange;
         rr=m_rrRange;
        }

      total=ApexClamp(total,-1.0,1.0);
      sig.score=total;

      if(MathAbs(total)<m_minScore)
        {
         sig.note+=StringFormat(" | score %.2f < min %.2f",total,m_minScore);
         return false;
        }

      sig.dir=(total>0.0)?APEX_DIR_LONG:APEX_DIR_SHORT;

      //--- conviction: how far past the threshold, tempered by regime clarity
      double past=(MathAbs(total)-m_minScore)/MathMax(1.0-m_minScore,0.0001);
      sig.confidence=ApexClamp(0.35+0.45*past+0.20*regimeStrength,0.0,1.0);

      //--- geometry ------------------------------------------------
      double stop=v.atrNow*atrStopMult;
      double minStop=ctx.MinStopDistance()+ctx.SpreadPrice();
      if(stop<minStop) stop=minStop*1.15;      // respect the broker's stop level

      sig.stopDistance=stop;
      sig.targetDistance=stop*rr;
      return true;
     }

   double            MinScore(void) const { return m_minScore; }
  };

#endif // APEX_SIGNALS_MQH
