//+------------------------------------------------------------------+
//|                                                   ApexAlgoEA.mq5 |
//|                              ApexAlgo - autonomous trading robot |
//|                                                                  |
//| A regime-aware, multi-symbol Expert Advisor with a hard risk      |
//| engine and an optional remote control link for phone/browser.     |
//|                                                                  |
//| READ THIS FIRST                                                   |
//| No trading robot can guarantee profit. This one is built to be    |
//| survivable and auditable: every entry is risk-sized, every        |
//| position carries a server-side stop, and every limit is enforced  |
//| before an order is sent. Validate it on the Strategy Tester and   |
//| then on a demo account before it ever sees real money.            |
//+------------------------------------------------------------------+
#property copyright "ApexAlgo"
#property version   "1.00"
#property description "Regime-aware multi-strategy EA with hard risk controls and remote control."
#property description "Trend (Donchian + pullback + momentum) and Range (Bollinger + RSI) ensembles."
#property description "Mobile/browser control via the companion ApexAlgo control server."

#include <ApexAlgo/Defs.mqh>
#include <ApexAlgo/Logger.mqh>
#include <ApexAlgo/Json.mqh>
#include <ApexAlgo/SymbolCtx.mqh>
#include <ApexAlgo/MarketView.mqh>
#include <ApexAlgo/Filters.mqh>
#include <ApexAlgo/Signals.mqh>
#include <ApexAlgo/RiskManager.mqh>
#include <ApexAlgo/Executor.mqh>
#include <ApexAlgo/PositionManager.mqh>
#include <ApexAlgo/RemoteControl.mqh>
#include <ApexAlgo/Dashboard.mqh>

//+------------------------------------------------------------------+
//| INPUTS                                                           |
//+------------------------------------------------------------------+
input group "=== General ==="
input long              InpMagic              = 20260921;   // Magic number (unique per EA instance)
input string            InpSymbols            = "";         // Symbols, comma separated (empty = chart symbol)
input ENUM_TIMEFRAMES   InpSignalTF           = PERIOD_M15; // Signal timeframe
input ENUM_TIMEFRAMES   InpBiasTF             = PERIOD_H4;  // Higher timeframe bias
input string            InpTradeComment       = "ApexAlgo"; // Order comment
input ENUM_APEX_LOGLEVEL InpLogLevel          = APEX_LOG_INFO; // Log level
input bool              InpLogToFile          = true;       // Write a CSV audit log

input group "=== Strategy ==="
input ENUM_APEX_STRATMODE InpStrategyMode     = APEX_STRAT_AUTO; // Strategy selection
input bool              InpRequireBias        = true;       // Trend trades must agree with higher timeframe
input double            InpMinScore           = 0.45;       // Minimum ensemble score to trade (0..1)
input int               InpAtrPeriod          = 14;         // ATR period
input int               InpAdxPeriod          = 14;         // ADX period
input int               InpRsiPeriod          = 14;         // RSI period
input int               InpBBPeriod           = 20;         // Bollinger period
input double            InpBBDev              = 2.0;        // Bollinger deviation
input int               InpEmaFast            = 21;         // Fast EMA (signal TF)
input int               InpEmaSlow            = 55;         // Slow EMA (signal TF)
input int               InpBiasEmaFast        = 50;         // Fast EMA (bias TF)
input int               InpBiasEmaSlow        = 200;        // Slow EMA (bias TF)
input int               InpDonchianPeriod     = 20;         // Donchian breakout period
input int               InpErPeriod           = 20;         // Efficiency ratio period
input int               InpAtrAvgPeriod       = 100;        // ATR average / percentile window

input group "=== Regime thresholds ==="
input double            InpAdxTrendMin        = 23.0;       // ADX above this = trending
input double            InpAdxRangeMax        = 18.0;       // ADX below this = ranging
input double            InpErTrendMin         = 0.30;       // Efficiency ratio above this = trending
input double            InpErRangeMax         = 0.22;       // Efficiency ratio below this = ranging
input double            InpRsiOversold        = 30.0;       // RSI oversold
input double            InpRsiOverbought      = 70.0;       // RSI overbought
input int               InpPullbackLookback   = 5;          // Bars to look back for a pullback

input group "=== Stop / target geometry ==="
input double            InpAtrStopTrend       = 2.0;        // Stop = ATR x this (trend trades)
input double            InpAtrStopRange       = 1.5;        // Stop = ATR x this (range trades)
input double            InpRRTrend            = 2.0;        // Reward:risk target (trend)
input double            InpRRRange            = 1.2;        // Reward:risk target (range)

