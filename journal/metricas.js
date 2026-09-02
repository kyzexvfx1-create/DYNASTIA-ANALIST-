"use strict";
/* ==========================================================================
   MOTOR DE METRICAS DEL JOURNAL
   Funciones puras: entra una lista de operaciones, sale el resumen. Sin DOM,
   sin red. Asi se puede probar entera y reutilizar en el servidor.

   Operacion esperada (la que envia el EA de MT5):
     { ticket, symbol, type:'buy'|'sell', volume, mercado,
       open_time, close_time (ISO o ms), open_price, close_price,
       sl, tp, commission, swap, profit }
   El resultado NETO de una operacion es profit + commission + swap.
   ========================================================================== */

const ms = t => (t == null ? null : (typeof t === 'number' ? t : Date.parse(t)));
const n2 = v => Math.round((v + Number.EPSILON) * 100) / 100;

/** Resultado neto: bruto menos comisiones, swaps y tasas.
 *  El EA ya lo manda calculado; si no viene, se compone aqui. */
function neto(o) {
  if (o.neto != null && isFinite(o.neto)) return +o.neto;
  return (+o.profit || 0) + (+o.commission || 0) + (+o.swap || 0) + (+o.fee || 0);
}

/** Operaciones cerradas, ordenadas por fecha de cierre. */
function cerradas(ops) {
  return (ops || [])
    .filter(o => o && ms(o.close_time) != null && o.close_time !== '')
    .sort((a, b) => ms(a.close_time) - ms(b.close_time));
}
function abiertas(ops) {
  return (ops || []).filter(o => o && (o.close_time == null || o.close_time === ''));
}

/* ---------- RESUMEN PRINCIPAL ---------- */
function resumen(ops, saldoInicial) {
  const C = cerradas(ops), A = abiertas(ops);
  const res = C.map(neto);
  const gan = res.filter(v => v > 0), per = res.filter(v => v < 0), pla = res.filter(v => v === 0);
  const total = res.reduce((s, v) => s + v, 0);
  const ini = +saldoInicial || 0;

  // Porcentaje medio: cada operacion sobre el saldo con el que se abrio, no
  // sobre el inicial. Si no, una cuenta que ha crecido infla sus porcentajes.
  const pct = [];
  let saldo = ini;
  C.forEach(o => {
    const r = neto(o);
    if (saldo > 0) pct.push((r / saldo) * 100);
    saldo += r;
  });
  const pctGan = pct.filter(v => v > 0), pctPer = pct.filter(v => v < 0);
  const med = a => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : 0);

  const brutoGan = gan.reduce((s, v) => s + v, 0);
  const brutoPer = Math.abs(per.reduce((s, v) => s + v, 0));

  return {
    saldoInicial: n2(ini),
    saldoActual: n2(ini + total),
    totalOperaciones: C.length,
    operacionesAbiertas: A.length,
    ganadoras: gan.length,
    perdedoras: per.length,
    planas: pla.length,
    // El acierto se mide sobre las que tuvieron resultado, no sobre las planas.
    tasaAcierto: (gan.length + per.length) ? n2((gan.length / (gan.length + per.length)) * 100) : 0,
    gananciaMedia: n2(med(gan)),
    perdidaMedia: n2(med(per)),
    gananciaMediaPct: n2(med(pctGan)),
    perdidaMediaPct: n2(med(pctPer)),
    mayorGanancia: n2(gan.length ? Math.max(...gan) : 0),
    mayorPerdida: n2(per.length ? Math.min(...per) : 0),
    resultadoTotal: n2(total),
    resultadoMedio: n2(C.length ? total / C.length : 0),
    rentabilidadPct: ini > 0 ? n2((total / ini) * 100) : 0,
    // Factor de beneficio: cuanto gana por cada euro que pierde.
    factorBeneficio: brutoPer > 0 ? n2(brutoGan / brutoPer) : (brutoGan > 0 ? Infinity : 0),
    // Esperanza matematica por operacion.
    esperanza: n2(C.length ? total / C.length : 0),
    comisiones: n2(C.reduce((s, o) => s + (+o.commission || 0), 0)),
    swaps: n2(C.reduce((s, o) => s + (+o.swap || 0), 0))
  };
}

/* ---------- CURVA DE CAPITAL Y CAIDA MAXIMA ---------- */
function curva(ops, saldoInicial) {
  const C = cerradas(ops);
  let saldo = +saldoInicial || 0, pico = saldo, caida = 0, caidaPct = 0;
  const pts = [{ t: C.length ? ms(C[0].close_time) - 864e5 : Date.now(), v: n2(saldo) }];
  C.forEach(o => {
    saldo += neto(o);
    if (saldo > pico) pico = saldo;
    const d = pico - saldo;
    if (d > caida) caida = d;
    if (pico > 0 && (d / pico) * 100 > caidaPct) caidaPct = (d / pico) * 100;
    pts.push({ t: ms(o.close_time), v: n2(saldo), op: o.ticket });
  });
  return { puntos: pts, caidaMaxima: n2(caida), caidaMaximaPct: n2(caidaPct), pico: n2(pico) };
}

