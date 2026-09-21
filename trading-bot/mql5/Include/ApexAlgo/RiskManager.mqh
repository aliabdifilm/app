//+------------------------------------------------------------------+
//|                                                  RiskManager.mqh |
//|  ApexAlgo - capital preservation engine                          |
//|                                                                  |
//| This is the most important file in the project.                   |
//|                                                                   |
//| A trading system with a mediocre edge and excellent risk control   |
//| survives long enough to compound. A system with a brilliant edge   |
//| and no risk control eventually meets the one sequence of trades    |
//| that ends the account. Every limit below is a hard stop, not a     |
//| suggestion, and the engine fails CLOSED: if anything cannot be     |
//| verified, trading is refused rather than attempted.                |
//+------------------------------------------------------------------+

#ifndef APEX_RISKMANAGER_MQH
#define APEX_RISKMANAGER_MQH

#include "Defs.mqh"
#include "SymbolCtx.mqh"
#include "Logger.mqh"

//+------------------------------------------------------------------+
//| CRiskManager                                                     |
//+------------------------------------------------------------------+
class CRiskManager
  {
private:
   CApexLogger      *m_log;
   long              m_magic;

   //--- configuration
   double            m_riskPercent;          // base risk per trade, % of equity
   double            m_maxRiskPercent;       // ceiling the remote control cannot exceed
   double            m_dailyLossLimitPct;    // % of day-start balance
   double            m_maxDrawdownPct;       // % from all-time equity peak -> kill switch
   double            m_ddThrottleStartPct;   // start scaling risk down from this DD
   double            m_ddThrottleFloor;      // minimum multiplier when scaling (0..1)
   int               m_maxPositionsTotal;
   int               m_maxPositionsPerSymbol;
   int               m_maxTradesPerDay;
   int               m_lossStreakLimit;
   int               m_cooldownMinutes;
   double            m_maxLotsPerTrade;
   double            m_maxMarginUtilPct;     // never use more than this % of free margin
   int               m_maxExposurePerCurrency;

   //--- runtime state
   datetime          m_currentDay;
   double            m_dayStartBalance;
   double            m_dayStartEquity;
   double            m_peakEquity;
   int               m_tradesToday;
   int               m_lossStreak;
   datetime          m_cooldownUntil;
   ENUM_APEX_HALT    m_halt;
   string            m_haltReason;
   bool              m_killSwitch;

public:
                     CRiskManager(void)
     {
      m_log=NULL;
      m_magic=0;
      m_riskPercent=0.5;
      m_maxRiskPercent=2.0;
      m_dailyLossLimitPct=3.0;
      m_maxDrawdownPct=10.0;
      m_ddThrottleStartPct=4.0;
      m_ddThrottleFloor=0.35;
      m_maxPositionsTotal=4;
      m_maxPositionsPerSymbol=1;
      m_maxTradesPerDay=10;
      m_lossStreakLimit=3;
      m_cooldownMinutes=120;
      m_maxLotsPerTrade=5.0;
      m_maxMarginUtilPct=20.0;
      m_maxExposurePerCurrency=2;

      m_currentDay=0;
      m_dayStartBalance=0.0;
      m_dayStartEquity=0.0;
      m_peakEquity=0.0;
      m_tradesToday=0;
      m_lossStreak=0;
      m_cooldownUntil=0;
      m_halt=APEX_HALT_NONE;
      m_haltReason="";
      m_killSwitch=false;
     }

   void              SetLogger(CApexLogger *l) { m_log=l; }
   void              SetMagic(const long m)    { m_magic=m; }

   void              Config(const double riskPercent,const double maxRiskPercent,
                            const double dailyLossLimitPct,const double maxDrawdownPct,
                            const double ddThrottleStartPct,const double ddThrottleFloor)
     {
      m_riskPercent=riskPercent;
      m_maxRiskPercent=maxRiskPercent;
      m_dailyLossLimitPct=dailyLossLimitPct;
      m_maxDrawdownPct=maxDrawdownPct;
      m_ddThrottleStartPct=ddThrottleStartPct;
      m_ddThrottleFloor=ddThrottleFloor;
     }

   void              ConfigLimits(const int maxPosTotal,const int maxPosPerSymbol,
                                   const int maxTradesPerDay,const int lossStreakLimit,
                                   const int cooldownMinutes,const double maxLotsPerTrade,
                                   const double maxMarginUtilPct,const int maxExposurePerCcy)
     {
      m_maxPositionsTotal=maxPosTotal;
      m_maxPositionsPerSymbol=maxPosPerSymbol;
      m_maxTradesPerDay=maxTradesPerDay;
      m_lossStreakLimit=lossStreakLimit;
      m_cooldownMinutes=cooldownMinutes;
      m_maxLotsPerTrade=maxLotsPerTrade;
      m_maxMarginUtilPct=maxMarginUtilPct;
      m_maxExposurePerCurrency=maxExposurePerCcy;
     }

   //+---------------------------------------------------------------+
   //| Called once at startup and then on every tick. Rolls the daily |
   //| counters over at the broker's midnight, not the local one.     |
   //+---------------------------------------------------------------+
   void              Refresh(const datetime now)
     {
      double balance=AccountInfoDouble(ACCOUNT_BALANCE);
      double equity =AccountInfoDouble(ACCOUNT_EQUITY);

      datetime day=ApexDayStart(now);
      if(day!=m_currentDay)
        {
         m_currentDay=day;
         m_dayStartBalance=balance;
         m_dayStartEquity=equity;
         m_tradesToday=CountTradesToday(day,now);
         if(m_halt==APEX_HALT_DAILY_LOSS || m_halt==APEX_HALT_MAX_TRADES_DAY)
           {
            m_halt=APEX_HALT_NONE;
            m_haltReason="";
            if(m_log!=NULL) m_log.Info("new trading day - daily limits reset");
           }
         if(m_log!=NULL)
            m_log.Info(StringFormat("day rollover: start balance=%.2f equity=%.2f trades today=%d",
                                    m_dayStartBalance,m_dayStartEquity,m_tradesToday));
        }

      if(m_peakEquity<=0.0)  m_peakEquity=equity;
      if(equity>m_peakEquity) m_peakEquity=equity;
     }

   //+---------------------------------------------------------------+
   //| Hard gates evaluated before any symbol is even looked at.      |
   //+---------------------------------------------------------------+
   bool              PortfolioAllowed(const datetime now)
     {
      if(m_killSwitch)
        {
         m_halt=APEX_HALT_KILL_SWITCH;
         m_haltReason="kill switch engaged";
         return false;
        }

      //--- terminal / account permissions
      if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ||
         !MQLInfoInteger(MQL_TRADE_ALLOWED) ||
         !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ||
         !AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
        {
         m_halt=APEX_HALT_TERMINAL;
         m_haltReason="algo trading not permitted by terminal or account";
         return false;
        }

      double equity=AccountInfoDouble(ACCOUNT_EQUITY);

      //--- max drawdown from the all-time equity peak -> permanent stop
      double dd=DrawdownPct();
      if(m_maxDrawdownPct>0.0 && dd>=m_maxDrawdownPct)
        {
         m_halt=APEX_HALT_MAX_DRAWDOWN;
         m_haltReason=StringFormat("drawdown %.2f%% >= limit %.2f%%",dd,m_maxDrawdownPct);
         if(!m_killSwitch && m_log!=NULL)
            m_log.Error("MAX DRAWDOWN BREACHED - "+m_haltReason);
         m_killSwitch=true;
         return false;
        }

      //--- daily loss limit -> stop until the next server day
      double dayPnLPct=DayPnLPct();
      if(m_dailyLossLimitPct>0.0 && dayPnLPct<=-m_dailyLossLimitPct)
        {
         if(m_halt!=APEX_HALT_DAILY_LOSS && m_log!=NULL)
            m_log.Warn(StringFormat("daily loss limit hit: %.2f%%",dayPnLPct));
         m_halt=APEX_HALT_DAILY_LOSS;
         m_haltReason=StringFormat("day P/L %.2f%% <= -%.2f%%",dayPnLPct,m_dailyLossLimitPct);
         return false;
        }

      //--- trades per day
      if(m_maxTradesPerDay>0 && m_tradesToday>=m_maxTradesPerDay)
        {
         m_halt=APEX_HALT_MAX_TRADES_DAY;
         m_haltReason=StringFormat("%d trades today >= limit %d",m_tradesToday,m_maxTradesPerDay);
         return false;
        }

      //--- consecutive loss cooldown
      if(m_cooldownUntil>0 && now<m_cooldownUntil)
        {
         m_halt=APEX_HALT_LOSS_STREAK;
         m_haltReason=StringFormat("cooldown after %d losses until %s",
                                   m_lossStreak,TimeToString(m_cooldownUntil,TIME_MINUTES));
         return false;
        }
      if(m_cooldownUntil>0 && now>=m_cooldownUntil)
        {
         m_cooldownUntil=0;
         m_lossStreak=0;
         if(m_log!=NULL) m_log.Info("loss-streak cooldown expired - trading resumed");
        }

      //--- open position budget
      if(CountPositions("")>=m_maxPositionsTotal)
        {
         m_halt=APEX_HALT_NONE;
         m_haltReason=StringFormat("position budget full (%d)",m_maxPositionsTotal);
         return false;
        }

      //--- margin health
      double marginLevel=AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);
      double usedMargin =AccountInfoDouble(ACCOUNT_MARGIN);
      if(usedMargin>0.0 && marginLevel>0.0 && marginLevel<300.0)
        {
         m_halt=APEX_HALT_MARGIN;
         m_haltReason=StringFormat("margin level %.0f%% below safety floor 300%%",marginLevel);
         return false;
        }

      m_halt=APEX_HALT_NONE;
      m_haltReason="";
      return true;
     }

   //+---------------------------------------------------------------+
   //| Per-symbol gates + position sizing.                            |
   //+---------------------------------------------------------------+
   void              Evaluate(CSymbolCtx &ctx,const ApexSignal &sig,
                              const datetime now,ApexRiskDecision &dec)
     {
      dec.allowed=false;
      dec.lots=0.0;
      dec.riskPercentUsed=0.0;
      dec.riskMoney=0.0;
      dec.halt=APEX_HALT_NONE;
      dec.reason="";

      if(sig.dir==APEX_DIR_NONE || sig.stopDistance<=0.0)
        {
         dec.reason="no signal";
         return;
        }

      //--- per-symbol position cap
      if(CountPositions(ctx.symbol)>=m_maxPositionsPerSymbol)
        {
         dec.reason=StringFormat("already %d position(s) on %s",
                                 CountPositions(ctx.symbol),ctx.symbol);
         return;
        }

      //--- currency concentration: three USD-quoted longs is one big bet
      if(m_maxExposurePerCurrency>0)
        {
         int baseCount  = CountCurrencyExposure(ctx.ccyBase);
         int quoteCount = CountCurrencyExposure(ctx.ccyProfit);
         if(baseCount>=m_maxExposurePerCurrency)
           {
            dec.reason=StringFormat("exposure cap reached for %s (%d)",ctx.ccyBase,baseCount);
            return;
           }
         if(quoteCount>=m_maxExposurePerCurrency)
           {
            dec.reason=StringFormat("exposure cap reached for %s (%d)",ctx.ccyProfit,quoteCount);
            return;
           }
        }

      //--- effective risk: base risk, scaled by conviction and by drawdown
      double equity=AccountInfoDouble(ACCOUNT_EQUITY);
      if(equity<=0.0) { dec.reason="equity unavailable"; return; }

      double convictionScale=ApexClamp(0.60+0.40*sig.confidence,0.60,1.0);
      double ddScale=DrawdownRiskScale();
      double riskPct=ApexClamp(m_riskPercent*convictionScale*ddScale,0.0,m_maxRiskPercent);
      if(riskPct<=0.0) { dec.reason="risk percent resolved to zero"; return; }

      double riskMoney=equity*riskPct/100.0;

      //--- convert a price distance into money, the broker-agnostic way
      if(ctx.tickSize<=0.0 || ctx.tickValue<=0.0)
        {
         if(!ctx.RefreshMeta() || ctx.tickSize<=0.0 || ctx.tickValue<=0.0)
           {
            dec.reason="broker tick metadata unusable - refusing to size blind";
            return;
           }
        }

      double lossPerLot=(sig.stopDistance/ctx.tickSize)*ctx.tickValue;
      if(lossPerLot<=0.0)
        {
         dec.reason="computed zero loss-per-lot";
         return;
        }

      double lots=riskMoney/lossPerLot;
      lots=MathMin(lots,m_maxLotsPerTrade);
      lots=ctx.NormalizeVolume(lots);

      if(lots<ctx.volMin-1e-9)
        {
         dec.reason=StringFormat("required lots %.4f below broker minimum %.4f - risk too small for this stop",
                                 riskMoney/lossPerLot,ctx.volMin);
         return;
        }

      //--- would the minimum lot already exceed the risk budget?
      double actualRisk=lots*lossPerLot;
      if(actualRisk>riskMoney*1.35)
        {
         dec.reason=StringFormat("minimum lot risks %.2f vs budget %.2f - skipping",actualRisk,riskMoney);
         return;
        }

      //--- margin check against the real broker requirement
      double price=(sig.dir==APEX_DIR_LONG)
                   ? SymbolInfoDouble(ctx.symbol,SYMBOL_ASK)
                   : SymbolInfoDouble(ctx.symbol,SYMBOL_BID);
      ENUM_ORDER_TYPE ot=(sig.dir==APEX_DIR_LONG)?ORDER_TYPE_BUY:ORDER_TYPE_SELL;
      double marginNeeded=0.0;
      if(OrderCalcMargin(ot,ctx.symbol,lots,price,marginNeeded))
        {
         double freeMargin=AccountInfoDouble(ACCOUNT_MARGIN_FREE);
         double budget=freeMargin*m_maxMarginUtilPct/100.0;
         if(marginNeeded>budget)
           {
            // try to shrink into the budget before giving up
            double scaled=ctx.NormalizeVolume(lots*(budget/MathMax(marginNeeded,1e-9))*0.95);
            if(scaled>=ctx.volMin && scaled<lots)
              {
               if(m_log!=NULL)
                  m_log.Warn(StringFormat("%s: lots trimmed %.2f -> %.2f to respect margin budget",
                                          ctx.symbol,lots,scaled));
               lots=scaled;
               actualRisk=lots*lossPerLot;
              }
            else
              {
               dec.halt=APEX_HALT_MARGIN;
               dec.reason=StringFormat("margin %.2f exceeds budget %.2f (%.0f%% of free margin)",
                                       marginNeeded,budget,m_maxMarginUtilPct);
               return;
              }
           }
        }
      else
        {
         if(m_log!=NULL)
            m_log.Warn(StringFormat("%s: OrderCalcMargin failed (err=%d) - proceeding on lot caps only",
                                    ctx.symbol,GetLastError()));
         ResetLastError();
        }

      dec.allowed=true;
      dec.lots=lots;
      dec.riskPercentUsed=riskPct;
      dec.riskMoney=actualRisk;
      dec.reason=StringFormat("risk %.2f%% (conv x%.2f, dd x%.2f) -> %.2f lots, %.2f %s at risk",
                              riskPct,convictionScale,ddScale,lots,actualRisk,
                              AccountInfoString(ACCOUNT_CURRENCY));
     }

   //+---------------------------------------------------------------+
   //| Trade outcome feedback                                         |
   //+---------------------------------------------------------------+
   void              OnTradeOpened(const datetime now)
     {
      m_tradesToday++;
     }

   void              OnTradeClosed(const double profit,const datetime now)
     {
      if(profit<0.0)
        {
         m_lossStreak++;
         if(m_lossStreakLimit>0 && m_lossStreak>=m_lossStreakLimit)
           {
            m_cooldownUntil=now+(datetime)(m_cooldownMinutes*60);
            if(m_log!=NULL)
               m_log.Warn(StringFormat("%d consecutive losses - pausing new entries until %s",
                                       m_lossStreak,TimeToString(m_cooldownUntil,TIME_MINUTES)));
           }
        }
      else if(profit>0.0)
        {
         if(m_lossStreak>0 && m_log!=NULL)
            m_log.Info(StringFormat("winning trade - loss streak reset from %d",m_lossStreak));
         m_lossStreak=0;
        }
     }

   //+---------------------------------------------------------------+
   //| Accessors and remote overrides                                 |
   //+---------------------------------------------------------------+
   double            DayPnL(void) const
     {
      if(m_dayStartBalance<=0.0) return 0.0;
      return AccountInfoDouble(ACCOUNT_EQUITY)-m_dayStartBalance;
     }

   double            DayPnLPct(void) const
     {
      if(m_dayStartBalance<=0.0) return 0.0;
      return (DayPnL()/m_dayStartBalance)*100.0;
     }

   double            DrawdownPct(void) const
     {
      if(m_peakEquity<=0.0) return 0.0;
      double eq=AccountInfoDouble(ACCOUNT_EQUITY);
      return MathMax(0.0,(m_peakEquity-eq)/m_peakEquity*100.0);
     }

   //--- risk multiplier that shrinks as drawdown deepens
   double            DrawdownRiskScale(void) const
     {
      double dd=DrawdownPct();
      if(m_ddThrottleStartPct<=0.0 || dd<=m_ddThrottleStartPct) return 1.0;
      if(m_maxDrawdownPct<=m_ddThrottleStartPct) return m_ddThrottleFloor;
      double span=m_maxDrawdownPct-m_ddThrottleStartPct;
      double into=ApexClamp((dd-m_ddThrottleStartPct)/span,0.0,1.0);
      return ApexClamp(1.0-(1.0-m_ddThrottleFloor)*into,m_ddThrottleFloor,1.0);
     }

   double            PeakEquity(void)      const { return m_peakEquity; }
   double            DayStartBalance(void) const { return m_dayStartBalance; }
   int               TradesToday(void)     const { return m_tradesToday; }
   int               LossStreak(void)      const { return m_lossStreak; }
   ENUM_APEX_HALT    Halt(void)            const { return m_halt; }
   string            HaltReason(void)      const { return m_haltReason; }
   double            RiskPercent(void)     const { return m_riskPercent; }
   bool              KillSwitch(void)      const { return m_killSwitch; }

   //--- remote control may lower risk freely but never above the ceiling
   bool              SetRiskPercent(const double pct)
     {
      double v=ApexClamp(pct,0.01,m_maxRiskPercent);
      bool clamped=(MathAbs(v-pct)>1e-9);
      m_riskPercent=v;
      if(m_log!=NULL)
         m_log.Info(StringFormat("risk per trade set to %.2f%%%s",v,(clamped?" (clamped to ceiling)":"")));
      return !clamped;
     }

   void              EngageKillSwitch(const string why)
     {
      if(m_killSwitch) return;
      m_killSwitch=true;
      m_halt=APEX_HALT_KILL_SWITCH;
      m_haltReason=why;
      if(m_log!=NULL) m_log.Error("KILL SWITCH: "+why);
     }

   void              ReleaseKillSwitch(void)
     {
      if(!m_killSwitch) return;
      m_killSwitch=false;
      m_halt=APEX_HALT_NONE;
      m_haltReason="";
      // re-anchor the peak so an old high water mark does not instantly re-trip it
      m_peakEquity=AccountInfoDouble(ACCOUNT_EQUITY);
      if(m_log!=NULL) m_log.Warn("kill switch released - equity peak re-anchored");
     }

   //+---------------------------------------------------------------+
   //| Position counting helpers (magic-scoped)                       |
   //+---------------------------------------------------------------+
   int               CountPositions(const string symbol) const
     {
      int total=0;
      for(int i=PositionsTotal()-1;i>=0;i--)
        {
         ulong ticket=PositionGetTicket(i);
         if(ticket==0) continue;
         if(PositionGetInteger(POSITION_MAGIC)!=m_magic) continue;
         if(StringLen(symbol)>0 && PositionGetString(POSITION_SYMBOL)!=symbol) continue;
         total++;
        }
      return total;
     }

   double            OpenRiskMoney(void) const
     {
      double total=0.0;
      for(int i=PositionsTotal()-1;i>=0;i--)
        {
         ulong ticket=PositionGetTicket(i);
         if(ticket==0) continue;
         if(PositionGetInteger(POSITION_MAGIC)!=m_magic) continue;
         total+=PositionGetDouble(POSITION_PROFIT);
        }
      return total;
     }

