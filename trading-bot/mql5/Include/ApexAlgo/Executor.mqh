//+------------------------------------------------------------------+
//|                                                     Executor.mqh |
//|  ApexAlgo - order placement that survives real broker behaviour  |
//|                                                                  |
//| A backtest fills every order at the price you asked for. A live   |
//| account requotes, rejects your filling mode, refuses stops that   |
//| are one point too close, and occasionally just times out. This    |
//| class turns each of those into a specific, bounded recovery       |
//| action instead of a silent failed trade.                          |
//+------------------------------------------------------------------+

#ifndef APEX_EXECUTOR_MQH
#define APEX_EXECUTOR_MQH

#include <Trade/Trade.mqh>
#include "Defs.mqh"
#include "SymbolCtx.mqh"
#include "Logger.mqh"

//+------------------------------------------------------------------+
//| CExecutor                                                        |
//+------------------------------------------------------------------+
class CExecutor
  {
private:
   CTrade            m_trade;
   CApexLogger      *m_log;
   long              m_magic;
   int               m_slippagePoints;
   int               m_maxRetries;
   int               m_retryDelayMs;
   string            m_comment;

   //--- is this retcode worth another attempt?
   bool              IsRetryable(const uint rc) const
     {
      switch(rc)
        {
         case TRADE_RETCODE_REQUOTE:
         case TRADE_RETCODE_PRICE_CHANGED:
         case TRADE_RETCODE_PRICE_OFF:
         case TRADE_RETCODE_TIMEOUT:
         case TRADE_RETCODE_CONNECTION:
         case TRADE_RETCODE_TOO_MANY_REQUESTS:
         case TRADE_RETCODE_FROZEN:
            return true;
        }
      return false;
     }

   bool              IsSuccess(const uint rc) const
     {
      return (rc==TRADE_RETCODE_DONE ||
              rc==TRADE_RETCODE_PLACED ||
              rc==TRADE_RETCODE_DONE_PARTIAL);
     }

public:
                     CExecutor(void)
     {
      m_log=NULL;
      m_magic=0;
      m_slippagePoints=20;
      m_maxRetries=3;
      m_retryDelayMs=250;
      m_comment="ApexAlgo";
     }

   void              SetLogger(CApexLogger *l) { m_log=l; }

   void              Config(const long magic,const int slippagePoints,
                            const int maxRetries,const int retryDelayMs,
                            const string comment)
     {
      m_magic=magic;
      m_slippagePoints=slippagePoints;
      m_maxRetries=MathMax(1,maxRetries);
      m_retryDelayMs=MathMax(0,retryDelayMs);
      m_comment=comment;

      m_trade.SetExpertMagicNumber((ulong)magic);
      m_trade.SetDeviationInPoints((ulong)slippagePoints);
      m_trade.SetAsyncMode(false);
      m_trade.LogLevel(LOG_LEVEL_ERRORS);
     }

   CTrade           *Trade(void) { return GetPointer(m_trade); }

   //+---------------------------------------------------------------+
   //| Clamp a stop / target so the broker will actually accept it.   |
   //| Returns the adjusted price (0.0 means "send without it").      |
   //+---------------------------------------------------------------+
   double            SanitizeStop(CSymbolCtx &ctx,const double price,const double stopPrice,
                                   const bool isLong,const bool isStopLoss) const
     {
      if(stopPrice<=0.0) return 0.0;

      double minDist=ctx.MinStopDistance();
      if(minDist<=0.0) minDist=ctx.point*2.0;   // brokers reporting 0 still reject 0-distance stops

      double adjusted=stopPrice;

      if(isStopLoss)
        {
         if(isLong)
           {
            double maxAllowed=price-minDist;
            if(adjusted>maxAllowed) adjusted=maxAllowed;
           }
         else
           {
            double minAllowed=price+minDist;
            if(adjusted<minAllowed) adjusted=minAllowed;
           }
        }
      else
        {
         if(isLong)
           {
            double minAllowed=price+minDist;
            if(adjusted<minAllowed) adjusted=minAllowed;
           }
         else
           {
            double maxAllowed=price-minDist;
            if(adjusted>maxAllowed) adjusted=maxAllowed;
           }
        }

      return ctx.NormalizePrice(adjusted);
     }

   //+---------------------------------------------------------------+
   //| Open a market position with stop and target.                   |
   //+---------------------------------------------------------------+
   bool              OpenPosition(CSymbolCtx &ctx,const ENUM_APEX_DIR dir,
                                   double lots,const double stopDistance,
                                   const double targetDistance,
                                   const string tag,ulong &ticketOut)
     {
      ticketOut=0;
      if(dir==APEX_DIR_NONE || lots<=0.0) return false;

      bool isLong=(dir==APEX_DIR_LONG);
      ENUM_ORDER_TYPE_FILLING filling=ctx.filling;
      double volume=ctx.NormalizeVolume(lots);

      for(int attempt=1;attempt<=m_maxRetries;attempt++)
        {
         MqlTick tick;
         if(!SymbolInfoTick(ctx.symbol,tick))
           {
            if(m_log!=NULL) m_log.Error(ctx.symbol+": no tick available, aborting entry");
            return false;
           }

         double price=isLong?tick.ask:tick.bid;
         if(price<=0.0)
           {
            if(m_log!=NULL) m_log.Error(ctx.symbol+": zero price from broker, aborting entry");
            return false;
           }

         double rawSl=isLong?(price-stopDistance):(price+stopDistance);
         double rawTp=(targetDistance>0.0)
                      ? (isLong?(price+targetDistance):(price-targetDistance))
                      : 0.0;

         double sl=SanitizeStop(ctx,price,rawSl,isLong,true);
         double tp=SanitizeStop(ctx,price,rawTp,isLong,false);

         m_trade.SetTypeFilling(filling);
         m_trade.SetDeviationInPoints((ulong)m_slippagePoints);

         string comment=StringFormat("%s|%s",m_comment,tag);
         if(StringLen(comment)>31) comment=StringSubstr(comment,0,31);

         bool sent = isLong
                     ? m_trade.Buy (volume,ctx.symbol,price,sl,tp,comment)
                     : m_trade.Sell(volume,ctx.symbol,price,sl,tp,comment);

         uint rc=m_trade.ResultRetcode();

         if(sent && IsSuccess(rc))
           {
            ticketOut=m_trade.ResultOrder();
            ctx.consecutiveErrors=0;
            if(m_log!=NULL)
               m_log.Info(StringFormat("%s %s %.2f lots @ %.*f sl=%.*f tp=%.*f [%s] attempt=%d",
                                       ctx.symbol,(isLong?"BUY":"SELL"),volume,
                                       ctx.digits,m_trade.ResultPrice(),
                                       ctx.digits,sl,ctx.digits,tp,tag,attempt));
            return true;
           }

         //--- targeted recovery per retcode ------------------------
         if(rc==TRADE_RETCODE_INVALID_FILL)
           {
            ENUM_ORDER_TYPE_FILLING next=filling;
            if(filling==ORDER_FILLING_IOC)      next=ORDER_FILLING_FOK;
            else if(filling==ORDER_FILLING_FOK) next=ORDER_FILLING_RETURN;
            else                                next=ORDER_FILLING_IOC;
            if(m_log!=NULL)
               m_log.Warn(StringFormat("%s: filling mode rejected (10030), switching %d -> %d",
                                       ctx.symbol,(int)filling,(int)next));
            filling=next;
            ctx.filling=next;   // remember for next time
            continue;
           }

         if(rc==TRADE_RETCODE_INVALID_STOPS)
           {
            if(m_log!=NULL)
               m_log.Warn(ctx.symbol+": stops rejected (10016), refreshing broker levels and widening");
            ctx.RefreshMeta();
            ctx.stopsLevel=(int)MathMax(ctx.stopsLevel*2,10);
            continue;
           }

         if(rc==TRADE_RETCODE_INVALID_VOLUME)
           {
            ctx.RefreshMeta();
            double fixed=ctx.NormalizeVolume(volume);
            if(m_log!=NULL)
               m_log.Warn(StringFormat("%s: volume rejected (10014) %.4f -> %.4f",ctx.symbol,volume,fixed));
            if(MathAbs(fixed-volume)<1e-9) return false;
            volume=fixed;
            continue;
           }

         if(rc==TRADE_RETCODE_NO_MONEY)
           {
            double reduced=ctx.NormalizeVolume(volume*0.5);
            if(reduced>=ctx.volMin && reduced<volume)
              {
               if(m_log!=NULL)
                  m_log.Warn(StringFormat("%s: not enough money, halving %.2f -> %.2f",ctx.symbol,volume,reduced));
               volume=reduced;
               continue;
              }
            if(m_log!=NULL) m_log.Error(ctx.symbol+": not enough money even at minimum volume");
            return false;
           }

         if(rc==TRADE_RETCODE_MARKET_CLOSED || rc==TRADE_RETCODE_TRADE_DISABLED)
           {
            if(m_log!=NULL)
               m_log.Warn(StringFormat("%s: market closed or trading disabled (rc=%u)",ctx.symbol,rc));
            return false;
           }

         if(IsRetryable(rc))
           {
            if(m_log!=NULL)
               m_log.Warn(StringFormat("%s: retryable rc=%u (%s), attempt %d/%d",
                                       ctx.symbol,rc,m_trade.ResultRetcodeDescription(),
                                       attempt,m_maxRetries));
            if(m_retryDelayMs>0) Sleep(m_retryDelayMs*attempt);
            continue;
           }

         //--- anything else is a hard failure
         ctx.consecutiveErrors++;
         if(m_log!=NULL)
            m_log.Error(StringFormat("%s: entry failed rc=%u (%s)",
                                     ctx.symbol,rc,m_trade.ResultRetcodeDescription()));
         return false;
        }

      ctx.consecutiveErrors++;
      if(m_log!=NULL) m_log.Error(ctx.symbol+": entry abandoned after all retries");
      return false;
     }

   //+---------------------------------------------------------------+
   //| Modify an open position's stop / target.                       |
   //+---------------------------------------------------------------+
   bool              ModifyPosition(CSymbolCtx &ctx,const ulong ticket,
                                     const double newSl,const double newTp)
     {
      if(!PositionSelectByTicket(ticket)) return false;

      double curSl=PositionGetDouble(POSITION_SL);
      double curTp=PositionGetDouble(POSITION_TP);
      bool isLong=((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY);

      MqlTick tick;
      if(!SymbolInfoTick(ctx.symbol,tick)) return false;
      double ref=isLong?tick.bid:tick.ask;

      double sl=(newSl>0.0)?SanitizeStop(ctx,ref,newSl,isLong,true) :curSl;
      double tp=(newTp>0.0)?SanitizeStop(ctx,ref,newTp,isLong,false):curTp;

      // nothing meaningful changed -> do not spam the server
      if(MathAbs(sl-curSl)<ctx.point*0.5 && MathAbs(tp-curTp)<ctx.point*0.5)
         return true;

      // respect the freeze level: brokers reject modifications too close to price
      if(ctx.freezeLevel>0)
        {
         double freeze=(double)ctx.freezeLevel*ctx.point;
         if(MathAbs(ref-sl)<freeze) return true;
        }

      for(int attempt=1;attempt<=m_maxRetries;attempt++)
        {
         if(m_trade.PositionModify(ticket,sl,tp))
            return true;

         uint rc=m_trade.ResultRetcode();
         if(rc==TRADE_RETCODE_INVALID_STOPS || rc==TRADE_RETCODE_FROZEN)
           {
            ctx.RefreshMeta();
            return false;   // the next tick will try again with fresh levels
           }
         if(!IsRetryable(rc))
           {
            if(m_log!=NULL)
               m_log.Warn(StringFormat("%s #%I64u: modify failed rc=%u (%s)",
                                       ctx.symbol,ticket,rc,m_trade.ResultRetcodeDescription()));
            return false;
           }
         if(m_retryDelayMs>0) Sleep(m_retryDelayMs);
        }
      return false;
     }

   //+---------------------------------------------------------------+
   //| Close (fully or partially) with retries.                       |
   //+---------------------------------------------------------------+
   bool              ClosePosition(CSymbolCtx &ctx,const ulong ticket,const string why)
     {
      for(int attempt=1;attempt<=m_maxRetries;attempt++)
        {
         if(!PositionSelectByTicket(ticket)) return true;   // already gone

         m_trade.SetTypeFilling(ctx.filling);
         if(m_trade.PositionClose(ticket,(ulong)m_slippagePoints))
           {
            if(m_log!=NULL)
               m_log.Info(StringFormat("%s #%I64u closed (%s)",ctx.symbol,ticket,why));
            return true;
           }

         uint rc=m_trade.ResultRetcode();
         if(rc==TRADE_RETCODE_INVALID_FILL)
           {
            ctx.filling=(ctx.filling==ORDER_FILLING_IOC)?ORDER_FILLING_FOK:ORDER_FILLING_IOC;
            continue;
           }
         if(!IsRetryable(rc))
           {
            if(m_log!=NULL)
               m_log.Error(StringFormat("%s #%I64u close failed rc=%u (%s)",
                                        ctx.symbol,ticket,rc,m_trade.ResultRetcodeDescription()));
            return false;
           }
         if(m_retryDelayMs>0) Sleep(m_retryDelayMs*attempt);
        }
      return false;
     }

   bool              ClosePartial(CSymbolCtx &ctx,const ulong ticket,const double volume,const string why)
     {
      if(!PositionSelectByTicket(ticket)) return false;

      double posVol=PositionGetDouble(POSITION_VOLUME);
      double vol=ctx.NormalizeVolume(volume);

      // closing so much that the remainder would be untradeable -> close it all
      double remainder=posVol-vol;
      if(vol>=posVol-1e-9 || remainder<ctx.volMin-1e-9)
         return ClosePosition(ctx,ticket,why+" (full, remainder below min lot)");

      for(int attempt=1;attempt<=m_maxRetries;attempt++)
        {
         m_trade.SetTypeFilling(ctx.filling);
         if(m_trade.PositionClosePartial(ticket,vol,(ulong)m_slippagePoints))
           {
            if(m_log!=NULL)
               m_log.Info(StringFormat("%s #%I64u partial close %.2f of %.2f (%s)",
                                       ctx.symbol,ticket,vol,posVol,why));
            return true;
           }

         uint rc=m_trade.ResultRetcode();
         if(rc==TRADE_RETCODE_INVALID_FILL)
           {
            ctx.filling=(ctx.filling==ORDER_FILLING_IOC)?ORDER_FILLING_FOK:ORDER_FILLING_IOC;
            continue;
           }
         if(!IsRetryable(rc))
           {
            if(m_log!=NULL)
               m_log.Warn(StringFormat("%s #%I64u partial close failed rc=%u (%s)",
                                       ctx.symbol,ticket,rc,m_trade.ResultRetcodeDescription()));
            return false;
           }
         if(m_retryDelayMs>0) Sleep(m_retryDelayMs*attempt);
        }
      return false;
     }

   //+---------------------------------------------------------------+
   //| Close everything belonging to this EA. Used by the remote      |
   //| panic button, the weekend rule and the kill switch.            |
   //+---------------------------------------------------------------+
   int               CloseAll(const string symbolFilter,const string why)
     {
      int closed=0;
      for(int i=PositionsTotal()-1;i>=0;i--)
        {
         ulong ticket=PositionGetTicket(i);
         if(ticket==0) continue;
         if(PositionGetInteger(POSITION_MAGIC)!=m_magic) continue;
         string sym=PositionGetString(POSITION_SYMBOL);
         if(StringLen(symbolFilter)>0 && sym!=symbolFilter) continue;

         CSymbolCtx tmp;
         tmp.symbol=sym;
         tmp.RefreshMeta();

         if(ClosePosition(tmp,ticket,why)) closed++;
        }
      if(closed>0 && m_log!=NULL)
         m_log.Warn(StringFormat("closed %d position(s): %s",closed,why));
      return closed;
     }
  };

#endif // APEX_EXECUTOR_MQH
