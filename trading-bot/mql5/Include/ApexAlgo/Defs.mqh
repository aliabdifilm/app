//+------------------------------------------------------------------+
//|                                                         Defs.mqh |
//|                       ApexAlgo - shared types, enums, utilities  |
//+------------------------------------------------------------------+

#ifndef APEX_DEFS_MQH
#define APEX_DEFS_MQH

#define APEX_VERSION        "1.0.0"
#define APEX_MAX_SYMBOLS    32
#define APEX_MAX_CCY        16

//--- Direction of a trade idea -------------------------------------
enum ENUM_APEX_DIR
  {
   APEX_DIR_NONE = 0,
   APEX_DIR_LONG = 1,
   APEX_DIR_SHORT= -1
  };

//--- Market regime --------------------------------------------------
enum ENUM_APEX_REGIME
  {
   APEX_REGIME_UNKNOWN = 0,   // not enough data
   APEX_REGIME_TREND   = 1,   // directional / momentum market
   APEX_REGIME_RANGE   = 2,   // mean-reverting / choppy market
   APEX_REGIME_NEUTRAL = 3    // transition - stand aside
  };

//--- Which strategy families are allowed to fire --------------------
enum ENUM_APEX_STRATMODE
  {
   APEX_STRAT_AUTO       = 0, // regime decides (recommended)
   APEX_STRAT_TREND_ONLY = 1,
   APEX_STRAT_RANGE_ONLY = 2
  };

//--- How the bot behaves when the control server is unreachable ----
enum ENUM_APEX_OFFLINE
  {
   APEX_OFFLINE_KEEP_TRADING = 0, // keep running on local rules (default)
   APEX_OFFLINE_NO_NEW_TRADES= 1, // manage open trades, open nothing new
   APEX_OFFLINE_FLATTEN      = 2  // close everything and pause
  };

//--- Reason the risk engine blocked trading -------------------------
enum ENUM_APEX_HALT
  {
   APEX_HALT_NONE            = 0,
   APEX_HALT_DAILY_LOSS      = 1,
   APEX_HALT_MAX_DRAWDOWN    = 2,
   APEX_HALT_LOSS_STREAK     = 3,
   APEX_HALT_MAX_TRADES_DAY  = 4,
   APEX_HALT_REMOTE_PAUSE    = 5,
   APEX_HALT_KILL_SWITCH     = 6,
   APEX_HALT_TERMINAL        = 7,
   APEX_HALT_MARGIN          = 8,
   APEX_HALT_WEEKEND         = 9
  };

//--- A scored trade idea produced by the signal ensemble ------------
struct ApexSignal
  {
   ENUM_APEX_DIR     dir;           // direction
   double            score;         // -1 .. +1 (sign = direction, |.| = conviction)
   double            confidence;    // 0 .. 1  (used to scale risk)
   ENUM_APEX_REGIME  regime;        // regime the idea came from
   string            source;        // human readable strategy name
   double            atr;           // ATR at signal time (price units)
   double            stopDistance;  // suggested stop distance in price units
   double            targetDistance;// suggested first target distance
   string            note;          // diagnostics
  };

//--- Result of a risk evaluation ------------------------------------
struct ApexRiskDecision
  {
   bool              allowed;
   double            lots;
   double            riskPercentUsed;
   double            riskMoney;
   ENUM_APEX_HALT    halt;
   string            reason;
  };

//--- Aggregated account snapshot broadcast to the control server ----
struct ApexSnapshot
  {
   double            balance;
   double            equity;
   double            margin;
   double            freeMargin;
   double            marginLevel;
   double            dayStartBalance;
   double            dayPnL;
   double            dayPnLPct;
   double            peakEquity;
   double            drawdownPct;
   int               openPositions;
   int               tradesToday;
   int               lossStreak;
   bool              paused;
   ENUM_APEX_HALT    halt;
   string            haltReason;
   double            riskPercent;
   string            currency;
   long              login;
   string            server;
   string            company;
  };

//+------------------------------------------------------------------+
//| Small helpers                                                    |
//+------------------------------------------------------------------+
string ApexDirToString(const ENUM_APEX_DIR d)
  {
   if(d==APEX_DIR_LONG)  return "LONG";
   if(d==APEX_DIR_SHORT) return "SHORT";
   return "NONE";
  }

