//+------------------------------------------------------------------+
//|                                                      Filters.mqh |
//|  ApexAlgo - the "do not trade" gate: spread, sessions, news, vol  |
//|                                                                  |
//| Most of an automated system's edge comes from the trades it does  |
//| NOT take. Every filter here exists because a specific, known way  |
//| of losing money passes straight through a naive signal engine.    |
//+------------------------------------------------------------------+

#ifndef APEX_FILTERS_MQH
#define APEX_FILTERS_MQH

#include "Defs.mqh"
#include "SymbolCtx.mqh"
#include "Logger.mqh"

//+------------------------------------------------------------------+
//| CFilters                                                         |
//+------------------------------------------------------------------+
class CFilters
  {
private:
   CApexLogger      *m_log;

   //--- spread
   double            m_maxSpreadAtr;     // reject if spread > this * ATR
   int               m_maxSpreadPoints;  // hard ceiling in points (0 = off)

   //--- sessions (server time, inclusive start, exclusive end)
   bool              m_useSessions;
   int               m_sess1Start, m_sess1End;
   int               m_sess2Start, m_sess2End;

   //--- weekday mask: index 0=Sunday .. 6=Saturday
   bool              m_days[7];

   //--- weekend flat
   bool              m_weekendFlat;
   int               m_fridayCloseHour;
   int               m_mondayOpenHour;

   //--- news
   bool              m_useNews;
   int               m_newsMinutesBefore;
   int               m_newsMinutesAfter;
   int               m_newsMinImportance;  // 0 none,1 low,2 moderate,3 high
   bool              m_calendarAvailable;

   //--- volatility sanity
   double            m_minAtrPoints;       // ignore dead markets
   double            m_maxAtrMultiple;     // ignore spike/gap conditions

   //--- cached news lookups so we do not hammer the calendar API
   datetime          m_newsCacheUntil;
   string            m_newsCacheSymbol;
   bool              m_newsCacheBlocked;
   string            m_newsCacheReason;

public:
                     CFilters(void)
     {
      m_log=NULL;
      m_maxSpreadAtr=0.25;
      m_maxSpreadPoints=0;
      m_useSessions=false;
      m_sess1Start=0; m_sess1End=24;
      m_sess2Start=0; m_sess2End=0;
      for(int i=0;i<7;i++) m_days[i]=true;
      m_days[0]=false;   // Sunday
      m_days[6]=false;   // Saturday
      m_weekendFlat=true;
      m_fridayCloseHour=20;
      m_mondayOpenHour=0;
      m_useNews=true;
      m_newsMinutesBefore=30;
      m_newsMinutesAfter=30;
      m_newsMinImportance=3;
      m_calendarAvailable=true;
      m_minAtrPoints=0.0;
      m_maxAtrMultiple=4.0;
      m_newsCacheUntil=0;
      m_newsCacheSymbol="";
      m_newsCacheBlocked=false;
      m_newsCacheReason="";
     }

   void              SetLogger(CApexLogger *l) { m_log=l; }

   void              ConfigSpread(const double maxSpreadAtr,const int maxSpreadPoints)
     {
      m_maxSpreadAtr=maxSpreadAtr;
      m_maxSpreadPoints=maxSpreadPoints;
     }

   void              ConfigSessions(const bool use,
                                    const int s1a,const int s1b,
                                    const int s2a,const int s2b)
     {
      m_useSessions=use;
      m_sess1Start=s1a; m_sess1End=s1b;
      m_sess2Start=s2a; m_sess2End=s2b;
     }

   void              ConfigDays(const bool sun,const bool mon,const bool tue,
                                const bool wed,const bool thu,const bool fri,const bool sat)
     {
      m_days[0]=sun; m_days[1]=mon; m_days[2]=tue;
      m_days[3]=wed; m_days[4]=thu; m_days[5]=fri; m_days[6]=sat;
     }

   void              ConfigWeekend(const bool flat,const int fridayCloseHour,const int mondayOpenHour)
     {
      m_weekendFlat=flat;
      m_fridayCloseHour=fridayCloseHour;
      m_mondayOpenHour=mondayOpenHour;
     }

   void              ConfigNews(const bool use,const int before,const int after,const int minImportance)
     {
      m_useNews=use;
      m_newsMinutesBefore=before;
      m_newsMinutesAfter=after;
      m_newsMinImportance=minImportance;
     }

   void              ConfigVolatility(const double minAtrPoints,const double maxAtrMultiple)
     {
      m_minAtrPoints=minAtrPoints;
      m_maxAtrMultiple=maxAtrMultiple;
     }

   //+---------------------------------------------------------------+
   //| Is the current server time inside an allowed trading window?  |
   //+---------------------------------------------------------------+
   bool              TimeAllowed(const datetime now,string &reason)
     {
      int dow=ApexDayOfWeek(now);
      if(dow<0 || dow>6) { reason="bad server time"; return false; }

      if(!m_days[dow])
        {
         reason="weekday disabled";
         return false;
        }

      if(m_weekendFlat)
        {
         int h=ApexHourOf(now);
         if(dow==5 && h>=m_fridayCloseHour)   // Friday late
           {
            reason="friday close window";
            return false;
           }
         if(dow==1 && h<m_mondayOpenHour)     // Monday open gap
           {
            reason="monday open window";
            return false;
           }
        }

      if(!m_useSessions) { reason=""; return true; }

      int hour=ApexHourOf(now);
      bool in1=InWindow(hour,m_sess1Start,m_sess1End);
      bool in2=(m_sess2End>m_sess2Start) ? InWindow(hour,m_sess2Start,m_sess2End) : false;
      if(in1||in2) { reason=""; return true; }

      reason="outside session";
      return false;
     }

   //+---------------------------------------------------------------+
   //| Should we force-flatten because the weekend is coming?        |
   //+---------------------------------------------------------------+
   bool              MustFlattenForWeekend(const datetime now) const
     {
      if(!m_weekendFlat) return false;
      int dow=ApexDayOfWeek(now);
      int h=ApexHourOf(now);
      if(dow==5 && h>=m_fridayCloseHour) return true;
      if(dow==6) return true;   // Saturday
      return false;
     }

   //+---------------------------------------------------------------+
   //| Spread gate. Spread is compared against ATR so the same        |
   //| setting works for EURUSD and for gold without retuning.        |
   //+---------------------------------------------------------------+
   bool              SpreadAllowed(CSymbolCtx &ctx,const double atr,string &reason)
     {
      double spread=ctx.SpreadPrice();

      if(m_maxSpreadPoints>0)
        {
         double limit=(double)m_maxSpreadPoints*ctx.point;
         if(spread>limit)
           {
            reason=StringFormat("spread %.1f pts > %d pts",spread/ctx.point,m_maxSpreadPoints);
            return false;
           }
        }

      if(m_maxSpreadAtr>0.0 && atr>0.0)
        {
         if(spread>m_maxSpreadAtr*atr)
           {
            reason=StringFormat("spread %.5f > %.2f*ATR(%.5f)",spread,m_maxSpreadAtr,atr);
            return false;
           }
        }

      reason="";
      return true;
     }

   //+---------------------------------------------------------------+
   //| Volatility sanity. Dead markets produce stops so tight they    |
   //| are pure noise; spikes produce stops so wide the risk maths    |
   //| stops being meaningful.                                        |
   //+---------------------------------------------------------------+
   bool              VolatilityAllowed(CSymbolCtx &ctx,const double atr,const double atrAverage,string &reason)
     {
      if(atr<=0.0) { reason="atr unavailable"; return false; }

      if(m_minAtrPoints>0.0)
        {
         double atrPoints=atr/ctx.point;
         if(atrPoints<m_minAtrPoints)
           {
            reason=StringFormat("atr %.1f pts < min %.1f",atrPoints,m_minAtrPoints);
            return false;
           }
        }

      if(m_maxAtrMultiple>0.0 && atrAverage>0.0)
        {
         if(atr>m_maxAtrMultiple*atrAverage)
           {
            reason=StringFormat("volatility spike: atr %.5f > %.1fx avg %.5f",atr,m_maxAtrMultiple,atrAverage);
            return false;
           }
        }

      reason="";
      return true;
     }

   //+---------------------------------------------------------------+
   //| High-impact news blackout using the terminal's own calendar.   |
   //|                                                                |
   //| The calendar API is unavailable in the Strategy Tester and on   |
   //| some broker builds. When that happens we disable the filter     |
   //| rather than blocking every trade forever.                       |
   //+---------------------------------------------------------------+
   bool              NewsAllowed(CSymbolCtx &ctx,const datetime now,string &reason)
     {
      reason="";
      if(!m_useNews) return true;
      if(!m_calendarAvailable) return true;
      if(MQLInfoInteger(MQL_TESTER)) return true;   // calendar not available in tester

      // cache for one minute per symbol
      if(m_newsCacheSymbol==ctx.symbol && now<m_newsCacheUntil)
        {
         reason=m_newsCacheReason;
         return !m_newsCacheBlocked;
        }

      string currencies[2];
      currencies[0]=ctx.ccyBase;
      currencies[1]=ctx.ccyProfit;

      datetime from=now-(datetime)(m_newsMinutesAfter*60);
      datetime to  =now+(datetime)(m_newsMinutesBefore*60);

      bool blocked=false;
      string blockReason="";

      for(int c=0;c<2 && !blocked;c++)
        {
         if(StringLen(currencies[c])==0) continue;
         if(c==1 && currencies[1]==currencies[0]) continue;

         MqlCalendarValue values[];
         int n=CalendarValueHistory(values,from,to,NULL,currencies[c]);
         if(n<0)
           {
            int err=GetLastError();
            // 4001/5401 style failures mean the calendar is simply not there
            if(m_log!=NULL)
               m_log.Warn(StringFormat("news filter disabled: calendar unavailable (err=%d)",err));
            m_calendarAvailable=false;
            ResetLastError();
            return true;
           }

         for(int i=0;i<n;i++)
           {
            MqlCalendarEvent ev;
            if(!CalendarEventById(values[i].event_id,ev)) continue;
            if((int)ev.importance<m_newsMinImportance) continue;

            blocked=true;
            blockReason=StringFormat("news blackout: %s %s at %s",
                                     currencies[c],ev.name,
                                     TimeToString(values[i].time,TIME_DATE|TIME_MINUTES));
            break;
           }
        }

      m_newsCacheSymbol=ctx.symbol;
      m_newsCacheUntil=now+60;
      m_newsCacheBlocked=blocked;
      m_newsCacheReason=blockReason;

      reason=blockReason;
      return !blocked;
     }

   bool              CalendarAvailable(void) const { return m_calendarAvailable; }

private:
   //--- handles windows that wrap past midnight (e.g. 22 -> 6)
   bool              InWindow(const int hour,const int start,const int end) const
     {
      if(start==end) return false;
      if(start<end)  return (hour>=start && hour<end);
      return (hour>=start || hour<end);
     }
  };

#endif // APEX_FILTERS_MQH
