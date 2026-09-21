//+------------------------------------------------------------------+
//|                                                RemoteControl.mqh |
//|  ApexAlgo - the bridge to the phone / browser control panel      |
//|                                                                  |
//| The MetaTrader mobile app cannot run an Expert Advisor. The only  |
//| honest way to "control the bot from a phone" is therefore:        |
//|                                                                   |
//|   EA (PC or VPS)  --HTTPS-->  control server  <--HTTPS--  phone   |
//|                                                                   |
//| The EA PUSHES its state and PULLS pending commands on a timer.    |
//| It never accepts inbound connections, so nothing has to be opened |
//| on the machine running the money.                                 |
//|                                                                   |
//| Design rule: the link is an accessory, never a dependency. If the |
//| server is unreachable the EA keeps trading on its local rules     |
//| (configurable), because a network hiccup must not leave positions |
//| unmanaged.                                                        |
//+------------------------------------------------------------------+

#ifndef APEX_REMOTECONTROL_MQH
#define APEX_REMOTECONTROL_MQH

#include "Defs.mqh"
#include "Json.mqh"
#include "Logger.mqh"

//+------------------------------------------------------------------+
//| A single instruction coming back from the control panel          |
//+------------------------------------------------------------------+
struct ApexCommand
  {
   long              id;
   string            type;     // pause | resume | close_all | close_symbol |
                               // set_risk | kill | release_kill | ping
   string            symbol;
   double            value;
  };