input group "=== Risk (hard limits) ==="
input double            InpRiskPercent        = 0.5;        // Risk per trade, % of equity
input double            InpMaxRiskPercent     = 2.0;        // Ceiling remote control cannot exceed
input double            InpDailyLossLimitPct  = 3.0;        // Stop for the day at this % loss
input double            InpMaxDrawdownPct     = 10.0;       // Kill switch at this % drawdown from peak
input double            InpDDThrottleStartPct = 4.0;        // Start reducing risk at this drawdown
input double            InpDDThrottleFloor    = 0.35;       // Minimum risk multiplier when throttled
input int               InpMaxPositionsTotal  = 4;          // Max simultaneous positions
input int               InpMaxPositionsPerSym = 1;          // Max positions per symbol
input int               InpMaxTradesPerDay    = 10;         // Max new trades per day
input int               InpLossStreakLimit    = 3;          // Consecutive losses before cooldown
input int               InpCooldownMinutes    = 120;        // Cooldown length in minutes
input double            InpMaxLotsPerTrade    = 5.0;        // Absolute lot ceiling
input double            InpMaxMarginUtilPct   = 20.0;       // Max % of free margin per trade
input int               InpMaxExposurePerCcy  = 2;          // Max open positions per currency

input group "=== Trade management ==="
input bool              InpUseBreakEven       = true;       // Move stop to break-even
input double            InpBeTriggerR         = 1.0;        // Break-even after this many R
input double            InpBeOffsetR          = 0.10;       // Lock in this many R past entry
input bool              InpUsePartial         = true;       // Take partial profit
input double            InpPartialTriggerR    = 1.0;        // Partial close at this many R
input double            InpPartialPercent     = 50.0;       // % of position to close
input bool              InpUseTrailing        = true;       // ATR trailing stop
input double            InpTrailStartR        = 1.2;        // Start trailing after this many R
input double            InpTrailAtrMult       = 2.0;        // Trail distance = ATR x this
input bool              InpUseTimeStop        = true;       // Close stagnant trades
input int               InpMaxBarsInTrade     = 96;         // Max bars before time stop
input double            InpTimeStopMinR       = 0.5;        // Only time-stop trades below this R

input group "=== Filters ==="
input double            InpMaxSpreadAtr       = 0.25;       // Reject if spread > this x ATR
input int               InpMaxSpreadPoints    = 0;          // Hard spread ceiling in points (0 = off)
input double            InpMinAtrPoints       = 0.0;        // Minimum ATR in points (0 = off)
input double            InpMaxAtrMultiple     = 4.0;        // Reject if ATR > this x average (0 = off)
input int               InpMinBarsBetweenTrd  = 3;          // Minimum bars between entries per symbol
input bool              InpUseSessions        = false;      // Restrict to trading sessions
input int               InpSess1Start         = 7;          // Session 1 start hour (server time)
input int               InpSess1End           = 16;         // Session 1 end hour
input int               InpSess2Start         = 0;          // Session 2 start hour (0 = unused)
input int               InpSess2End           = 0;          // Session 2 end hour
input bool              InpTradeSunday        = false;      // Trade Sunday (crypto/CFD only)
input bool              InpTradeMonday        = true;       // Trade Monday
input bool              InpTradeTuesday       = true;       // Trade Tuesday
input bool              InpTradeWednesday     = true;       // Trade Wednesday
input bool              InpTradeThursday      = true;       // Trade Thursday
input bool              InpTradeFriday        = true;       // Trade Friday
input bool              InpTradeSaturday      = false;      // Trade Saturday (crypto/CFD only)
input bool              InpWeekendFlat        = true;       // Flatten before the weekend
input int               InpFridayCloseHour    = 20;         // Stop and flatten from this hour on Friday
input int               InpMondayOpenHour     = 1;          // Do not trade before this hour on Monday
input bool              InpUseNewsFilter      = true;       // Block around high-impact news
input int               InpNewsMinutesBefore  = 30;         // Blackout minutes before news
input int               InpNewsMinutesAfter   = 30;         // Blackout minutes after news
input int               InpNewsMinImportance  = 3;          // 1=low 2=moderate 3=high

input group "=== Execution ==="
input int               InpSlippagePoints     = 20;         // Max deviation in points
input int               InpMaxRetries         = 3;          // Order retry attempts
input int               InpRetryDelayMs       = 250;        // Delay between retries (ms)

input group "=== Remote control (phone / browser) ==="
input bool              InpRemoteEnabled      = false;      // Enable the control server link
input string            InpRemoteUrl          = "http://127.0.0.1:8800"; // Control server base URL
input string            InpRemoteToken        = "";         // Shared secret token
input string            InpBotId              = "apex-1";   // This bot's id on the panel
input int               InpRemoteTimeoutMs    = 3000;       // HTTP timeout (ms)
input int               InpRemotePollSeconds  = 10;         // Heartbeat interval (seconds)
input int               InpRemoteStaleSeconds = 180;        // Link considered dead after this
input ENUM_APEX_OFFLINE InpOfflinePolicy      = APEX_OFFLINE_KEEP_TRADING; // If the server is unreachable

input group "=== Display ==="
input bool              InpShowPanel          = true;       // Show the on-chart panel
input int               InpPanelX             = 12;         // Panel X offset
input int               InpPanelY             = 22;         // Panel Y offset

