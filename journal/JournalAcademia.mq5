//+------------------------------------------------------------------+
//|                                           JournalAcademia.mq5     |
//|                                                          v2.00    |
//|                                                                   |
//|  Envia al Journal de la academia las posiciones CERRADAS de esta  |
//|  cuenta. No abre, no modifica y no cierra nada: el codigo no      |
//|  contiene ni una llamada a OrderSend.                             |
//|                                                                   |
//|  INSTALACION                                                      |
//|  1. Copiar en MQL5/Experts y compilar (F7). Debe decir 0 errores. |
//|  2. Herramientas > Opciones > Asesores Expertos:                  |
//|     marcar "Permitir WebRequest para las siguientes URL" y anadir |
//|     la direccion del servidor tal cual, sin barra final.          |
//|  3. Arrastrar a un grafico que vaya a quedarse abierto y pegar    |
//|     el codigo de vinculacion que da la plataforma.                |
//+------------------------------------------------------------------+
#property copyright "Academia"
#property version   "2.00"
#property strict
#property description "Importa posiciones cerradas de MetaTrader 5 al Journal."
#property description "Solo lectura: no abre, modifica ni cierra operaciones."

input string ServidorURL        = "http://127.0.0.1:8787"; // Direccion del servidor
input string CodigoVinculo      = "";                      // Codigo de la plataforma
input int    DiasHistorico      = 365;                     // Historico a importar
input int    SegundosSincro     = 15;                      // Cada cuanto revisa
input int    MaxPorCiclo        = 40;                      // Operaciones por envio
input string EtiquetaCuenta     = "";                      // Nombre para distinguir cuentas
input bool   ImportarHistorico  = true;                    // Enviar lo ya cerrado
input bool   ReenviarTodo       = false;                   // Poner a true UNA vez para reimportar
input bool   Verboso            = true;                    // Detalle en el registro

#define EA_VERSION "2.00"

bool     g_pendiente     = true;
datetime g_ultimo_latido = 0;
datetime g_ultimo_barrido= 0;
string   g_marca         = "";
int      g_desfase_gmt   = 0;

struct Operacion
  {
   long     posicion;
   string   simbolo;
   string   sentido;
   datetime abre;
   datetime cierra;
   double   precio_abre;
   double   precio_cierra;
   double   sl;
   double   tp;
   double   sl_pips;
   double   tp_pips;
   double   volumen;
   double   bruto;
   double   comision;
   double   swap;
   double   tasa;
   double   neto;
   double   saldo_antes;
   double   riesgo_dinero;
   double   riesgo_pct;
   double   resultado_pct;
   double   resultado_r;
   double   rr_previsto;
   int      duracion_min;
   string   motivo_cierre;
   string   origen;
   string   comentario;
   long     magico;
  };

//+------------------------------------------------------------------+
//| Ciclo de vida                                                     |
//+------------------------------------------------------------------+
int OnInit()
  {
   if(StringLen(CodigoVinculo)<6)
     {
      Print("[Journal] Falta el codigo de vinculacion. Generalo en la plataforma ",
            "y pegalo en las propiedades del experto.");
      return(INIT_FAILED);
     }

   // Desfase del servidor del broker respecto a UTC. Sin esto, el analisis
   // por sesiones (Asia, Londres, Nueva York) sale desplazado dos o tres
   // horas, que es justo lo que invalida ese analisis.
   g_desfase_gmt = (int)((long)TimeCurrent() - (long)TimeGMT());

   g_marca = "JA_" + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) + "_" +
             IntegerToString(HashServidor(AccountInfoString(ACCOUNT_SERVER))) + "_";

   if(ReenviarTodo)
      BorraMarcas();

   EventSetTimer((int)MathMax(5,SegundosSincro));
   EnviaLatido();

   if(!ImportarHistorico)
      MarcaHistoricoComoEnviado();

   g_pendiente = true;
   Log("Activo en la cuenta " + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) +
       ". Desfase del servidor respecto a UTC: " + IntegerToString(g_desfase_gmt/3600) + " h.");
   Log("Si aparece el error 4014 o 5200, autoriza " + ServidorURL +
       " en Herramientas > Opciones > Asesores Expertos.");
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int motivo)
  {
   EventKillTimer();
   Comment("");
  }