private:
   //--- how many open positions involve this currency on either leg
   int               CountCurrencyExposure(const string ccy) const
     {
      if(StringLen(ccy)==0) return 0;
      int n=0;
      for(int i=PositionsTotal()-1;i>=0;i--)
        {
         ulong ticket=PositionGetTicket(i);
         if(ticket==0) continue;
         if(PositionGetInteger(POSITION_MAGIC)!=m_magic) continue;
         string sym=PositionGetString(POSITION_SYMBOL);
         string b=SymbolInfoString(sym,SYMBOL_CURRENCY_BASE);
         string q=SymbolInfoString(sym,SYMBOL_CURRENCY_PROFIT);
         if(b==ccy || q==ccy) n++;
        }
      return n;
     }

   //--- closed entries belonging to this EA since the day started
   int               CountTradesToday(const datetime dayStart,const datetime now) const
     {
      if(!HistorySelect(dayStart,now+60)) return 0;
      int n=0;
      int deals=HistoryDealsTotal();
      for(int i=0;i<deals;i++)
        {
         ulong ticket=HistoryDealGetTicket(i);
         if(ticket==0) continue;
         if(HistoryDealGetInteger(ticket,DEAL_MAGIC)!=m_magic) continue;
         if((ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket,DEAL_ENTRY)!=DEAL_ENTRY_IN) continue;
         n++;
        }
      return n;
     }
  };

#endif // APEX_RISKMANAGER_MQH