/* ---------- RACHAS ---------- */
function rachas(ops) {
  const C = cerradas(ops);
  let g = 0, p = 0, maxG = 0, maxP = 0;
  C.forEach(o => {
    const r = neto(o);
    if (r > 0) { g++; p = 0; if (g > maxG) maxG = g; }
    else if (r < 0) { p++; g = 0; if (p > maxP) maxP = p; }
  });
  return { rachaGanadora: maxG, rachaPerdedora: maxP };
}

/* ---------- AGRUPACIONES ---------- */
function agrupa(ops, clave) {
  const C = cerradas(ops), m = new Map();
  C.forEach(o => {
    const k = clave(o);
    if (k == null) return;
    if (!m.has(k)) m.set(k, { clave: k, n: 0, resultado: 0, ganadas: 0, perdidas: 0 });
    const g = m.get(k);
    const r = neto(o);
    g.n++; g.resultado += r;
    if (r > 0) g.ganadas++; else if (r < 0) g.perdidas++;
  });
  return [...m.values()].map(g => ({
    clave: g.clave, n: g.n, resultado: n2(g.resultado),
    ganadas: g.ganadas, perdidas: g.perdidas,
    acierto: (g.ganadas + g.perdidas) ? n2((g.ganadas / (g.ganadas + g.perdidas)) * 100) : 0,
    cuota: 0
  }));
}
function conCuota(filas) {
  const t = filas.reduce((s, f) => s + f.n, 0) || 1;
  filas.forEach(f => f.cuota = n2((f.n / t) * 100));
  return filas;
}

const DIAS = ['domingo', 'lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado'];

function porSimbolo(ops)  { return conCuota(agrupa(ops, o => o.symbol || '—')).sort((a, b) => b.resultado - a.resultado); }
function porMercado(ops)  { return conCuota(agrupa(ops, o => o.mercado || mercadoDe(o.symbol))); }
function porDiaSemana(ops){ return conCuota(agrupa(ops, o => DIAS[new Date(ms(o.close_time)).getDay()])); }
function porHora(ops)     { return conCuota(agrupa(ops, o => new Date(ms(o.close_time)).getHours())).sort((a, b) => a.clave - b.clave); }
function porSentido(ops)  { return conCuota(agrupa(ops, o => (o.type || '').toLowerCase() === 'sell' ? 'venta' : 'compra')); }
function porDia(ops)      { return agrupa(ops, o => new Date(ms(o.close_time)).toISOString().slice(0, 10)).sort((a, b) => a.clave < b.clave ? -1 : 1); }
function porMes(ops)      { return agrupa(ops, o => new Date(ms(o.close_time)).toISOString().slice(0, 7)).sort((a, b) => a.clave < b.clave ? -1 : 1); }

