const M = require('./metricas.js');
let ok = 0, n = 0;
const chk = (t, c, extra) => { n++; if (c) ok++; console.log((c ? '  OK  ' : '  MAL ') + t + (c ? '' : '   -> ' + extra)); };
const op = (o) => Object.assign({ ticket: Math.random(), symbol: 'EURUSD', type: 'buy', volume: .1,
  open_time: '2026-08-01T09:00:00Z', close_time: '2026-08-01T11:00:00Z',
  open_price: 1.1, close_price: 1.1, sl: 1.09, tp: 1.12, commission: 0, swap: 0, profit: 0 }, o);

/* --- 1. Caso comprobado a mano --- */
const A = [
  op({ profit: 100, commission: -2, swap: -1 }),   // neto  97
  op({ profit: -50, commission: -2, swap: 0 }),    // neto -52
  op({ profit: 200, commission: -4, swap: -2 }),   // neto 194
  op({ profit: -30, commission: -1, swap: 0 }),    // neto -31
  op({ profit: 0,   commission: 0,  swap: 0 })     // plana
];
const r = M.resumen(A, 10000);
chk('neto descuenta comisión y swap', M.neto(A[0]) === 97, M.neto(A[0]));
chk('resultado total = 208', r.resultadoTotal === 208, r.resultadoTotal);
chk('saldo actual = 10208', r.saldoActual === 10208, r.saldoActual);
chk('total operaciones = 5', r.totalOperaciones === 5, r.totalOperaciones);
chk('ganadoras 2 / perdedoras 2 / planas 1', r.ganadoras===2 && r.perdedoras===2 && r.planas===1, [r.ganadoras,r.perdedoras,r.planas]);
chk('acierto 50% (las planas no cuentan)', r.tasaAcierto === 50, r.tasaAcierto);
chk('ganancia media = 145.5', r.gananciaMedia === 145.5, r.gananciaMedia);
chk('pérdida media = -41.5', r.perdidaMedia === -41.5, r.perdidaMedia);
chk('mayor ganancia 194 / mayor pérdida -52', r.mayorGanancia===194 && r.mayorPerdida===-52, [r.mayorGanancia,r.mayorPerdida]);
chk('factor de beneficio = 291/83 = 3.51', r.factorBeneficio === 3.51, r.factorBeneficio);
chk('resultado medio = 41.6', r.resultadoMedio === 41.6, r.resultadoMedio);
chk('rentabilidad = 2.08%', r.rentabilidadPct === 2.08, r.rentabilidadPct);
chk('comisiones = -9, swaps = -3', r.comisiones === -9 && r.swaps === -3, [r.comisiones, r.swaps]);

/* --- 2. Curva y caída máxima --- */
const c = M.curva(A, 10000);
chk('la curva empieza en el saldo inicial', c.puntos[0].v === 10000, c.puntos[0].v);
chk('la curva acaba en el saldo final', c.puntos[c.puntos.length-1].v === 10208, c.puntos.at(-1).v);
// 10000 -> 10097 -> 10045 -> 10239 -> 10208 -> 10208
// pico 10097 cuando cae a 10045 => 52 ; pico 10239 cuando cae a 10208 => 31
// La mayor de las dos es 52, y 52/10097 = 0,52 %.
chk('caída máxima = 52 (la mayor de las dos)', c.caidaMaxima === 52, c.caidaMaxima);
chk('caída máxima % = 0.52', c.caidaMaximaPct === 0.52, c.caidaMaximaPct);

/* --- 3. Rachas --- */
const R = M.rachas([op({profit:10}),op({profit:10}),op({profit:10}),op({profit:-5}),op({profit:-5}),op({profit:20})]);
chk('racha ganadora 3, perdedora 2', R.rachaGanadora===3 && R.rachaPerdedora===2, R);