void OnTimer()
  {
   datetime ahora = TimeCurrent();
   if(ahora - g_ultimo_latido >= 30)
      EnviaLatido();

   if(g_pendiente || ahora - g_ultimo_barrido >= 120)
     {
      int n = Sincroniza();
      g_ultimo_barrido = ahora;
      // Si se lleno el cupo del ciclo, quedan mas: se sigue en el siguiente.
      g_pendiente = (n >= MathMax(1,MaxPorCiclo));
      Panel(n);
     }
  }

// Al cerrarse algo, se sincroniza sin esperar al temporizador.
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &req,
                        const MqlTradeResult &res)
  {
   if(trans.type==TRADE_TRANSACTION_DEAL_ADD  ||
      trans.type==TRADE_TRANSACTION_DEAL_UPDATE ||
      trans.type==TRADE_TRANSACTION_HISTORY_ADD ||
      trans.type==TRADE_TRANSACTION_POSITION)
      g_pendiente = true;
  }

void OnTick() { }   // el experto no opera

//+------------------------------------------------------------------+
//| Utilidades                                                        |
//+------------------------------------------------------------------+
void Log(string s) { if(Verboso) Print("[Journal] ", s); }

void Panel(const int enviados)
  {
   string s = "Journal de la academia\n";
   s += "Cuenta: " + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) + "\n";
   s += "Servidor: " + ServidorURL + "\n";
   s += "Ultimo ciclo: " + IntegerToString(enviados) + " operacion(es)\n";
   s += "Estado: activo · solo lectura";
   Comment(s);
  }

string EscapaJson(string v)
  {
   StringReplace(v,"\\","\\\\");
   StringReplace(v,"\"","\\\"");
   StringReplace(v,"\r","\\r");
   StringReplace(v,"\n","\\n");
   StringReplace(v,"\t","\\t");
   return v;
  }

string Num(const double v,const int dig)
  {
   if(!MathIsValidNumber(v)) return "0";
   return DoubleToString(v,dig);
  }

// Fecha ISO en UTC. El servidor guarda siempre UTC y ademas recibe el
// desfase, para poder reconstruir la hora del broker cuando haga falta.
string IsoUtc(const datetime t)
  {
   MqlDateTime d;
   TimeToStruct((datetime)((long)t - g_desfase_gmt), d);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",d.year,d.mon,d.day,d.hour,d.min,d.sec);
  }

string IsoBroker(const datetime t)
  {
   MqlDateTime d;
   TimeToStruct(t,d);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02d",d.year,d.mon,d.day,d.hour,d.min,d.sec);
  }

long HashServidor(const string t)
  {
   long h = 5381;
   for(int i=0;i<StringLen(t);i++)
      h = ((h<<5)+h) + (long)StringGetCharacter(t,i);
   if(h<0) h = -h;
   return h % 1000000000;
  }

//+------------------------------------------------------------------+
//| Marcas de enviado. Se guardan como variables globales del         |
//| terminal: sobreviven a reinicios y son por cuenta y servidor.     |
//+------------------------------------------------------------------+
string NombreMarca(const long pos) { return g_marca + IntegerToString(pos); }
bool   YaEnviada(const long pos)   { return GlobalVariableCheck(NombreMarca(pos)); }
void   MarcaEnviada(const long pos){ GlobalVariableSet(NombreMarca(pos),(double)TimeCurrent()); }

void BorraMarcas()
  {
   int n = 0;
   for(int i=GlobalVariablesTotal()-1;i>=0;i--)
     {
      string nm = GlobalVariableName(i);
      if(StringFind(nm,g_marca)==0) { GlobalVariableDel(nm); n++; }
     }
   Log("Marcas borradas: " + IntegerToString(n) + ". Se reenviara todo el historico.");
   Log("Vuelve a poner ReenviarTodo en false cuando termine.");
  }

void MarcaHistoricoComoEnviado()
  {
   datetime hasta = TimeCurrent()+60;
   datetime desde = hasta - (datetime)MathMax(1,DiasHistorico)*86400;
   if(!HistorySelect(desde,hasta)) return;
   long ids[];
   int n = RecogePosiciones(ids);
   for(int i=0;i<n;i++)
      if(!PosicionAbierta(ids[i]))
         MarcaEnviada(ids[i]);
   Log("Historico existente marcado como enviado: " + IntegerToString(n) + " posiciones.");
  }