//+------------------------------------------------------------------+
//| CRemoteControl                                                   |
//+------------------------------------------------------------------+
class CRemoteControl
  {
private:
   CApexLogger      *m_log;
   bool              m_enabled;
   string            m_baseUrl;      // e.g. https://my-vps:8443
   string            m_token;
   string            m_botId;
   int               m_timeoutMs;
   ENUM_APEX_OFFLINE m_offlinePolicy;

   datetime          m_lastSuccess;
   int               m_consecutiveFailures;
   bool              m_warnedAllowlist;
   string            m_lastError;
   long              m_ackIds[];

   string            Url(const string path) const
     {
      string b=m_baseUrl;
      int n=StringLen(b);
      if(n>0 && StringGetCharacter(b,n-1)=='/')
         b=StringSubstr(b,0,n-1);
      return b+path;
     }

   //--- serialise the ids we already executed so the server can retire them
   string            AckArrayJson(void)
     {
      string s="[";
      int n=ArraySize(m_ackIds);
      for(int i=0;i<n;i++)
        {
         if(i>0) s+=",";
         s+=StringFormat("%I64d",m_ackIds[i]);
        }
      return s+"]";
     }

public:
                     CRemoteControl(void)
     {
      m_log=NULL;
      m_enabled=false;
      m_baseUrl="";
      m_token="";
      m_botId="apex-1";
      m_timeoutMs=4000;
      m_offlinePolicy=APEX_OFFLINE_KEEP_TRADING;
      m_lastSuccess=0;
      m_consecutiveFailures=0;
      m_warnedAllowlist=false;
      m_lastError="";
      ArrayResize(m_ackIds,0);
     }

   void              SetLogger(CApexLogger *l) { m_log=l; }

   void              Config(const bool enabled,const string baseUrl,const string token,
                            const string botId,const int timeoutMs,
                            const ENUM_APEX_OFFLINE offlinePolicy)
     {
      m_enabled=enabled;
      m_baseUrl=baseUrl;
      m_token=token;
      m_botId=botId;
      m_timeoutMs=MathMax(1000,timeoutMs);
      m_offlinePolicy=offlinePolicy;
     }

   bool              Enabled(void)            const { return m_enabled; }
   datetime          LastSuccess(void)        const { return m_lastSuccess; }
   int               Failures(void)           const { return m_consecutiveFailures; }
   string            LastError(void)          const { return m_lastError; }
   ENUM_APEX_OFFLINE OfflinePolicy(void)      const { return m_offlinePolicy; }

   //--- have we lost the link for longer than 'seconds'?
   bool              IsStale(const int seconds) const
     {
      if(!m_enabled) return false;
      if(m_lastSuccess==0) return (m_consecutiveFailures>3);
      return (TimeCurrent()-m_lastSuccess)>seconds;
     }

   void              Ack(const long id)
     {
      int n=ArraySize(m_ackIds);
      ArrayResize(m_ackIds,n+1);
      m_ackIds[n]=id;
     }

   //+---------------------------------------------------------------+
   //| Build the heartbeat body.                                      |
   //+---------------------------------------------------------------+
   string            BuildPayload(const ApexSnapshot &s,const string positionsJson,
                                   const string symbolsJson,const string version)
     {
      CJsonWriter j;
      j.Str ("bot_id",       m_botId);
      j.Str ("version",      version);
      j.Int ("ts",           (long)TimeCurrent());
      j.Int ("login",        s.login);
      j.Str ("server",       s.server);
      j.Str ("company",      s.company);
      j.Str ("currency",     s.currency);
      j.Num ("balance",      s.balance,2);
      j.Num ("equity",       s.equity,2);
      j.Num ("margin",       s.margin,2);
      j.Num ("free_margin",  s.freeMargin,2);
      j.Num ("margin_level", s.marginLevel,2);
      j.Num ("day_start_balance",s.dayStartBalance,2);
      j.Num ("day_pnl",      s.dayPnL,2);
      j.Num ("day_pnl_pct",  s.dayPnLPct,3);
      j.Num ("peak_equity",  s.peakEquity,2);
      j.Num ("drawdown_pct", s.drawdownPct,3);
      j.Int ("open_positions",s.openPositions);
      j.Int ("trades_today", s.tradesToday);
      j.Int ("loss_streak",  s.lossStreak);
      j.Bool("paused",       s.paused);
      j.Str ("halt",         ApexHaltToString(s.halt));
      j.Str ("halt_reason",  s.haltReason);
      j.Num ("risk_percent", s.riskPercent,3);
      j.Raw ("positions",    positionsJson);
      j.Raw ("symbols",      symbolsJson);
      j.Raw ("ack",          AckArrayJson());
      return j.Finish();
     }

   //+---------------------------------------------------------------+
   //| POST the heartbeat, parse the command list.                    |
   //| Returns true on a successful round trip.                       |
   //+---------------------------------------------------------------+
   bool              Poll(const string payload,ApexCommand &cmds[],bool &serverPaused)
     {
      ArrayResize(cmds,0);
      serverPaused=false;

      if(!m_enabled || StringLen(m_baseUrl)==0)
         return false;

      char   data[];
      char   result[];
      string resultHeaders="";

      int len=StringToCharArray(payload,data,0,WHOLE_ARRAY,CP_UTF8);
      // StringToCharArray appends a terminating zero - the server must not receive it
      if(len>0) ArrayResize(data,len-1);

      string headers="Content-Type: application/json\r\n";
      headers+="X-Apex-Token: "+m_token+"\r\n";
      headers+="X-Apex-Bot: "+m_botId+"\r\n";

      ResetLastError();
      int status=WebRequest("POST",Url("/api/heartbeat"),headers,m_timeoutMs,data,result,resultHeaders);

      if(status==-1)
        {
         int err=GetLastError();
         m_consecutiveFailures++;
         m_lastError=StringFormat("WebRequest failed err=%d",err);

         if(err==ERR_FUNCTION_NOT_ALLOWED && !m_warnedAllowlist)
           {
            m_warnedAllowlist=true;
            if(m_log!=NULL)
               m_log.Error("Remote control blocked. Add '"+m_baseUrl+
                           "' to Tools > Options > Expert Advisors > Allow WebRequest for listed URL.");
           }
         else if(m_consecutiveFailures==1 || m_consecutiveFailures%20==0)
           {
            if(m_log!=NULL)
               m_log.Warn(StringFormat("remote control unreachable (err=%d, %d consecutive failures)",
                                       err,m_consecutiveFailures));
           }
         return false;
        }

      if(status!=200)
        {
         m_consecutiveFailures++;
         m_lastError=StringFormat("HTTP %d",status);
         if(m_consecutiveFailures==1 || m_consecutiveFailures%20==0)
           {
            string body=CharArrayToString(result,0,MathMin(ArraySize(result),256),CP_UTF8);
            if(m_log!=NULL)
               m_log.Warn(StringFormat("remote control HTTP %d: %s",status,body));
           }
         return false;
        }

      //--- success ------------------------------------------------
      if(m_consecutiveFailures>0 && m_log!=NULL)
         m_log.Info(StringFormat("remote control link restored after %d failures",m_consecutiveFailures));

      m_consecutiveFailures=0;
      m_lastSuccess=TimeCurrent();
      m_lastError="";
      ArrayResize(m_ackIds,0);   // the server accepted our acks

      string body=CharArrayToString(result,0,ArraySize(result),CP_UTF8);
      serverPaused=JsonGetBool(body,"paused",false);

      string items[];
      int n=JsonSplitObjectArray(body,"commands",items);
      for(int i=0;i<n;i++)
        {
         ApexCommand c;
         c.id    =(long)JsonGetNumber(items[i],"id",0);
         c.type  =JsonGetString(items[i],"type","");
         c.symbol=JsonGetString(items[i],"symbol","");
         c.value =JsonGetNumber(items[i],"value",0.0);
         if(StringLen(c.type)==0) continue;

         int k=ArraySize(cmds);
         ArrayResize(cmds,k+1);
         cmds[k]=c;
        }

      if(n>0 && m_log!=NULL)
         m_log.Info(StringFormat("received %d remote command(s)",n));

      return true;
     }

   //+---------------------------------------------------------------+
   //| Fire-and-forget event notification (trade opened / closed /    |
   //| limit breached). Failures are ignored on purpose: a missed     |
   //| notification must never interfere with trading.                |
   //+---------------------------------------------------------------+
   void              Notify(const string level,const string title,const string message)
     {
      if(!m_enabled || StringLen(m_baseUrl)==0) return;
      if(m_consecutiveFailures>5) return;   // link is down, do not stall OnTick

      CJsonWriter j;
      j.Str("bot_id",m_botId);
      j.Str("level",level);
      j.Str("title",title);
      j.Str("message",message);
      j.Int("ts",(long)TimeCurrent());
      string payload=j.Finish();

      char   data[];
      char   result[];
      string resultHeaders="";
      int len=StringToCharArray(payload,data,0,WHOLE_ARRAY,CP_UTF8);
      if(len>0) ArrayResize(data,len-1);

      string headers="Content-Type: application/json\r\n";
      headers+="X-Apex-Token: "+m_token+"\r\n";
      headers+="X-Apex-Bot: "+m_botId+"\r\n";

      ResetLastError();
      int status=WebRequest("POST",Url("/api/event"),headers,2000,data,result,resultHeaders);
      if(status!=200 && m_log!=NULL)
         m_log.Debug(StringFormat("notify dropped (status=%d)",status));
     }
  };

#endif // APEX_REMOTECONTROL_MQH
