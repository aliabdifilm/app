//+------------------------------------------------------------------+
//|                                              PositionManager.mqh |
//|  ApexAlgo - what happens AFTER the entry                         |
//|                                                                  |
//| Entries get all the attention; exits produce most of the result.  |
//| This module runs four independent, cooperating rules:             |
//|                                                                   |
//|   1. Break-even  - remove the risk once the trade has proved it.  |
//|   2. Partial TP  - bank a piece at a fixed R multiple.            |
//|   3. ATR trail   - let the rest run, tightening only, never       |
//|                    loosening (a widening stop is not a stop).     |
//|   4. Time stop   - a trade that has gone nowhere for N bars is    |
//|                    capital sitting in the wrong place.            |
//+------------------------------------------------------------------+

#ifndef APEX_POSITIONMANAGER_MQH
#define APEX_POSITIONMANAGER_MQH

#include "Defs.mqh"
#include "SymbolCtx.mqh"
#include "Executor.mqh"
#include "Logger.mqh"

//+------------------------------------------------------------------+
//| Per-position bookkeeping                                         |
//+------------------------------------------------------------------+
struct SPosState
  {
   ulong             ticket;
   double            initialRisk;    // price distance from entry to the original stop
   bool              beApplied;
   bool              partialTaken;
   datetime          openTime;
  };