/* --- 4. Operaciones abiertas --- */
const B = [op({profit:50}), op({close_time:null, profit:0}), op({close_time:'', profit:0})];
chk('las abiertas no cuentan en el resultado', M.resumen(B,1000).resultadoTotal === 50, M.resumen(B,1000).resultadoTotal);
chk('cuenta 2 abiertas', M.resumen(B,1000).operacionesAbiertas === 2, M.resumen(B,1000).operacionesAbiertas);

/* --- 5. Clasificación de mercado --- */
const casos = [['EURUSD','Divisas'],['XAUUSD','Metales'],['BTCUSD','Cripto'],['US500','Índices'],['USOIL','Energía'],['AAPL','Otros']];
casos.forEach(([s,e]) => chk('mercado de ' + s + ' = ' + e, M.mercadoDe(s) === e, M.mercadoDe(s)));

/* --- 6. Agrupación y reparto --- */
const D = [op({symbol:'EURUSD',profit:100}), op({symbol:'EURUSD',profit:-40}),
           op({symbol:'XAUUSD',profit:300}), op({symbol:'BTCUSD',profit:-10})];
const S = M.porSimbolo(D);
chk('ordena por resultado descendente', S[0].clave==='XAUUSD' && S[0].resultado===300, S[0]);
chk('agrupa EURUSD: 2 ops, +60, 50% acierto', S.find(x=>x.clave==='EURUSD').n===2 && S.find(x=>x.clave==='EURUSD').resultado===60 && S.find(x=>x.clave==='EURUSD').acierto===50, S.find(x=>x.clave==='EURUSD'));
const mk = M.porMercado(D);
chk('reparto por mercado suma 100%', Math.round(mk.reduce((s,f)=>s+f.cuota,0))===100, mk.map(f=>f.cuota));
chk('Divisas = 50% de las operaciones', mk.find(x=>x.clave==='Divisas').cuota===50, mk);

/* --- 7. Duración --- */
const du = M.duraciones([op({open_time:'2026-08-01T09:00:00Z',close_time:'2026-08-01T10:00:00Z'}),
                         op({open_time:'2026-08-01T09:00:00Z',close_time:'2026-08-01T12:00:00Z'}),
                         op({open_time:'2026-08-01T09:00:00Z',close_time:'2026-08-01T11:00:00Z'})]);
chk('duración media 120 min, mediana 120', du.mediaMin===120 && du.medianaMin===120, du);
chk('más corta 60, más larga 180', du.masCortaMin===60 && du.masLargaMin===180, du);

/* --- 8. Riesgo --- */
const rg = M.riesgo([op({profit:100,sl:1.09}), op({profit:-50,sl:0}), op({profit:20,sl:1.09})], 1000);
chk('cuenta stops: 2 con, 1 sin', rg.conStopLoss===2 && rg.sinStopLoss===1, rg);
chk('% con stop = 66.67', rg.pctConStopLoss===66.67, rg.pctConStopLoss);

/* --- 9. Bordes --- */
const v = M.informe([], 5000);
chk('cuenta vacía no rompe', v.resumen.totalOperaciones===0 && v.resumen.saldoActual===5000, v.resumen);
chk('sin pérdidas, factor de beneficio infinito', M.resumen([op({profit:10})],100).factorBeneficio === Infinity, M.resumen([op({profit:10})],100).factorBeneficio);
chk('saldo inicial 0 no divide por cero', M.resumen([op({profit:10})],0).rentabilidadPct === 0, M.resumen([op({profit:10})],0).rentabilidadPct);
chk('acepta fechas en milisegundos', M.resumen([op({close_time: Date.parse('2026-08-01T11:00:00Z'), profit: 5})],100).resultadoTotal===5, 'x');

/* --- 10. El % medio usa el saldo del momento, no el inicial --- */
const P = M.resumen([op({profit:100}), op({profit:100})], 1000);
// 1a: 100/1000 = 10% ; 2a: 100/1100 = 9.09% ; media 9.55
chk('% medio de ganancia = 9.55 (saldo vivo)', P.gananciaMediaPct === 9.55, P.gananciaMediaPct);

console.log('\n  ' + ok + '/' + n + ' correctos');
process.exit(ok === n ? 0 : 1);
