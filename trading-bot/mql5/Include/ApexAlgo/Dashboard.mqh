//+------------------------------------------------------------------+
//|                                                    Dashboard.mqh |
//|  ApexAlgo - on-chart status panel                                |
//|                                                                  |
//| When something looks wrong on a live account you want the answer  |
//| on the chart, not buried in the journal. This panel always shows  |
//| the four questions that matter: is it allowed to trade, how much  |
//| is at risk, how far into drawdown are we, and is the remote link  |
//| alive.                                                            |
//+------------------------------------------------------------------+

#ifndef APEX_DASHBOARD_MQH
#define APEX_DASHBOARD_MQH

#include "Defs.mqh"

#define APEX_HUD_PREFIX "ApexHUD_"

//+------------------------------------------------------------------+
//| CDashboard                                                       |
//+------------------------------------------------------------------+
class CDashboard
  {
private:
   bool              m_enabled;
   int               m_x;
   int               m_y;
   int               m_lineHeight;
   int               m_fontSize;
   string            m_font;
   color             m_bg;
   color             m_titleColor;
   int               m_rows;
   int               m_width;

   string            Name(const string id) const { return APEX_HUD_PREFIX+id; }

   void              EnsureLabel(const string id,const int row,const color clr,
                                  const int size,const bool bold)
     {
      string n=Name(id);
      if(ObjectFind(0,n)<0)
        {
         ObjectCreate(0,n,OBJ_LABEL,0,0,0);
         ObjectSetInteger(0,n,OBJPROP_CORNER,CORNER_LEFT_UPPER);
         ObjectSetInteger(0,n,OBJPROP_SELECTABLE,false);
         ObjectSetInteger(0,n,OBJPROP_HIDDEN,true);
         ObjectSetInteger(0,n,OBJPROP_BACK,false);
         ObjectSetInteger(0,n,OBJPROP_ZORDER,100);
        }
      ObjectSetInteger(0,n,OBJPROP_XDISTANCE,m_x+10);
      ObjectSetInteger(0,n,OBJPROP_YDISTANCE,m_y+8+row*m_lineHeight);
      ObjectSetInteger(0,n,OBJPROP_COLOR,clr);
      ObjectSetInteger(0,n,OBJPROP_FONTSIZE,size);
      ObjectSetString (0,n,OBJPROP_FONT,(bold?"Arial Bold":m_font));
     }

   void              SetText(const string id,const string text)
     {
      ObjectSetString(0,Name(id),OBJPROP_TEXT,text);
     }

   void              EnsurePanel(void)
     {
      string n=Name("bg");
      if(ObjectFind(0,n)<0)
        {
         ObjectCreate(0,n,OBJ_RECTANGLE_LABEL,0,0,0);
         ObjectSetInteger(0,n,OBJPROP_CORNER,CORNER_LEFT_UPPER);
         ObjectSetInteger(0,n,OBJPROP_SELECTABLE,false);
         ObjectSetInteger(0,n,OBJPROP_HIDDEN,true);
         ObjectSetInteger(0,n,OBJPROP_BACK,false);
         ObjectSetInteger(0,n,OBJPROP_BORDER_TYPE,BORDER_FLAT);
         ObjectSetInteger(0,n,OBJPROP_ZORDER,99);
        }
      ObjectSetInteger(0,n,OBJPROP_XDISTANCE,m_x);
      ObjectSetInteger(0,n,OBJPROP_YDISTANCE,m_y);
      ObjectSetInteger(0,n,OBJPROP_XSIZE,m_width);
      ObjectSetInteger(0,n,OBJPROP_YSIZE,16+m_rows*m_lineHeight);
      ObjectSetInteger(0,n,OBJPROP_BGCOLOR,m_bg);
      ObjectSetInteger(0,n,OBJPROP_COLOR,clrDimGray);
     }

public:
                     CDashboard(void)
     {
      m_enabled=true;
      m_x=12;
      m_y=22;
      m_lineHeight=16;
      m_fontSize=9;
      m_font="Consolas";
      m_bg=C'18,20,26';
      m_titleColor=clrGold;
      m_rows=13;
      m_width=330;
     }

   void              Config(const bool enabled,const int x,const int y)
     {
      m_enabled=enabled;
      m_x=x;
      m_y=y;
     }

   void              Destroy(void)
     {
      ObjectsDeleteAll(0,APEX_HUD_PREFIX);
      ChartRedraw(0);
     }

   //+---------------------------------------------------------------+
   //| Redraw. Called on a timer, not on every tick.                  |
   //+---------------------------------------------------------------+
   void              Render(const ApexSnapshot &s,
                            const string version,
                            const string regimeLine,
                            const bool remoteEnabled,
                            const bool remoteAlive,
                            const string remoteNote,
                            const int trackedPositions)
     {
      if(!m_enabled) return;

      EnsurePanel();

      color okColor   = C'120,220,150';
      color warnColor = C'255,196,80';
      color badColor  = C'255,110,110';
      color dimColor  = C'150,158,175';
      color valColor  = C'225,230,240';

      int row=0;

      EnsureLabel("title",row,m_titleColor,10,true);
      SetText("title",StringFormat("APEX ALGO  v%s",version));
      row++;

      EnsureLabel("acct",row,dimColor,m_fontSize,false);
      SetText("acct",StringFormat("%I64d @ %s (%s)",s.login,s.server,s.currency));
      row++;

      //--- state line ---------------------------------------------
      color stateColor=okColor;
      string stateText="ACTIVE";
      if(s.paused)                        { stateColor=warnColor; stateText="PAUSED"; }
      if(s.halt==APEX_HALT_KILL_SWITCH)   { stateColor=badColor;  stateText="KILL SWITCH"; }
      else if(s.halt!=APEX_HALT_NONE)     { stateColor=warnColor; stateText=ApexHaltToString(s.halt); }

      EnsureLabel("state",row,stateColor,10,true);
      SetText("state","STATE: "+stateText);
      row++;

      if(StringLen(s.haltReason)>0)
        {
         EnsureLabel("reason",row,dimColor,m_fontSize,false);
         string r=s.haltReason;
         if(StringLen(r)>44) r=StringSubstr(r,0,44)+"...";
         SetText("reason","  "+r);
        }
      else
        {
         EnsureLabel("reason",row,dimColor,m_fontSize,false);
         SetText("reason","  all guards clear");
        }
      row++;

      EnsureLabel("sep1",row,dimColor,m_fontSize,false);
      SetText("sep1","------------------------------------------");
      row++;

      EnsureLabel("bal",row,valColor,m_fontSize,false);
      SetText("bal",StringFormat("Balance   %12.2f   Equity %12.2f",s.balance,s.equity));
      row++;

      color pnlColor=(s.dayPnL>=0.0)?okColor:badColor;
      EnsureLabel("day",row,pnlColor,m_fontSize,false);
      SetText("day",StringFormat("Day P/L   %12.2f   (%+.2f%%)",s.dayPnL,s.dayPnLPct));
      row++;

      color ddColor=(s.drawdownPct<3.0)?okColor:((s.drawdownPct<7.0)?warnColor:badColor);
      EnsureLabel("dd",row,ddColor,m_fontSize,false);
      SetText("dd",StringFormat("Drawdown  %11.2f%%   Peak %12.2f",s.drawdownPct,s.peakEquity));
      row++;

      EnsureLabel("risk",row,valColor,m_fontSize,false);
      SetText("risk",StringFormat("Risk/trade %10.2f%%   Margin lvl %8.0f%%",
                                  s.riskPercent,s.marginLevel));
      row++;

      EnsureLabel("pos",row,valColor,m_fontSize,false);
      SetText("pos",StringFormat("Positions %4d tracked %2d   Trades today %3d",
                                 s.openPositions,trackedPositions,s.tradesToday));
      row++;

      color streakColor=(s.lossStreak>=2)?warnColor:dimColor;
      EnsureLabel("streak",row,streakColor,m_fontSize,false);
      SetText("streak",StringFormat("Loss streak %2d",s.lossStreak));
      row++;

      EnsureLabel("regime",row,dimColor,m_fontSize,false);
      string rl=regimeLine;
      if(StringLen(rl)>46) rl=StringSubstr(rl,0,46)+"...";
      SetText("regime",rl);
      row++;

      color linkColor = !remoteEnabled ? dimColor : (remoteAlive?okColor:badColor);
      string linkText = !remoteEnabled
                        ? "Remote  : disabled (local only)"
                        : (remoteAlive ? "Remote  : connected" : "Remote  : OFFLINE - "+remoteNote);
      EnsureLabel("link",row,linkColor,m_fontSize,false);
      if(StringLen(linkText)>46) linkText=StringSubstr(linkText,0,46);
      SetText("link",linkText);
      row++;

      m_rows=row;
      ChartRedraw(0);
     }
  };

#endif // APEX_DASHBOARD_MQH