//+------------------------------------------------------------------+
//| CPositionManager                                                 |
//+------------------------------------------------------------------+
class CPositionManager
  {
private:
   CApexLogger      *m_log;
   CExecutor        *m_exec;
   long              m_magic;

   //--- break-even
   bool              m_useBreakEven;
   double            m_beTriggerR;       // move to BE after this many R of profit
   double            m_beOffsetR;        // lock in this much R beyond entry

   //--- partial take profit
   bool              m_usePartial;
   double            m_partialTriggerR;
   double            m_partialPercent;   // % of the position to close

   //--- trailing
   bool              m_useTrailing;
   double            m_trailStartR;      // only start trailing after this much profit
   double            m_trailAtrMult;

   //--- time stop
   bool              m_useTimeStop;
   int               m_maxBarsInTrade;
   double            m_timeStopMinR;     // only time-out trades below this R

   SPosState         m_states[];

   int               FindState(const ulong ticket)
     {
      for(int i=ArraySize(m_states)-1;i>=0;i--)
         if(m_states[i].ticket==ticket) return i;
      return -1;
     }

   //--- has any partial close already happened on this position?
   bool              HasPartialInHistory(const ulong positionId)
     {
      if(!HistorySelectByPosition(positionId)) return false;
      int deals=HistoryDealsTotal();
      for(int i=0;i<deals;i++)
        {
         ulong d=HistoryDealGetTicket(i);
         if(d==0) continue;
         if((ENUM_DEAL_ENTRY)HistoryDealGetInteger(d,DEAL_ENTRY)==DEAL_ENTRY_OUT)
            return true;
        }
      return false;
     }

   //+---------------------------------------------------------------+
   //| Rebuild state for a position we have not seen before. After a  |
   //| terminal restart or a recompile the in-memory table is empty,  |
   //| so everything is re-derived from the position and its history. |
   //+---------------------------------------------------------------+
   int               EnsureState(const ulong ticket)
     {
      int idx=FindState(ticket);
      if(idx>=0) return idx;

      if(!PositionSelectByTicket(ticket)) return -1;

      double openPrice=PositionGetDouble(POSITION_PRICE_OPEN);
      double sl       =PositionGetDouble(POSITION_SL);
      bool   isLong   =((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY);
      ulong  posId    =(ulong)PositionGetInteger(POSITION_IDENTIFIER);

      SPosState st;
      st.ticket=ticket;
      st.openTime=(datetime)PositionGetInteger(POSITION_TIME);
      st.initialRisk=(sl>0.0)?MathAbs(openPrice-sl):0.0;
      st.beApplied=false;
      if(sl>0.0)
         st.beApplied = isLong ? (sl>=openPrice) : (sl<=openPrice);
      st.partialTaken=HasPartialInHistory(posId);

      int n=ArraySize(m_states);
      ArrayResize(m_states,n+1);
      m_states[n]=st;

      if(m_log!=NULL)
         m_log.Debug(StringFormat("adopted position #%I64u risk=%.5f be=%s partial=%s",
                                  ticket,st.initialRisk,
                                  (st.beApplied?"yes":"no"),(st.partialTaken?"yes":"no")));
      return n;
     }

   //--- drop entries for positions that no longer exist
   void              Prune(void)
     {
      int n=ArraySize(m_states);
      for(int i=n-1;i>=0;i--)
        {
         if(!PositionSelectByTicket(m_states[i].ticket))
           {
            for(int j=i;j<n-1;j++) m_states[j]=m_states[j+1];
            n--;
            ArrayResize(m_states,n);
           }
        }
     }

public:
                     CPositionManager(void)
     {
      m_log=NULL;
      m_exec=NULL;
      m_magic=0;
      m_useBreakEven=true;
      m_beTriggerR=1.0;
      m_beOffsetR=0.10;
      m_usePartial=true;
      m_partialTriggerR=1.0;
      m_partialPercent=50.0;
      m_useTrailing=true;
      m_trailStartR=1.2;
      m_trailAtrMult=2.0;
      m_useTimeStop=true;
      m_maxBarsInTrade=96;
      m_timeStopMinR=0.5;
      ArrayResize(m_states,0);
     }

   void              SetLogger(CApexLogger *l)  { m_log=l; }
   void              SetExecutor(CExecutor *e)  { m_exec=e; }
   void              SetMagic(const long m)     { m_magic=m; }

   void              ConfigBreakEven(const bool use,const double triggerR,const double offsetR)
     {
      m_useBreakEven=use;
      m_beTriggerR=triggerR;
      m_beOffsetR=offsetR;
     }

   void              ConfigPartial(const bool use,const double triggerR,const double percent)
     {
      m_usePartial=use;
      m_partialTriggerR=triggerR;
      m_partialPercent=ApexClamp(percent,1.0,95.0);
     }

   void              ConfigTrailing(const bool use,const double startR,const double atrMult)
     {
      m_useTrailing=use;
      m_trailStartR=startR;
      m_trailAtrMult=atrMult;
     }

   void              ConfigTimeStop(const bool use,const int maxBars,const double minR)
     {
      m_useTimeStop=use;
      m_maxBarsInTrade=maxBars;
      m_timeStopMinR=minR;
     }

   void              Register(const ulong ticket,const double initialRisk)
     {
      int idx=FindState(ticket);
      if(idx<0)
        {
         int n=ArraySize(m_states);
         ArrayResize(m_states,n+1);
         idx=n;
         m_states[idx].ticket=ticket;
        }
      m_states[idx].initialRisk=initialRisk;
      m_states[idx].beApplied=false;
      m_states[idx].partialTaken=false;
      m_states[idx].openTime=TimeCurrent();
     }

   //+---------------------------------------------------------------+
   //| Run every management rule over one symbol's open positions.    |
   //+---------------------------------------------------------------+
   void              Manage(CSymbolCtx &ctx,const double atr)
     {
      if(m_exec==NULL) return;
      Prune();

      for(int i=PositionsTotal()-1;i>=0;i--)
        {
         ulong ticket=PositionGetTicket(i);
         if(ticket==0) continue;
         if(PositionGetInteger(POSITION_MAGIC)!=m_magic) continue;
         if(PositionGetString(POSITION_SYMBOL)!=ctx.symbol) continue;

         int idx=EnsureState(ticket);
         if(idx<0) continue;

         if(!PositionSelectByTicket(ticket)) continue;

         bool   isLong    = ((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY);
         double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
         double curSl     = PositionGetDouble(POSITION_SL);
         double curTp     = PositionGetDouble(POSITION_TP);
         double volume    = PositionGetDouble(POSITION_VOLUME);

         MqlTick tick;
         if(!SymbolInfoTick(ctx.symbol,tick)) continue;
         double market = isLong ? tick.bid : tick.ask;   // the price we would exit at

         double risk = m_states[idx].initialRisk;
         if(risk<=0.0)
           {
            // no original stop recorded - fall back to ATR so the R maths still works
            risk = (atr>0.0) ? atr*2.0 : 0.0;
            if(risk<=0.0) continue;
            m_states[idx].initialRisk=risk;
           }

         double profit = isLong ? (market-openPrice) : (openPrice-market);
         double rMultiple = profit/risk;

         //--- 1. partial take profit -----------------------------
         if(m_usePartial && !m_states[idx].partialTaken && rMultiple>=m_partialTriggerR)
           {
            double closeVol=ctx.NormalizeVolume(volume*m_partialPercent/100.0);
            if(closeVol>=ctx.volMin && (volume-closeVol)>=ctx.volMin-1e-9)
              {
               if(m_exec.ClosePartial(ctx,ticket,closeVol,
                                      StringFormat("partial TP at %.2fR",rMultiple)))
                 {
                  m_states[idx].partialTaken=true;
                  if(!PositionSelectByTicket(ticket)) continue;
                  volume=PositionGetDouble(POSITION_VOLUME);
                 }
              }
            else
              {
               // position too small to split - mark done so we stop retrying
               m_states[idx].partialTaken=true;
              }
           }

         //--- 2. break-even ---------------------------------------
         if(m_useBreakEven && !m_states[idx].beApplied && rMultiple>=m_beTriggerR)
           {
            double offset = risk*m_beOffsetR;
            double beSl   = isLong ? (openPrice+offset) : (openPrice-offset);
            beSl = ctx.NormalizePrice(beSl);

            bool improves = (curSl<=0.0) ||
                            (isLong ? (beSl>curSl) : (beSl<curSl));
            if(improves && m_exec.ModifyPosition(ctx,ticket,beSl,curTp))
              {
               m_states[idx].beApplied=true;
               curSl=beSl;
               if(m_log!=NULL)
                  m_log.Info(StringFormat("%s #%I64u moved to break-even+%.2fR at %.2fR profit",
                                          ctx.symbol,ticket,m_beOffsetR,rMultiple));
              }
           }

         //--- 3. ATR trailing stop (tighten only) -----------------
         if(m_useTrailing && atr>0.0 && rMultiple>=m_trailStartR)
           {
            double trailDistance=atr*m_trailAtrMult;
            double candidate = isLong ? (market-trailDistance) : (market+trailDistance);
            candidate = ctx.NormalizePrice(candidate);

            bool tighter = (curSl<=0.0) ||
                           (isLong ? (candidate>curSl) : (candidate<curSl));

            // never trail into a worse-than-entry stop once break-even is set
            if(tighter)
              {
               if(m_exec.ModifyPosition(ctx,ticket,candidate,curTp))
                 {
                  curSl=candidate;
                  if(m_log!=NULL)
                     m_log.Debug(StringFormat("%s #%I64u trail -> %.*f (%.2fR)",
                                              ctx.symbol,ticket,ctx.digits,candidate,rMultiple));
                 }
              }
           }

         //--- 4. time stop ----------------------------------------
         if(m_useTimeStop && m_maxBarsInTrade>0)
           {
            int secs=PeriodSeconds(ctx.tfSignal);
            if(secs>0)
              {
               datetime opened=(datetime)PositionGetInteger(POSITION_TIME);
               int barsHeld=(int)((TimeCurrent()-opened)/secs);
               if(barsHeld>=m_maxBarsInTrade && rMultiple<m_timeStopMinR)
                 {
                  m_exec.ClosePosition(ctx,ticket,
                                       StringFormat("time stop: %d bars at %.2fR",barsHeld,rMultiple));
                  continue;
                 }
              }
           }
        }
     }

   //--- forget a closed position
   void              Forget(const ulong ticket)
     {
      int idx=FindState(ticket);
      if(idx<0) return;
      int n=ArraySize(m_states);
      for(int j=idx;j<n-1;j++) m_states[j]=m_states[j+1];
      ArrayResize(m_states,n-1);
     }

   int               Tracked(void) { return ArraySize(m_states); }
  };

#endif // APEX_POSITIONMANAGER_MQH