//+------------------------------------------------------------------+
//| Recogida de posiciones del historial                              |
//+------------------------------------------------------------------+
int RecogePosiciones(long &ids[])
  {
   ArrayResize(ids,0);
   int total = HistoryDealsTotal();
   long vistos[];
   ArrayResize(vistos,0);
   int n = 0;
   for(int i=0;i<total;i++)
     {
      ulong tk = HistoryDealGetTicket(i);
      if(tk==0) continue;
      ENUM_DEAL_TYPE tipo = (ENUM_DEAL_TYPE)HistoryDealGetInteger(tk,DEAL_TYPE);
      if(tipo!=DEAL_TYPE_BUY && tipo!=DEAL_TYPE_SELL) continue;
      long pid = (long)HistoryDealGetInteger(tk,DEAL_POSITION_ID);
      if(pid<=0) continue;
      ArrayResize(vistos,n+1);
      vistos[n] = pid;
      n++;
     }
   if(n==0) return 0;
   // Ordenar y quitar repetidos: con miles de operaciones, buscar uno a uno
   // en un array sin ordenar cuesta el cuadrado del numero de registros.
   ArraySort(vistos);
   int m = 0;
   for(int i=0;i<n;i++)
     {
      if(i>0 && vistos[i]==vistos[i-1]) continue;
      ArrayResize(ids,m+1);
      ids[m] = vistos[i];
      m++;
     }
   return m;
  }