//+------------------------------------------------------------------+
//| GLOBALS                                                          |
//+------------------------------------------------------------------+
CApexLogger       g_log;
CMarketView       g_view;
CFilters          g_filters;
CSignalEngine     g_signals;
CRiskManager      g_risk;
CExecutor         g_exec;
CPositionManager  g_posmgr;
CRemoteControl    g_remote;
CDashboard        g_hud;

CSymbolCtx        g_ctx[APEX_MAX_SYMBOLS];
int               g_symbolCount = 0;

SMarketView       g_mv;

bool              g_paused        = false;   // set by the remote panel
bool              g_initialised   = false;
datetime          g_lastHeartbeat = 0;
datetime          g_lastHudDraw   = 0;
string            g_regimeLine    = "regime: warming up";
string            g_lastNotes[APEX_MAX_SYMBOLS];

//+------------------------------------------------------------------+
//| Forward declarations                                             |
//+------------------------------------------------------------------+
bool   SetupSymbols(void);
void   ApplyConfiguration(void);
void   ProcessSymbol(const int idx,const datetime now);
void   ManageOpenPositions(void);
void   DoHeartbeat(void);
void   ApplyCommand(const ApexCommand &cmd);
void   BuildSnapshot(ApexSnapshot &s);
string BuildPositionsJson(void);
string BuildSymbolsJson(void);
bool   ValidateInputs(string &problem);
bool   SignalTfExceedsBiasTf(void);

//+------------------------------------------------------------------+
//| OnInit                                                           |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_log.Init("ApexAlgo",InpLogLevel,InpLogToFile);
   g_log.Info("=============================================");
   g_log.Info(StringFormat("ApexAlgo v%s starting - account %I64d @ %s",
                           APEX_VERSION,
                           AccountInfoInteger(ACCOUNT_LOGIN),
                           AccountInfoString(ACCOUNT_SERVER)));

   string problem="";
   if(!ValidateInputs(problem))
     {
      g_log.Error("configuration rejected: "+problem);
      Alert("ApexAlgo: "+problem);
      return INIT_PARAMETERS_INCORRECT;
     }

   if(!SetupSymbols())
     {
      g_log.Error("no tradable symbol could be initialised");
      return INIT_FAILED;
     }

   ApplyConfiguration();

   //--- baseline the risk engine before the first tick
   g_risk.Refresh(TimeCurrent());

   if(!EventSetTimer(MathMax(1,InpRemotePollSeconds)))
      g_log.Warn("EventSetTimer failed - remote control and panel will update on ticks only");

   if(InpRemoteEnabled)
     {
      if(StringLen(InpRemoteToken)<8)
         g_log.Warn("remote token is very short - use a long random secret");
      g_log.Info("remote control enabled -> "+InpRemoteUrl+
                 "  (must be in Tools > Options > Expert Advisors > allowed URLs)");
     }

   if(!AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
      g_log.Warn("the broker reports algo trading is NOT allowed on this account");
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      g_log.Warn("'Algo Trading' is OFF in the terminal toolbar - no order will be sent");

   g_initialised=true;
   g_log.Info(StringFormat("initialised with %d symbol(s), magic=%I64d, risk=%.2f%%",
                           g_symbolCount,InpMagic,InpRiskPercent));
   g_log.Info("=============================================");

   if(InpShowPanel)
     {
      ApexSnapshot s;
      BuildSnapshot(s);
      g_hud.Render(s,APEX_VERSION,g_regimeLine,InpRemoteEnabled,false,"starting",g_posmgr.Tracked());
     }

   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
//| OnDeinit                                                         |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   g_hud.Destroy();

   for(int i=0;i<g_symbolCount;i++)
      g_ctx[i].Release();

   g_log.Info(StringFormat("ApexAlgo stopped (reason=%d). Open positions are NOT closed on shutdown - "
                           "their server-side stops remain active.",reason));
   g_log.Close();
  }

//+------------------------------------------------------------------+
//| OnTick                                                           |
//+------------------------------------------------------------------+
void OnTick()
  {
   if(!g_initialised) return;

   datetime now=TimeCurrent();
   g_risk.Refresh(now);

   //--- trade management runs on EVERY tick: trailing stops and
   //--- break-even must react to price, not to bar closes.
   ManageOpenPositions();

   //--- weekend flat rule
   if(g_filters.MustFlattenForWeekend(now))
     {
      if(g_risk.CountPositions("")>0)
        {
         int n=g_exec.CloseAll("","weekend flat rule");
         if(n>0) g_remote.Notify("warn","Weekend flat",
                                 StringFormat("Closed %d position(s) before the weekend.",n));
        }
      return;
     }

   //--- kill switch: flatten once, then stand down
   if(g_risk.KillSwitch())
     {
      if(g_risk.CountPositions("")>0)
        {
         int n=g_exec.CloseAll("","kill switch / max drawdown");
         if(n>0) g_remote.Notify("error","Kill switch",
                                 StringFormat("Max drawdown breached. Closed %d position(s).",n));
        }
      return;
     }

   if(g_paused) return;

   //--- portfolio-level gates
   if(!g_risk.PortfolioAllowed(now)) return;

   //--- remote link policy
   if(InpRemoteEnabled && g_remote.IsStale(InpRemoteStaleSeconds))
     {
      if(InpOfflinePolicy==APEX_OFFLINE_NO_NEW_TRADES) return;
      if(InpOfflinePolicy==APEX_OFFLINE_FLATTEN)
        {
         if(g_risk.CountPositions("")>0)
            g_exec.CloseAll("","control server unreachable (offline policy = flatten)");
         return;
        }
     }

   //--- time-of-day / weekday gate
   string timeReason="";
   if(!g_filters.TimeAllowed(now,timeReason)) return;

   //--- per-symbol work, only on a new completed bar
   for(int i=0;i<g_symbolCount;i++)
     {
      if(!g_ctx[i].valid) continue;
      if(!g_ctx[i].IsNewBar()) continue;
      ProcessSymbol(i,now);
     }
  }

