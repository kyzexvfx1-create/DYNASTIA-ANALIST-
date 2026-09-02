const M = require('./metricas.js');
let ok=0,n=0; const chk=(t,c,e)=>{n++;if(c)ok++;console.log((c?'  OK  ':'  MAL ')+t+(c?'':'  -> '+JSON.stringify(e)))};

// Operacion tal y como la manda el EA v2
const op = o => Object.assign({
  ticket: Math.random(), symbol: 'EURUSD', type: 'buy', volume: 0.1,
  open_time: '2026-08-17T09:30:00Z', close_time: '2026-08-17T11:00:00Z',
  open_price: 1.1000, close_price: 1.1050, sl: 1.0950, tp: 1.1100,
  sl_pips: 50, tp_pips: 100, profit: 50, commission: -2, swap: 0, fee: -0.5,
  neto: 47.5, saldo_antes: 10000, riesgo: 50, riesgo_pct: 0.5,
  resultado_pct: 0.475, r: 0.95, rr_previsto: 2, duracion_min: 90,
  motivo: 'Cierre manual', origen: 'Escritorio' }, o);

chk('neto usa el campo del EA, con tasas', M.neto(op({})) === 47.5, M.neto(op({})));
chk('si no viene neto, lo compone (50-2-0.5)', M.neto(op({neto:null})) === 47.5, M.neto(op({neto:null})));

// --- SESIONES (hora UTC de apertura) ---
const casos = [['2026-08-17T02:00:00Z','Asia'],['2026-08-17T08:30:00Z','Londres'],
  ['2026-08-17T13:00:00Z','Solape Londres-Nueva York'],['2026-08-17T18:00:00Z','Nueva York'],
  ['2026-08-17T22:00:00Z','Fuera de sesión'],['2026-08-17T23:30:00Z','Asia'],
  ['2026-08-17T06:59:00Z','Asia'],['2026-08-17T07:00:00Z','Londres']];
casos.forEach(([t,e]) => chk('sesión de las ' + t.slice(11,16) + ' UTC = ' + e, M.sesionDe(Date.parse(t))===e, M.sesionDe(Date.parse(t))));

const S = M.porSesion([op({open_time:'2026-08-17T02:00:00Z',neto:10}),
                       op({open_time:'2026-08-17T13:00:00Z',neto:100}),
                       op({open_time:'2026-08-17T13:30:00Z',neto:-40}),
                       op({open_time:'2026-08-17T18:00:00Z',neto:-5})]);
const sol = S.find(x=>x.clave==='Solape Londres-Nueva York');
chk('agrupa el solape: 2 ops, +60', sol.n===2 && sol.resultado===60, sol);
chk('el reparto suma 100%', Math.round(S.reduce((s,f)=>s+f.cuota,0))===100, S.map(f=>f.cuota));
chk('las sesiones salen en orden horario', S[0].clave==='Asia', S.map(f=>f.clave));
chk('cada sesión lleva su explicación', !!sol.desc, sol);

// --- MULTIPLO R ---
const R = M.enR([op({r:2,riesgo:50,neto:100}), op({r:-1,riesgo:50,neto:-50}),
                 op({r:3,riesgo:50,neto:150}), op({r:null,riesgo:0,neto:20})]);
chk('R: solo cuenta las que tenían stop', R.n===3 && R.sinRiesgo===1, R);
chk('R total = 4', R.totalR===4, R);
chk('esperanza 1.33 R por operación', R.esperanzaR===1.33, R);
chk('mejor 3R, peor -1R', R.mejorR===3 && R.peorR===-1, R);
chk('media ganada 2.5R', R.mediaGanadaR===2.5, R);

// --- STOPS ---
const T = M.stops([op({sl_pips:40,tp_pips:80,rr_previsto:2,riesgo_pct:0.5}),
                   op({sl_pips:60,tp_pips:60,rr_previsto:1,riesgo_pct:0.5}),
                   op({sl_pips:50,tp_pips:150,rr_previsto:3,riesgo_pct:0.5}),
                   op({sl_pips:0,tp_pips:0,rr_previsto:0,riesgo_pct:0,sl:0})]);
chk('stop medio 50 pips', T.slMedioPips===50, T);
chk('mediana del stop 50 pips', T.slMedianaPips===50, T);
chk('objetivo medio 96.67 pips', T.tpMedioPips===96.67, T);
chk('R:R previsto medio = 2', T.rrMedioPrevisto===2, T);
chk('3 con stop, 1 sin', T.conStop===3 && T.sinStop===1, T);
chk('75% con stop', T.pctConStop===75, T);
chk('riesgo medio 0.5%', T.riesgoMedioPct===0.5, T);
chk('riesgo perfectamente consistente = 0', T.riesgoConsistente===0, T);
const T2 = M.stops([op({riesgo_pct:0.5}),op({riesgo_pct:3}),op({riesgo_pct:0.2})]);
chk('riesgo disperso lo delata', T2.riesgoConsistente>1, T2.riesgoConsistente);

// --- MOTIVOS ---
const MO = M.porMotivo([op({motivo:'Stop Loss',neto:-50}), op({motivo:'Stop Loss',neto:-50}),
                        op({motivo:'Take Profit',neto:100}), op({motivo:'Cierre manual',neto:5})]);
chk('el motivo más frecuente es Stop Loss', MO[0].clave==='Stop Loss' && MO[0].n===2, MO);
chk('reparto de motivos suma 100%', Math.round(MO.reduce((s,f)=>s+f.cuota,0))===100, MO.map(f=>f.cuota));

// --- SEMANAS ---
chk('lunes de una semana', M.claveSemana(Date.parse('2026-08-19T10:00:00Z'))==='2026-08-17', M.claveSemana(Date.parse('2026-08-19T10:00:00Z')));
chk('el domingo cuenta en la semana que empieza el lunes anterior', M.claveSemana(Date.parse('2026-08-23T10:00:00Z'))==='2026-08-17', M.claveSemana(Date.parse('2026-08-23T10:00:00Z')));
const W = M.porSemana([op({close_time:'2026-08-18T10:00:00Z',neto:100}),
                       op({close_time:'2026-08-20T10:00:00Z',neto:-40}),
                       op({close_time:'2026-08-25T10:00:00Z',neto:200})], 10000);
chk('dos semanas', W.length===2, W);
chk('semana 1: +60 sobre 10000 = 0.6%', W[0].resultado===60 && W[0].pct===0.6, W[0]);
chk('semana 2 arranca con el saldo de la anterior', W[1].saldoInicio===10060, W[1]);
chk('semana 2: +200 sobre 10060 = 1.99%', W[1].pct===1.99, W[1]);
chk('saldo final encadenado', W[1].saldoFin===10260, W[1]);

// --- INFORME COMPLETO ---
const I = M.informe([op({}),op({neto:-30,r:-0.6})], 10000);
['resumen','curva','rachas','riesgo','duracion','simbolos','mercados','semanas','sesiones','horasUTC','enR','stops','motivos','origenes']
  .forEach(k => chk('el informe incluye ' + k, I[k] !== undefined, Object.keys(I)));
chk('cuenta vacía no rompe el informe ampliado', (()=>{const v=M.informe([],5000);return v.sesiones.length===0&&v.enR.n===0&&v.semanas.length===0})());

console.log('\n  ' + ok + '/' + n + ' correctos');
process.exit(ok===n?0:1);