bool PosicionAbierta(const long id)
  {
   int total = PositionsTotal();
   for(int i=0;i<total;i++)
     {
      ulong tk = PositionGetTicket(i);
      if(tk==0) continue;
      if((long)PositionGetInteger(POSITION_IDENTIFIER)==id)
         return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
//| Construccion de una operacion completa                            |
//+------------------------------------------------------------------+
bool Construye(const long pos, Operacion &t)
  {
   t.posicion=pos; t.simbolo=""; t.sentido=""; t.abre=0; t.cierra=0;
   t.precio_abre=0; t.precio_cierra=0; t.sl=0; t.tp=0; t.sl_pips=0; t.tp_pips=0;
   t.volumen=0; t.bruto=0; t.comision=0; t.swap=0; t.tasa=0; t.neto=0;
   t.saldo_antes=0; t.riesgo_dinero=0; t.riesgo_pct=0; t.resultado_pct=0;
   t.resultado_r=0; t.rr_previsto=0; t.duracion_min=0;
   t.motivo_cierre="Cierre manual"; t.origen=""; t.comentario=""; t.magico=0;

   if(!HistorySelectByPosition(pos)) return false;

   double val_e=0, vol_e=0, val_s=0, vol_s=0;
   datetime ultima_salida=0;
   ENUM_DEAL_REASON motivo=DEAL_REASON_CLIENT;
   int total = HistoryDealsTotal();

   for(int i=0;i<total;i++)
     {
      ulong tk = HistoryDealGetTicket(i);
      if(tk==0) continue;

      // Comisiones y swaps se suman SIEMPRE, incluso de apuntes que no son
      // compra ni venta: si no, el resultado neto sale inflado.
      t.bruto    += HistoryDealGetDouble(tk,DEAL_PROFIT);
      t.comision += HistoryDealGetDouble(tk,DEAL_COMMISSION);
      t.swap     += HistoryDealGetDouble(tk,DEAL_SWAP);
      t.tasa     += HistoryDealGetDouble(tk,DEAL_FEE);

      ENUM_DEAL_TYPE tipo = (ENUM_DEAL_TYPE)HistoryDealGetInteger(tk,DEAL_TYPE);
      if(tipo!=DEAL_TYPE_BUY && tipo!=DEAL_TYPE_SELL) continue;

      ENUM_DEAL_ENTRY ent = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(tk,DEAL_ENTRY);
      datetime cuando = (datetime)HistoryDealGetInteger(tk,DEAL_TIME);
      double vol   = HistoryDealGetDouble(tk,DEAL_VOLUME);
      double prec  = HistoryDealGetDouble(tk,DEAL_PRICE);

      if(t.simbolo=="") t.simbolo = HistoryDealGetString(tk,DEAL_SYMBOL);
      if(t.magico==0)   t.magico  = HistoryDealGetInteger(tk,DEAL_MAGIC);

      if(ent==DEAL_ENTRY_IN || ent==DEAL_ENTRY_INOUT)
        {
         if(t.abre==0 || cuando<t.abre)
           {
            t.abre = cuando;
            t.sentido = (tipo==DEAL_TYPE_BUY ? "buy" : "sell");
            t.sl = HistoryDealGetDouble(tk,DEAL_SL);
            t.tp = HistoryDealGetDouble(tk,DEAL_TP);
            t.comentario = HistoryDealGetString(tk,DEAL_COMMENT);
           }
         val_e += prec*vol;
         vol_e += vol;
        }

      if(ent==DEAL_ENTRY_OUT || ent==DEAL_ENTRY_OUT_BY || ent==DEAL_ENTRY_INOUT)
        {
         val_s += prec*vol;
         vol_s += vol;
         if(cuando>=ultima_salida)
           {
            ultima_salida = cuando;
            t.cierra = cuando;
            motivo = (ENUM_DEAL_REASON)HistoryDealGetInteger(tk,DEAL_REASON);
           }
        }
     }

   if(t.simbolo=="" || t.abre==0 || t.cierra==0 || vol_e<=0 || vol_s<=0)
      return false;                                     // aun no cerrada del todo

   t.precio_abre   = val_e/vol_e;
   t.precio_cierra = val_s/vol_s;
   t.volumen       = vol_e;
   t.neto          = t.bruto + t.comision + t.swap + t.tasa;
   t.duracion_min  = (int)MathMax(0,(t.cierra-t.abre)/60);
   t.motivo_cierre = MotivoCierre(motivo,t.neto);
   t.origen        = Origen(motivo);
   t.saldo_antes   = SaldoAntes(t.abre);

   // Distancia del stop y del objetivo, en pips.
   double punto = SymbolInfoDouble(t.simbolo,SYMBOL_POINT);
   int    dig   = (int)SymbolInfoInteger(t.simbolo,SYMBOL_DIGITS);
   if(punto>0)
     {
      double pip = ((dig==3 || dig==5) ? punto*10.0 : punto);
      if(t.sl>0) t.sl_pips = MathAbs(t.precio_abre-t.sl)/pip;
      if(t.tp>0) t.tp_pips = MathAbs(t.tp-t.precio_abre)/pip;
     }

   // Riesgo en dinero: lo que habria perdido si salta el stop inicial.
   if(t.sl>0 && t.precio_abre>0)
     {
      double calc=0;
      ENUM_ORDER_TYPE ot = (t.sentido=="buy" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL);
      if(OrderCalcProfit(ot,t.simbolo,t.volumen,t.precio_abre,t.sl,calc))
         t.riesgo_dinero = MathAbs(calc);
     }

   if(t.saldo_antes>0)
     {
      t.resultado_pct = t.neto/t.saldo_antes*100.0;
      if(t.riesgo_dinero>0) t.riesgo_pct = t.riesgo_dinero/t.saldo_antes*100.0;
     }
   if(t.riesgo_dinero>0) t.resultado_r = t.neto/t.riesgo_dinero;

   double d_sl = MathAbs(t.precio_abre-t.sl);
   double d_tp = MathAbs(t.tp-t.precio_abre);
   if(t.sl>0 && t.tp>0 && d_sl>0) t.rr_previsto = d_tp/d_sl;

   return true;
  }

// Saldo justo antes de abrir. Se recorre el historial de la cuenta una vez.
double SaldoAntes(const datetime abre)
  {
   datetime hasta = TimeCurrent()+60;
   datetime desde = hasta - (datetime)MathMax(1,DiasHistorico)*86400;
   if(!HistorySelect(desde,hasta)) return 0;

   double despues = 0;
   int total = HistoryDealsTotal();
   for(int i=total-1;i>=0;i--)
     {
      ulong tk = HistoryDealGetTicket(i);
      if(tk==0) continue;
      if((datetime)HistoryDealGetInteger(tk,DEAL_TIME) < abre) break;   // el historial viene ordenado
      despues += HistoryDealGetDouble(tk,DEAL_PROFIT);
      despues += HistoryDealGetDouble(tk,DEAL_COMMISSION);
      despues += HistoryDealGetDouble(tk,DEAL_SWAP);
      despues += HistoryDealGetDouble(tk,DEAL_FEE);
     }
   return MathMax(0, AccountInfoDouble(ACCOUNT_BALANCE)-despues);
  }

string MotivoCierre(const ENUM_DEAL_REASON r,const double neto)
  {
   if(r==DEAL_REASON_SL) return "Stop Loss";
   if(r==DEAL_REASON_TP) return "Take Profit";
   if(r==DEAL_REASON_SO) return "Stop Out";
   if(MathAbs(neto)<0.01) return "Break even";
   return "Cierre manual";
  }

string Origen(const ENUM_DEAL_REASON r)
  {
   if(r==DEAL_REASON_CLIENT) return "Escritorio";
   if(r==DEAL_REASON_MOBILE) return "Movil";
   if(r==DEAL_REASON_WEB)    return "Web";
   if(r==DEAL_REASON_EXPERT) return "Experto";
   if(r==DEAL_REASON_SL)     return "Stop Loss";
   if(r==DEAL_REASON_TP)     return "Take Profit";
   if(r==DEAL_REASON_SO)     return "Stop Out";
   return "Servidor";
  }

string OperacionJson(const Operacion &t)
  {
   string login  = IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN));
   string serv   = AccountInfoString(ACCOUNT_SERVER);
   string ext    = "mt5:" + serv + ":" + login + ":" + IntegerToString(t.posicion);

   string j = "{";
   j += "\"idExterno\":\""   + EscapaJson(ext) + "\",";
   j += "\"ticket\":"        + IntegerToString(t.posicion) + ",";
   j += "\"symbol\":\""      + EscapaJson(t.simbolo) + "\",";
   j += "\"type\":\""        + t.sentido + "\",";
   j += "\"volume\":"        + Num(t.volumen,4) + ",";
   j += "\"open_time\":\""   + IsoUtc(t.abre) + "\",";
   j += "\"close_time\":\""  + IsoUtc(t.cierra) + "\",";
   j += "\"open_time_broker\":\""  + IsoBroker(t.abre) + "\",";
   j += "\"close_time_broker\":\"" + IsoBroker(t.cierra) + "\",";
   j += "\"open_price\":"    + Num(t.precio_abre,8) + ",";
   j += "\"close_price\":"   + Num(t.precio_cierra,8) + ",";
   j += "\"sl\":"            + Num(t.sl,8) + ",";
   j += "\"tp\":"            + Num(t.tp,8) + ",";
   j += "\"sl_pips\":"       + Num(t.sl_pips,1) + ",";
   j += "\"tp_pips\":"       + Num(t.tp_pips,1) + ",";
   j += "\"profit\":"        + Num(t.bruto,2) + ",";
   j += "\"commission\":"    + Num(t.comision,2) + ",";
   j += "\"swap\":"          + Num(t.swap,2) + ",";
   j += "\"fee\":"           + Num(t.tasa,2) + ",";
   j += "\"neto\":"          + Num(t.neto,2) + ",";
   j += "\"saldo_antes\":"   + Num(t.saldo_antes,2) + ",";
   j += "\"riesgo\":"        + Num(t.riesgo_dinero,2) + ",";
   j += "\"riesgo_pct\":"    + Num(t.riesgo_pct,4) + ",";
   j += "\"resultado_pct\":" + Num(t.resultado_pct,4) + ",";
   j += "\"r\":"             + Num(t.resultado_r,4) + ",";
   j += "\"rr_previsto\":"   + Num(t.rr_previsto,4) + ",";
   j += "\"duracion_min\":"  + IntegerToString(t.duracion_min) + ",";
   j += "\"motivo\":\""      + EscapaJson(t.motivo_cierre) + "\",";
   j += "\"origen\":\""      + EscapaJson(t.origen) + "\",";
   j += "\"magic\":"         + IntegerToString(t.magico) + ",";
   j += "\"comment\":\""     + EscapaJson(t.comentario) + "\"";
   j += "}";
   return j;
  }