//+------------------------------------------------------------------+
//| OnTimer - remote heartbeat and panel refresh                     |
//+------------------------------------------------------------------+
void OnTimer()
  {
   if(!g_initialised) return;

   g_risk.Refresh(TimeCurrent());

   if(InpRemoteEnabled)
      DoHeartbeat();

   if(InpShowPanel)
     {
      ApexSnapshot s;
      BuildSnapshot(s);
      bool alive=(InpRemoteEnabled && !g_remote.IsStale(InpRemoteStaleSeconds));
      g_hud.Render(s,APEX_VERSION,g_regimeLine,InpRemoteEnabled,alive,
                   g_remote.LastError(),g_posmgr.Tracked());
     }
  }

//+------------------------------------------------------------------+
//| OnTradeTransaction - authoritative trade outcome feedback        |
//|                                                                  |
//| Polling the history for closes is unreliable. This event fires    |
//| exactly once per deal, which is what the loss-streak counter and  |
//| the notifications need.                                           |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(trans.type!=TRADE_TRANSACTION_DEAL_ADD) return;
   if(trans.deal==0) return;

   if(!HistoryDealSelect(trans.deal)) return;
   if(HistoryDealGetInteger(trans.deal,DEAL_MAGIC)!=InpMagic) return;

   ENUM_DEAL_ENTRY entry=(ENUM_DEAL_ENTRY)HistoryDealGetInteger(trans.deal,DEAL_ENTRY);
   string  sym    = HistoryDealGetString(trans.deal,DEAL_SYMBOL);
   double  volume = HistoryDealGetDouble(trans.deal,DEAL_VOLUME);
   double  price  = HistoryDealGetDouble(trans.deal,DEAL_PRICE);

   if(entry==DEAL_ENTRY_IN)
     {
      g_risk.OnTradeOpened(TimeCurrent());
      g_remote.Notify("info","Trade opened",
                      StringFormat("%s %.2f lots @ %.5f",sym,volume,price));
      return;
     }

   if(entry==DEAL_ENTRY_OUT || entry==DEAL_ENTRY_OUT_BY)
     {
      double profit = HistoryDealGetDouble(trans.deal,DEAL_PROFIT)
                    + HistoryDealGetDouble(trans.deal,DEAL_SWAP)
                    + HistoryDealGetDouble(trans.deal,DEAL_COMMISSION);

      g_risk.OnTradeClosed(profit,TimeCurrent());

      g_log.Info(StringFormat("closed %s %.2f lots @ %.5f  net=%.2f %s  (streak=%d)",
                              sym,volume,price,profit,
                              AccountInfoString(ACCOUNT_CURRENCY),g_risk.LossStreak()));

      g_remote.Notify((profit>=0.0?"info":"warn"),"Trade closed",
                      StringFormat("%s %.2f lots, net %.2f %s",
                                   sym,volume,profit,AccountInfoString(ACCOUNT_CURRENCY)));
     }
  }

//+------------------------------------------------------------------+
//| Symbol setup                                                     |
//+------------------------------------------------------------------+
bool SetupSymbols(void)
  {
   string list=InpSymbols;
   StringTrimLeft(list);
   StringTrimRight(list);
   if(StringLen(list)==0) list=_Symbol;

   string names[];
   int n=ApexSplitCsv(list,names);
   if(n<=0)
     {
      ArrayResize(names,1);
      names[0]=_Symbol;
      n=1;
     }

   g_symbolCount=0;
   for(int i=0;i<n && g_symbolCount<APEX_MAX_SYMBOLS;i++)
     {
      if(!g_ctx[g_symbolCount].Init(names[i],InpSignalTF,InpBiasTF,
                                    InpAtrPeriod,InpAdxPeriod,InpRsiPeriod,
                                    InpBBPeriod,InpBBDev,
                                    InpEmaFast,InpEmaSlow,
                                    InpBiasEmaFast,InpBiasEmaSlow))
        {
         g_log.Error(StringFormat("could not initialise %s - it may not exist at this broker "
                                  "or may need a suffix (e.g. %s.raw)",names[i],names[i]));
         g_ctx[g_symbolCount].Clear();
         continue;
        }
      g_lastNotes[g_symbolCount]="";
      g_log.Info(StringFormat("symbol ready: %-12s digits=%d point=%.*f minLot=%.2f step=%.2f "
                              "stopsLevel=%d filling=%d",
                              names[i],g_ctx[g_symbolCount].digits,
                              g_ctx[g_symbolCount].digits,g_ctx[g_symbolCount].point,
                              g_ctx[g_symbolCount].volMin,g_ctx[g_symbolCount].volStep,
                              g_ctx[g_symbolCount].stopsLevel,
                              (int)g_ctx[g_symbolCount].filling));
      g_symbolCount++;
     }

   return (g_symbolCount>0);
  }