/** Clasificacion de mercado a partir del simbolo, para el reparto por tipo. */
function mercadoDe(sym) {
  const s = String(sym || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
  if (/^(XAU|XAG|XPT|XPD|XCU)/.test(s)) return 'Metales';
  if (/^(BTC|ETH|SOL|XRP|ADA|DOGE|LTC|BNB|AVAX|LINK|DOT|MATIC|TRX|SHIB)/.test(s)) return 'Cripto';
  if (/^(US30|US500|USTEC|NAS100|SPX|GER40|DAX|UK100|JP225|FRA40|ESP35)/.test(s)) return 'Índices';
  if (/^(WTI|BRENT|USOIL|UKOIL|NGAS|XNG)/.test(s)) return 'Energía';
  if (/^[A-Z]{6}$/.test(s)) return 'Divisas';
  return 'Otros';
}

/* ---------- DURACION ---------- */
function duraciones(ops) {
  const C = cerradas(ops).filter(o => ms(o.open_time) != null);
  if (!C.length) return { mediaMin: 0, medianaMin: 0, masCortaMin: 0, masLargaMin: 0 };
  const d = C.map(o => (ms(o.close_time) - ms(o.open_time)) / 60000).sort((a, b) => a - b);
  const mitad = Math.floor(d.length / 2);
  return {
    mediaMin: n2(d.reduce((s, v) => s + v, 0) / d.length),
    medianaMin: n2(d.length % 2 ? d[mitad] : (d[mitad - 1] + d[mitad]) / 2),
    masCortaMin: n2(d[0]), masLargaMin: n2(d[d.length - 1])
  };
}

/* ---------- RIESGO ---------- */
function riesgo(ops, saldoInicial) {
  const C = cerradas(ops);
  const c = curva(ops, saldoInicial);
  const res = C.map(neto);
  const media = res.length ? res.reduce((s, v) => s + v, 0) / res.length : 0;
  const varianza = res.length > 1
    ? res.reduce((s, v) => s + (v - media) ** 2, 0) / (res.length - 1) : 0;
  const desv = Math.sqrt(varianza);
  const conSL = C.filter(o => +o.sl > 0).length;
  const total = res.reduce((s, v) => s + v, 0);
  return {
    caidaMaxima: c.caidaMaxima,
    caidaMaximaPct: c.caidaMaximaPct,
    // Cuanto beneficio ha sacado por cada euro de caida maxima soportada.
    ratioRecuperacion: c.caidaMaxima > 0 ? n2(total / c.caidaMaxima) : 0,
    desviacion: n2(desv),
    // Consistencia del tamano: si varia mucho, el riesgo no esta controlado.
    dispersionRiesgo: media !== 0 ? n2(desv / Math.abs(media)) : 0,
    conStopLoss: conSL,
    sinStopLoss: C.length - conSL,
    pctConStopLoss: C.length ? n2((conSL / C.length) * 100) : 0
  };
}

/* ---------- INFORME COMPLETO ---------- */
function informe(ops, saldoInicial) {
  return {
    resumen: resumen(ops, saldoInicial),
    curva: curva(ops, saldoInicial),
    rachas: rachas(ops),
    riesgo: riesgo(ops, saldoInicial),
    duracion: duraciones(ops),
    simbolos: porSimbolo(ops),
    mercados: porMercado(ops),
    diasSemana: porDiaSemana(ops),
    horas: porHora(ops),
    sentido: porSentido(ops),
    dias: porDia(ops),
    meses: porMes(ops),
    semanas: porSemana(ops, saldoInicial),
    sesiones: porSesion(ops),
    horasUTC: porHoraUTC(ops),
    enR: enR(ops),
    stops: stops(ops),
    motivos: porMotivo(ops),
    origenes: porOrigen(ops)
  };
}



/* ---------- SESIONES DE MERCADO ----------
   Se calculan sobre la hora UTC, nunca sobre la del broker: casi todos
   trabajan en UTC+2 o +3, y usar su hora desplaza el analisis entero. */
const SESIONES = [
  { n: 'Asia',            a: 23, b: 7,  d: 'Tokio y Sídney. Rangos estrechos, menos volumen.' },
  { n: 'Londres',         a: 7,  b: 12, d: 'Apertura europea. Suele marcar el rango del día.' },
  { n: 'Solape Londres-Nueva York', a: 12, b: 16, d: 'Las dos plazas abiertas. Máximo volumen y volatilidad.' },
  { n: 'Nueva York',      a: 16, b: 21, d: 'Tarde americana. Reacción a los datos de EE. UU.' },
  { n: 'Fuera de sesión', a: 21, b: 23, d: 'Cierre del día. Liquidez muy baja.' }
];
function sesionDe(ts) {
  const h = new Date(ts).getUTCHours();
  for (const s of SESIONES) {
    if (s.a < s.b ? (h >= s.a && h < s.b) : (h >= s.a || h < s.b)) return s.n;
  }
  return 'Fuera de sesión';
}
/** Reparto por sesión, según la hora de APERTURA: es cuando se decide. */
function porSesion(ops) {
  const filas = conCuota(agrupa(ops, o => sesionDe(ms(o.open_time) != null ? ms(o.open_time) : ms(o.close_time))));
  const orden = SESIONES.map(s => s.n);
  filas.forEach(f => { const s = SESIONES.find(x => x.n === f.clave); f.desc = s ? s.d : ''; });
  return filas.sort((a, b) => orden.indexOf(a.clave) - orden.indexOf(b.clave));
}
function porHoraUTC(ops) {
  return conCuota(agrupa(ops, o => new Date(ms(o.open_time) || ms(o.close_time)).getUTCHours()))
    .sort((a, b) => a.clave - b.clave);
}

/* ---------- MULTIPLO R ----------
   R es el resultado medido en unidades de riesgo. Ganar 2R significa ganar
   el doble de lo que se arriesgaba. Es la unica forma honesta de comparar
   operaciones de tamano distinto. */
function enR(ops) {
  const C = cerradas(ops).filter(o => o.r != null && isFinite(o.r) && +o.riesgo > 0);
  if (!C.length) return { n: 0, sinRiesgo: cerradas(ops).length, esperanzaR: 0,
                          totalR: 0, mejorR: 0, peorR: 0, mediaGanadaR: 0, mediaPerdidaR: 0 };
  const r = C.map(o => +o.r);
  const g = r.filter(v => v > 0), p = r.filter(v => v < 0);
  const med = a => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : 0);
  return {
    n: C.length,
    sinRiesgo: cerradas(ops).length - C.length,
    totalR: n2(r.reduce((s, v) => s + v, 0)),
    esperanzaR: n2(med(r)),
    mejorR: n2(Math.max(...r)), peorR: n2(Math.min(...r)),
    mediaGanadaR: n2(med(g)), mediaPerdidaR: n2(med(p))
  };
}