//+------------------------------------------------------------------+
//| Envio                                                             |
//+------------------------------------------------------------------+
int Sincroniza()
  {
   datetime hasta = TimeCurrent()+60;
   datetime desde = hasta - (datetime)MathMax(1,DiasHistorico)*86400;
   if(!HistorySelect(desde,hasta))
     {
      Print("[Journal] HistorySelect fallo. Error ", GetLastError());
      return 0;
     }

   long ids[];
   int total = RecogePosiciones(ids);
   if(total==0) return 0;

   string lote = "";
   long enviadas[];
   ArrayResize(enviadas,0);
   int n = 0;

   // De la mas reciente hacia atras: lo ultimo aparece antes en el panel.
   for(int i=total-1; i>=0 && n<MathMax(1,MaxPorCiclo); i--)
     {
      long pid = ids[i];
      if(pid<=0 || YaEnviada(pid) || PosicionAbierta(pid)) continue;

      Operacion t;
      if(!Construye(pid,t)) continue;

      if(n>0) lote += ",";
      lote += OperacionJson(t);
      ArrayResize(enviadas,n+1);
      enviadas[n] = pid;
      n++;

      // Construye() y SaldoAntes() cambian la seleccion del historial.
      if(!HistorySelect(desde,hasta)) break;
     }

   if(n==0) return 0;

   string cuerpo = "{";
   cuerpo += "\"cuenta\":" + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
   cuerpo += "\"servidor\":\"" + EscapaJson(AccountInfoString(ACCOUNT_SERVER)) + "\",";
   cuerpo += "\"desfase_gmt\":" + IntegerToString(g_desfase_gmt/3600) + ",";
   cuerpo += "\"saldo\":" + Num(AccountInfoDouble(ACCOUNT_BALANCE),2) + ",";
   cuerpo += "\"operaciones\":[" + lote + "]}";

   int cod = Envia("/ea/v1/operaciones",cuerpo);
   if(cod<200 || cod>=300)
     {
      Log("Envio fallido. Se reintenta en el proximo ciclo: no se pierde nada.");
      return 0;
     }

   for(int i=0;i<n;i++) MarcaEnviada(enviadas[i]);
   Log("Enviadas " + IntegerToString(n) + " operaciones.");
   return n;
  }