//+------------------------------------------------------------------+
//| Push every input into the modules                                |
//+------------------------------------------------------------------+
void ApplyConfiguration(void)
  {
   g_view.Config(InpDonchianPeriod,InpErPeriod,InpAtrAvgPeriod);

   g_filters.SetLogger(GetPointer(g_log));
   g_filters.ConfigSpread(InpMaxSpreadAtr,InpMaxSpreadPoints);
   g_filters.ConfigSessions(InpUseSessions,InpSess1Start,InpSess1End,InpSess2Start,InpSess2End);
   g_filters.ConfigDays(InpTradeSunday,InpTradeMonday,InpTradeTuesday,InpTradeWednesday,
                        InpTradeThursday,InpTradeFriday,InpTradeSaturday);
   g_filters.ConfigWeekend(InpWeekendFlat,InpFridayCloseHour,InpMondayOpenHour);
   g_filters.ConfigNews(InpUseNewsFilter,InpNewsMinutesBefore,InpNewsMinutesAfter,InpNewsMinImportance);
   g_filters.ConfigVolatility(InpMinAtrPoints,InpMaxAtrMultiple);

   g_signals.SetLogger(GetPointer(g_log));
   g_signals.ConfigRegime(InpAdxTrendMin,InpAdxRangeMax,InpErTrendMin,InpErRangeMax);
   g_signals.ConfigEntry(InpMinScore,InpRsiOversold,InpRsiOverbought,InpPullbackLookback);
   g_signals.ConfigGeometry(InpAtrStopTrend,InpAtrStopRange,InpRRTrend,InpRRRange);
   g_signals.ConfigMode(InpStrategyMode,InpRequireBias);

   g_risk.SetLogger(GetPointer(g_log));
   g_risk.SetMagic(InpMagic);
   g_risk.Config(InpRiskPercent,InpMaxRiskPercent,InpDailyLossLimitPct,InpMaxDrawdownPct,
                 InpDDThrottleStartPct,InpDDThrottleFloor);
   g_risk.ConfigLimits(InpMaxPositionsTotal,InpMaxPositionsPerSym,InpMaxTradesPerDay,
                       InpLossStreakLimit,InpCooldownMinutes,InpMaxLotsPerTrade,
                       InpMaxMarginUtilPct,InpMaxExposurePerCcy);

   g_exec.SetLogger(GetPointer(g_log));
   g_exec.Config(InpMagic,InpSlippagePoints,InpMaxRetries,InpRetryDelayMs,InpTradeComment);

   g_posmgr.SetLogger(GetPointer(g_log));
   g_posmgr.SetExecutor(GetPointer(g_exec));
   g_posmgr.SetMagic(InpMagic);
   g_posmgr.ConfigBreakEven(InpUseBreakEven,InpBeTriggerR,InpBeOffsetR);
   g_posmgr.ConfigPartial(InpUsePartial,InpPartialTriggerR,InpPartialPercent);
   g_posmgr.ConfigTrailing(InpUseTrailing,InpTrailStartR,InpTrailAtrMult);
   g_posmgr.ConfigTimeStop(InpUseTimeStop,InpMaxBarsInTrade,InpTimeStopMinR);

   g_remote.SetLogger(GetPointer(g_log));
   g_remote.Config(InpRemoteEnabled,InpRemoteUrl,InpRemoteToken,InpBotId,
                   InpRemoteTimeoutMs,InpOfflinePolicy);

   g_hud.Config(InpShowPanel,InpPanelX,InpPanelY);
  }