string ApexRegimeToString(const ENUM_APEX_REGIME r)
  {
   switch(r)
     {
      case APEX_REGIME_TREND:   return "TREND";
      case APEX_REGIME_RANGE:   return "RANGE";
      case APEX_REGIME_NEUTRAL: return "NEUTRAL";
     }
   return "UNKNOWN";
  }

string ApexHaltToString(const ENUM_APEX_HALT h)
  {
   switch(h)
     {
      case APEX_HALT_DAILY_LOSS:     return "DAILY_LOSS_LIMIT";
      case APEX_HALT_MAX_DRAWDOWN:   return "MAX_DRAWDOWN";
      case APEX_HALT_LOSS_STREAK:    return "LOSS_STREAK_COOLDOWN";
      case APEX_HALT_MAX_TRADES_DAY: return "MAX_TRADES_PER_DAY";
      case APEX_HALT_REMOTE_PAUSE:   return "REMOTE_PAUSE";
      case APEX_HALT_KILL_SWITCH:    return "KILL_SWITCH";
      case APEX_HALT_TERMINAL:       return "TERMINAL_NOT_READY";
      case APEX_HALT_MARGIN:         return "INSUFFICIENT_MARGIN";
      case APEX_HALT_WEEKEND:        return "WEEKEND_FLAT";
     }
   return "NONE";
  }

//--- clamp a double into [lo,hi] ------------------------------------
double ApexClamp(const double v,const double lo,const double hi)
  {
   if(v<lo) return lo;
   if(v>hi) return hi;
   return v;
  }

//--- midnight of the server day that 't' belongs to -----------------
datetime ApexDayStart(const datetime t)
  {
   MqlDateTime s;
   TimeToStruct(t,s);
   s.hour=0; s.min=0; s.sec=0;
   return StructToTime(s);
  }

//--- 0=Sunday .. 6=Saturday -----------------------------------------
int ApexDayOfWeek(const datetime t)
  {
   MqlDateTime s;
   TimeToStruct(t,s);
   return s.day_of_week;
  }

int ApexHourOf(const datetime t)
  {
   MqlDateTime s;
   TimeToStruct(t,s);
   return s.hour;
  }

int ApexMinuteOf(const datetime t)
  {
   MqlDateTime s;
   TimeToStruct(t,s);
   return s.min;
  }

//--- split "EURUSD,GBPUSD, XAUUSD" into a clean array ---------------
int ApexSplitCsv(const string csv,string &out[])
  {
   ArrayResize(out,0);
   string parts[];
   int n=StringSplit(csv,',',parts);
   for(int i=0;i<n;i++)
     {
      string p=parts[i];
      StringTrimLeft(p);
      StringTrimRight(p);
      if(StringLen(p)==0) continue;
      int k=ArraySize(out);
      ArrayResize(out,k+1);
      out[k]=p;
     }
   return ArraySize(out);
  }

//--- standard deviation of an array slice ---------------------------
double ApexStdDev(const double &arr[],const int count)
  {
   int n=MathMin(count,ArraySize(arr));
   if(n<2) return 0.0;
   double mean=0.0;
   for(int i=0;i<n;i++) mean+=arr[i];
   mean/=n;
   double acc=0.0;
   for(int i=0;i<n;i++)
     {
      double d=arr[i]-mean;
      acc+=d*d;
     }
   return MathSqrt(acc/(n-1));
  }

//--- percentile rank (0..1) of 'value' inside arr[0..count-1] -------
double ApexPercentileRank(const double &arr[],const int count,const double value)
  {
   int n=MathMin(count,ArraySize(arr));
   if(n<=0) return 0.5;
   int below=0;
   for(int i=0;i<n;i++)
      if(arr[i]<value) below++;
   return (double)below/(double)n;
  }

//--- Kaufman efficiency ratio over 'period' closes (series order) ---
double ApexEfficiencyRatio(const double &close[],const int period)
  {
   int n=ArraySize(close);
   if(n<period+1 || period<1) return 0.0;
   double direction=MathAbs(close[0]-close[period]);
   double volatility=0.0;
   for(int i=0;i<period;i++)
      volatility+=MathAbs(close[i]-close[i+1]);
   if(volatility<=0.0) return 0.0;
   return direction/volatility;
  }

#endif // APEX_DEFS_MQH