bool EnviaLatido()
  {
   string j = "{";
   j += "\"cuenta\":"        + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
   j += "\"servidor\":\""    + EscapaJson(AccountInfoString(ACCOUNT_SERVER)) + "\",";
   j += "\"broker\":\""      + EscapaJson(AccountInfoString(ACCOUNT_COMPANY)) + "\",";
   j += "\"titular\":\""     + EscapaJson(AccountInfoString(ACCOUNT_NAME)) + "\",";
   j += "\"etiqueta\":\""    + EscapaJson(EtiquetaCuenta) + "\",";
   j += "\"divisa\":\""      + EscapaJson(AccountInfoString(ACCOUNT_CURRENCY)) + "\",";
   j += "\"saldo\":"         + Num(AccountInfoDouble(ACCOUNT_BALANCE),2) + ",";
   j += "\"equity\":"        + Num(AccountInfoDouble(ACCOUNT_EQUITY),2) + ",";
   j += "\"apalancamiento\":"+ IntegerToString((long)AccountInfoInteger(ACCOUNT_LEVERAGE)) + ",";
   j += "\"tipo\":\""        + (AccountInfoInteger(ACCOUNT_TRADE_MODE)==ACCOUNT_TRADE_MODE_DEMO
                                ? "demo" : "real") + "\",";
   j += "\"margen\":\""      + ModoMargen() + "\",";
   j += "\"desfase_gmt\":"   + IntegerToString(g_desfase_gmt/3600) + ",";
   j += "\"abiertas\":"      + IntegerToString(PositionsTotal()) + ",";
   j += "\"ea\":\""          + EA_VERSION + "\"";
   j += "}";

   int cod = Envia("/ea/v1/cuenta",j);
   if(cod>=200 && cod<300) { g_ultimo_latido = TimeCurrent(); return true; }
   if(cod==401 || cod==403)
      Print("[Journal] El codigo de vinculacion no es valido. Genera uno nuevo en la plataforma.");
   return false;
  }

string ModoMargen()
  {
   ENUM_ACCOUNT_MARGIN_MODE m = (ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE);
   if(m==ACCOUNT_MARGIN_MODE_RETAIL_HEDGING) return "hedging";
   if(m==ACCOUNT_MARGIN_MODE_RETAIL_NETTING) return "netting";
   return "exchange";
  }

int Envia(const string ruta,const string cuerpo)
  {
   string url = ServidorURL + ruta;
   string cab = "Content-Type: application/json\r\n"
                "X-Journal-Codigo: " + CodigoVinculo + "\r\n";
   char peticion[], respuesta[];
   string cabResp = "";

   int c = StringToCharArray(cuerpo,peticion,0,WHOLE_ARRAY,CP_UTF8);
   if(c>0) ArrayResize(peticion,c-1);

   ResetLastError();
   int cod = WebRequest("POST",url,cab,15000,peticion,respuesta,cabResp);
   if(cod==-1)
     {
      int e = GetLastError();
      if(e==4014 || e==5200)
         Print("[Journal] ERROR: la URL no esta autorizada. Herramientas > Opciones > ",
               "Asesores Expertos > Permitir WebRequest, y anade exactamente: ", ServidorURL);
      else
         Print("[Journal] ERROR de red ", e, " al llamar a ", url);
      return -1;
     }
   if(cod<200 || cod>=300)
      Print("[Journal] El servidor respondio ", cod, ": ",
            CharArrayToString(respuesta,0,WHOLE_ARRAY,CP_UTF8));
   return cod;
  }
//+------------------------------------------------------------------+