//+------------------------------------------------------------------+
//| Evaluate and possibly trade one symbol                           |
//+------------------------------------------------------------------+
void ProcessSymbol(const int idx,const datetime now)
  {
   // NOTE: never take a value copy of CSymbolCtx - the copy's destructor would
   // release the original's indicator handles. Always work through g_ctx[idx].
   if(!g_ctx[idx].HasHistory(g_view.RequiredBars()))
     {
      g_lastNotes[idx]="waiting for history";
      return;
     }

   if(!g_view.Build(g_ctx[idx],g_mv))
     {
      g_lastNotes[idx]="market data incomplete";
      return;
     }

   //--- cosmetic: the panel shows the chart symbol's regime
   double dummyStrength=0.0;
   ENUM_APEX_REGIME rg=g_signals.Classify(g_mv,dummyStrength);
   if(g_ctx[idx].symbol==_Symbol)
      g_regimeLine=StringFormat("%s %s  adx=%.0f er=%.2f atr%%=%.0f",
                                g_ctx[idx].symbol,ApexRegimeToString(rg),
                                g_mv.adx[0],g_mv.efficiencyRatio,g_mv.atrPercentile*100.0);

   //--- spacing between entries on the same symbol
   if(InpMinBarsBetweenTrd>0 && g_ctx[idx].lastTradeTime>0)
     {
      int secs=PeriodSeconds(InpSignalTF);
      if(secs>0)
        {
         int barsSince=(int)((now-g_ctx[idx].lastTradeTime)/secs);
         if(barsSince<InpMinBarsBetweenTrd)
           {
            g_lastNotes[idx]=StringFormat("cooldown %d/%d bars",barsSince,InpMinBarsBetweenTrd);
            return;
           }
        }
     }

   //--- filters that need market data
   string reason="";
   if(!g_filters.VolatilityAllowed(g_ctx[idx],g_mv.atrNow,g_mv.atrAverage,reason))
     {
      g_lastNotes[idx]=reason;
      return;
     }
   if(!g_filters.SpreadAllowed(g_ctx[idx],g_mv.atrNow,reason))
     {
      g_lastNotes[idx]=reason;
      return;
     }
   if(!g_filters.NewsAllowed(g_ctx[idx],now,reason))
     {
      g_lastNotes[idx]=reason;
      g_log.Debug(g_ctx[idx].symbol+": "+reason);
      return;
     }

   //--- signal
   ApexSignal sig;
   if(!g_signals.Evaluate(g_ctx[idx],g_mv,sig))
     {
      g_lastNotes[idx]=sig.note;
      g_log.Debug(StringFormat("%s no trade: %s",g_ctx[idx].symbol,sig.note));
      return;
     }

   //--- do we already hold the same direction here?
   for(int p=PositionsTotal()-1;p>=0;p--)
     {
      ulong t=PositionGetTicket(p);
      if(t==0) continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      if(PositionGetString(POSITION_SYMBOL)!=g_ctx[idx].symbol) continue;

      bool posLong=((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY);
      bool sigLong=(sig.dir==APEX_DIR_LONG);
      if(posLong==sigLong)
        {
         g_lastNotes[idx]="already positioned "+ApexDirToString(sig.dir);
         return;
        }
     }

   //--- risk
   ApexRiskDecision dec;
   g_risk.Evaluate(g_ctx[idx],sig,now,dec);
   if(!dec.allowed)
     {
      g_lastNotes[idx]=dec.reason;
      g_log.Debug(StringFormat("%s risk refused: %s",g_ctx[idx].symbol,dec.reason));
      return;
     }

   //--- execute
   string tag=StringFormat("%s%.0f",
                           (sig.regime==APEX_REGIME_TREND?"T":"R"),
                           MathAbs(sig.score)*100.0);
   ulong ticket=0;
   g_log.Info(StringFormat("%s SIGNAL %s score=%.2f conf=%.2f | %s | %s | %s",
                           g_ctx[idx].symbol,ApexDirToString(sig.dir),sig.score,sig.confidence,
                           sig.source,sig.note,dec.reason));

   if(g_exec.OpenPosition(g_ctx[idx],sig.dir,dec.lots,sig.stopDistance,sig.targetDistance,tag,ticket))
     {
      g_ctx[idx].lastTradeTime=now;
      g_lastNotes[idx]=StringFormat("opened %s %.2f lots",ApexDirToString(sig.dir),dec.lots);

      //--- attach management state to the position that was just created
      for(int p=PositionsTotal()-1;p>=0;p--)
        {
         ulong t=PositionGetTicket(p);
         if(t==0) continue;
         if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
         if(PositionGetString(POSITION_SYMBOL)!=g_ctx[idx].symbol) continue;
         g_posmgr.Register(t,sig.stopDistance);
         break;
        }

      g_remote.Notify("info","Entry",
                      StringFormat("%s %s %.2f lots, risk %.2f%% (%s)",
                                   g_ctx[idx].symbol,ApexDirToString(sig.dir),
                                   dec.lots,dec.riskPercentUsed,sig.source));
     }
   else
     {
      g_lastNotes[idx]="entry rejected by broker";
     }
  }

//+------------------------------------------------------------------+
//| Run trade management across every configured symbol              |
//+------------------------------------------------------------------+
void ManageOpenPositions(void)
  {
   for(int i=0;i<g_symbolCount;i++)
     {
      if(!g_ctx[i].valid) continue;
      if(g_risk.CountPositions(g_ctx[i].symbol)==0) continue;

      double atr=0.0;
      double buf[];
      ArraySetAsSeries(buf,true);
      if(CopyBuffer(g_ctx[i].hAtr,0,1,1,buf)==1) atr=buf[0];

      g_posmgr.Manage(g_ctx[i],atr);
     }
  }

//+------------------------------------------------------------------+
//| Remote heartbeat                                                 |
//+------------------------------------------------------------------+
void DoHeartbeat(void)
  {
   ApexSnapshot s;
   BuildSnapshot(s);

   string payload=g_remote.BuildPayload(s,BuildPositionsJson(),BuildSymbolsJson(),APEX_VERSION);

   ApexCommand cmds[];
   bool serverPaused=false;
   if(!g_remote.Poll(payload,cmds,serverPaused))
      return;

   int n=ArraySize(cmds);
   for(int i=0;i<n;i++)
     {
      ApplyCommand(cmds[i]);
      g_remote.Ack(cmds[i].id);
     }

   // The control server owns the pause flag. Re-syncing here means a terminal
   // restart cannot resume a bot that was paused from the phone.
   if(serverPaused!=g_paused)
     {
      g_paused=serverPaused;
      g_log.Warn(StringFormat("pause state synced from control server: %s",
                              (g_paused?"PAUSED":"ACTIVE")));
     }

   g_lastHeartbeat=TimeCurrent();
  }

//+------------------------------------------------------------------+
//| Execute one remote command                                       |
//+------------------------------------------------------------------+
void ApplyCommand(const ApexCommand &cmd)
  {
   string t=cmd.type;
   StringToLower(t);

   if(t=="ping")
     {
      g_log.Debug("remote ping");
      return;
     }

   if(t=="pause")
     {
      if(!g_paused) g_log.Warn("remote command: PAUSE - no new entries, open trades still managed");
      g_paused=true;
      return;
     }

   if(t=="resume")
     {
      if(g_paused) g_log.Warn("remote command: RESUME");
      g_paused=false;
      return;
     }

   if(t=="close_all")
     {
      int n=g_exec.CloseAll("","remote command: close all");
      g_log.Warn(StringFormat("remote command: CLOSE ALL - %d position(s) closed",n));
      return;
     }

   if(t=="close_symbol")
     {
      if(StringLen(cmd.symbol)==0) { g_log.Warn("close_symbol without a symbol - ignored"); return; }
      int n=g_exec.CloseAll(cmd.symbol,"remote command: close "+cmd.symbol);
      g_log.Warn(StringFormat("remote command: CLOSE %s - %d position(s) closed",cmd.symbol,n));
      return;
     }

   if(t=="flatten")
     {
      int n=g_exec.CloseAll("","remote command: flatten and pause");
      g_paused=true;
      g_log.Warn(StringFormat("remote command: FLATTEN - %d closed, bot paused",n));
      return;
     }

   if(t=="set_risk")
     {
      if(!g_risk.SetRiskPercent(cmd.value))
         g_log.Warn(StringFormat("requested risk %.2f%% was clamped to the %.2f%% ceiling",
                                 cmd.value,InpMaxRiskPercent));
      return;
     }

   if(t=="kill")
     {
      g_risk.EngageKillSwitch("remote kill switch");
      g_exec.CloseAll("","remote kill switch");
      g_paused=true;
      return;
     }

   if(t=="release_kill")
     {
      g_risk.ReleaseKillSwitch();
      g_paused=false;
      g_log.Warn("remote command: KILL SWITCH RELEASED");
      return;
     }

   g_log.Warn("unknown remote command: "+cmd.type);
  }

//+------------------------------------------------------------------+
//| Snapshot for the panel and the control server                    |
//+------------------------------------------------------------------+
void BuildSnapshot(ApexSnapshot &s)
  {
   s.balance         = AccountInfoDouble(ACCOUNT_BALANCE);
   s.equity          = AccountInfoDouble(ACCOUNT_EQUITY);
   s.margin          = AccountInfoDouble(ACCOUNT_MARGIN);
   s.freeMargin      = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   s.marginLevel     = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);
   s.dayStartBalance = g_risk.DayStartBalance();
   s.dayPnL          = g_risk.DayPnL();
   s.dayPnLPct       = g_risk.DayPnLPct();
   s.peakEquity      = g_risk.PeakEquity();
   s.drawdownPct     = g_risk.DrawdownPct();
   s.openPositions   = g_risk.CountPositions("");
   s.tradesToday     = g_risk.TradesToday();
   s.lossStreak      = g_risk.LossStreak();
   s.paused          = g_paused;
   s.halt            = g_risk.Halt();
   s.haltReason      = g_risk.HaltReason();
   s.riskPercent     = g_risk.RiskPercent();
   s.currency        = AccountInfoString(ACCOUNT_CURRENCY);
   s.login           = AccountInfoInteger(ACCOUNT_LOGIN);
   s.server          = AccountInfoString(ACCOUNT_SERVER);
   s.company         = AccountInfoString(ACCOUNT_COMPANY);
  }