/* ---------- STOPS Y OBJETIVOS ---------- */
function stops(ops) {
  const C = cerradas(ops);
  const conSL = C.filter(o => +o.sl_pips > 0), conTP = C.filter(o => +o.tp_pips > 0);
  const conRR = C.filter(o => +o.rr_previsto > 0);
  const conRiesgo = C.filter(o => +o.riesgo_pct > 0);
  const med = a => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : 0);
  const mediana = a => {
    if (!a.length) return 0;
    const x = [...a].sort((p, q) => p - q), m = Math.floor(x.length / 2);
    return x.length % 2 ? x[m] : (x[m - 1] + x[m]) / 2;
  };
  const rp = conRiesgo.map(o => +o.riesgo_pct);
  return {
    slMedioPips: n2(med(conSL.map(o => +o.sl_pips))),
    slMedianaPips: n2(mediana(conSL.map(o => +o.sl_pips))),
    tpMedioPips: n2(med(conTP.map(o => +o.tp_pips))),
    rrMedioPrevisto: n2(med(conRR.map(o => +o.rr_previsto))),
    conStop: conSL.length, sinStop: C.length - conSL.length,
    pctConStop: C.length ? n2((conSL.length / C.length) * 100) : 0,
    riesgoMedioPct: n2(med(rp)),
    riesgoMaxPct: rp.length ? n2(Math.max(...rp)) : 0,
    // Si el riesgo por operacion baila mucho, no hay sistema: hay impulsos.
    riesgoConsistente: rp.length > 2
      ? n2(Math.sqrt(rp.reduce((s, v) => s + (v - med(rp)) ** 2, 0) / (rp.length - 1)) / (med(rp) || 1))
      : 0
  };
}

/* ---------- COMO SE CIERRAN ---------- */
function porMotivo(ops) {
  return conCuota(agrupa(ops, o => o.motivo || 'Sin dato'))
    .sort((a, b) => b.n - a.n);
}
function porOrigen(ops) {
  return conCuota(agrupa(ops, o => o.origen || 'Sin dato')).sort((a, b) => b.n - a.n);
}

/* ---------- RENTABILIDAD SEMANAL ---------- */
function claveSemana(ts) {
  const d = new Date(ts);
  const j = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const dow = (j.getUTCDay() + 6) % 7;
  j.setUTCDate(j.getUTCDate() - dow);                    // lunes de esa semana
  return j.toISOString().slice(0, 10);
}
/** Resultado por semana, con el saldo al inicio de cada una para el %. */
function porSemana(ops, saldoInicial) {
  const C = cerradas(ops);
  const filas = [];
  let saldo = +saldoInicial || 0, actual = null;
  C.forEach(o => {
    const k = claveSemana(ms(o.close_time));
    if (!actual || actual.clave !== k) {
      actual = { clave: k, n: 0, resultado: 0, ganadas: 0, perdidas: 0, saldoInicio: saldo };
      filas.push(actual);
    }
    const r = neto(o);
    actual.n++; actual.resultado += r;
    if (r > 0) actual.ganadas++; else if (r < 0) actual.perdidas++;
    saldo += r;
  });
  return filas.map(f => ({
    clave: f.clave, n: f.n, ganadas: f.ganadas, perdidas: f.perdidas,
    resultado: n2(f.resultado),
    pct: f.saldoInicio > 0 ? n2((f.resultado / f.saldoInicio) * 100) : 0,
    saldoInicio: n2(f.saldoInicio), saldoFin: n2(f.saldoInicio + f.resultado),
    acierto: (f.ganadas + f.perdidas) ? n2((f.ganadas / (f.ganadas + f.perdidas)) * 100) : 0
  }));
}

if (typeof module !== 'undefined') module.exports = {
  neto, cerradas, abiertas, resumen, curva, rachas, agrupa, porSimbolo, porMercado,
  porDiaSemana, porHora, porSentido, porDia, porMes, mercadoDe, duraciones, riesgo, informe,
  SESIONES, sesionDe, porSesion, porHoraUTC, enR, stops, porMotivo, porOrigen,
  claveSemana, porSemana
};
