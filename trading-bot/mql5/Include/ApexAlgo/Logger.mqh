//+------------------------------------------------------------------+
//|                                                       Logger.mqh |
//|          ApexAlgo - levelled logging to journal + CSV audit file |
//+------------------------------------------------------------------+

#ifndef APEX_LOGGER_MQH
#define APEX_LOGGER_MQH

enum ENUM_APEX_LOGLEVEL
  {
   APEX_LOG_ERROR = 0,
   APEX_LOG_WARN  = 1,
   APEX_LOG_INFO  = 2,
   APEX_LOG_DEBUG = 3
  };

//+------------------------------------------------------------------+
//| CApexLogger                                                      |
//| Every decision the bot makes is written down. When a live account |
//| behaves unexpectedly the audit trail is the only way to find out  |
//| why, so logging is treated as a first-class component.            |
//+------------------------------------------------------------------+
class CApexLogger
  {
private:
   ENUM_APEX_LOGLEVEL m_level;
   bool              m_toFile;
   string            m_fileName;
   int               m_handle;
   string            m_tag;

   string            LevelName(const ENUM_APEX_LOGLEVEL lv)
     {
      switch(lv)
        {
         case APEX_LOG_ERROR: return "ERROR";
         case APEX_LOG_WARN:  return "WARN ";
         case APEX_LOG_INFO:  return "INFO ";
         case APEX_LOG_DEBUG: return "DEBUG";
        }
      return "?????";
     }

   void              WriteLine(const ENUM_APEX_LOGLEVEL lv,const string msg)
     {
      if(lv>m_level) return;
      string stamp=TimeToString(TimeCurrent(),TIME_DATE|TIME_SECONDS);
      string line=StringFormat("[%s][%s] %s",m_tag,LevelName(lv),msg);
      Print(line);
      if(m_toFile && m_handle!=INVALID_HANDLE)
        {
         FileWrite(m_handle,stamp,LevelName(lv),m_tag,msg);
         FileFlush(m_handle);
        }
     }

public:
                     CApexLogger(void)
     {
      m_level=APEX_LOG_INFO;
      m_toFile=false;
      m_fileName="";
      m_handle=INVALID_HANDLE;
      m_tag="ApexAlgo";
     }

                    ~CApexLogger(void) { Close(); }

   bool              Init(const string tag,const ENUM_APEX_LOGLEVEL level,const bool toFile)
     {
      m_tag=tag;
      m_level=level;
      m_toFile=toFile;
      if(!m_toFile) return true;

      // one file per account+day keeps the audit readable
      long login=AccountInfoInteger(ACCOUNT_LOGIN);
      m_fileName=StringFormat("ApexAlgo\\log_%I64d_%s.csv",login,
                              TimeToString(TimeCurrent(),TIME_DATE));
      StringReplace(m_fileName,".","-");
      StringReplace(m_fileName,"-csv",".csv");
      m_handle=FileOpen(m_fileName,FILE_WRITE|FILE_READ|FILE_CSV|FILE_ANSI|FILE_SHARE_READ,';');
      if(m_handle==INVALID_HANDLE)
        {
         Print("[ApexAlgo][WARN ] could not open log file, falling back to journal only. err=",GetLastError());
         m_toFile=false;
         return false;
        }
      FileSeek(m_handle,0,SEEK_END);
      return true;
     }

   void              Close(void)
     {
      if(m_handle!=INVALID_HANDLE)
        {
         FileClose(m_handle);
         m_handle=INVALID_HANDLE;
        }
     }

   void              SetLevel(const ENUM_APEX_LOGLEVEL lv) { m_level=lv; }
   ENUM_APEX_LOGLEVEL Level(void) const { return m_level; }

   void              Error(const string m) { WriteLine(APEX_LOG_ERROR,m); }
   void              Warn (const string m) { WriteLine(APEX_LOG_WARN ,m); }
   void              Info (const string m) { WriteLine(APEX_LOG_INFO ,m); }
   void              Debug(const string m) { WriteLine(APEX_LOG_DEBUG,m); }
  };

#endif // APEX_LOGGER_MQH