//+------------------------------------------------------------------+
//| Open positions as a JSON array                                   |
//+------------------------------------------------------------------+
string BuildPositionsJson(void)
  {
   string out="[";
   bool first=true;

   for(int i=PositionsTotal()-1;i>=0;i--)
     {
      ulong ticket=PositionGetTicket(i);
      if(ticket==0) continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;

      string sym=PositionGetString(POSITION_SYMBOL);
      bool   isLong=((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY);
      int    dig=(int)SymbolInfoInteger(sym,SYMBOL_DIGITS);

      CJsonWriter j;
      j.Int ("ticket",   (long)ticket);
      j.Str ("symbol",   sym);
      j.Str ("side",     (isLong?"BUY":"SELL"));
      j.Num ("volume",   PositionGetDouble(POSITION_VOLUME),2);
      j.Num ("open",     PositionGetDouble(POSITION_PRICE_OPEN),dig);
      j.Num ("current",  PositionGetDouble(POSITION_PRICE_CURRENT),dig);
      j.Num ("sl",       PositionGetDouble(POSITION_SL),dig);
      j.Num ("tp",       PositionGetDouble(POSITION_TP),dig);
      j.Num ("profit",   PositionGetDouble(POSITION_PROFIT),2);
      j.Num ("swap",     PositionGetDouble(POSITION_SWAP),2);
      j.Int ("opened",   (long)PositionGetInteger(POSITION_TIME));
      j.Str ("comment",  PositionGetString(POSITION_COMMENT));

      if(!first) out+=",";
      out+=j.Finish();
      first=false;
     }

   return out+"]";
  }

//+------------------------------------------------------------------+
//| Per-symbol status as a JSON array                                |
//+------------------------------------------------------------------+
string BuildSymbolsJson(void)
  {
   string out="[";
   for(int i=0;i<g_symbolCount;i++)
     {
      if(i>0) out+=",";
      CJsonWriter j;
      j.Str("symbol",   g_ctx[i].symbol);
      j.Bool("valid",   g_ctx[i].valid);
      j.Num("spread",   (g_ctx[i].point>0.0 ? g_ctx[i].SpreadPrice()/g_ctx[i].point : 0.0),1);
      j.Int("positions",g_risk.CountPositions(g_ctx[i].symbol));
      j.Str("note",     g_lastNotes[i]);
      out+=j.Finish();
     }
   return out+"]";
  }

//+------------------------------------------------------------------+
//| Input validation - fail loudly at load time, never mid-session   |
//+------------------------------------------------------------------+
bool ValidateInputs(string &problem)
  {
   if(InpRiskPercent<=0.0 || InpRiskPercent>10.0)
     { problem="Risk per trade must be between 0 and 10 percent."; return false; }

   if(InpMaxRiskPercent<InpRiskPercent)
     { problem="Max risk percent cannot be lower than the base risk percent."; return false; }

   if(InpMaxDrawdownPct<=0.0 || InpMaxDrawdownPct>90.0)
     { problem="Max drawdown must be between 0 and 90 percent."; return false; }

   if(InpDailyLossLimitPct<=0.0 || InpDailyLossLimitPct>InpMaxDrawdownPct)
     { problem="Daily loss limit must be positive and not larger than max drawdown."; return false; }

   if(InpEmaFast>=InpEmaSlow)
     { problem="Fast EMA period must be smaller than the slow EMA period."; return false; }

   if(InpBiasEmaFast>=InpBiasEmaSlow)
     { problem="Bias fast EMA must be smaller than the bias slow EMA."; return false; }

   if(InpAdxRangeMax>=InpAdxTrendMin)
     { problem="ADX range ceiling must be below the ADX trend floor."; return false; }

   if(InpErRangeMax>=InpErTrendMin)
     { problem="Efficiency-ratio range ceiling must be below the trend floor."; return false; }

   if(InpMinScore<=0.0 || InpMinScore>=1.0)
     { problem="Minimum score must be between 0 and 1."; return false; }

   if(InpAtrStopTrend<=0.0 || InpAtrStopRange<=0.0)
     { problem="ATR stop multipliers must be positive."; return false; }

   if(InpMaxPositionsTotal<1 || InpMaxPositionsPerSym<1)
     { problem="Position limits must be at least 1."; return false; }

   if(SignalTfExceedsBiasTf())
     { problem="Signal timeframe must be lower than or equal to the bias timeframe."; return false; }

   if(InpRemoteEnabled)
     {
      if(StringLen(InpRemoteUrl)==0)
        { problem="Remote control is enabled but the URL is empty."; return false; }
      if(StringFind(InpRemoteUrl,"http")!=0)
        { problem="Remote URL must start with http:// or https://"; return false; }
      if(StringLen(InpRemoteToken)==0)
        { problem="Remote control is enabled but no token was set."; return false; }
     }

   return true;
  }

//--- small helper so the validation above reads cleanly
bool SignalTfExceedsBiasTf(void)
  {
   int sig =PeriodSeconds(InpSignalTF);
   int bias=PeriodSeconds(InpBiasTF);
   if(sig<=0 || bias<=0) return false;
   return (sig>bias);
  }
//+------------------------------------------------------------------+
