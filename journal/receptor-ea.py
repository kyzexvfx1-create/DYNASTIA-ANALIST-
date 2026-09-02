#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Receptor de pruebas del EA del Journal.

Sirve para comprobar HOY que el Expert Advisor habla bien con un servidor,
antes de que exista la plataforma. Guarda lo que recibe en operaciones.json
y calcula el resumen por pantalla.

    python3 receptor-ea.py

Despues, en el EA:
    ServidorURL   = http://localhost:8787
    CodigoVinculo = PRUEBA01

NO es el servidor de produccion. No tiene autenticacion real, ni base de
datos, ni cifrado. Es un banco de pruebas.
"""
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUERTO = 8787
ARCHIVO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "operaciones.json")

# En produccion esto sale de la base de datos: codigo -> id de alumno.
CODIGOS = {"PRUEBA01": "alumno-de-pruebas"}

CAMPOS = ("idExterno", "ticket", "symbol", "type", "volume",
          "open_time", "close_time", "open_time_broker", "close_time_broker",
          "open_price", "close_price", "sl", "tp", "sl_pips", "tp_pips",
          "profit", "commission", "swap", "fee", "neto", "saldo_antes",
          "riesgo", "riesgo_pct", "resultado_pct", "r", "rr_previsto",
          "duracion_min", "motivo", "origen", "magic", "comment")


def carga():
    if not os.path.exists(ARCHIVO):
        return {"cuentas": {}, "operaciones": {}}
    try:
        with open(ARCHIVO, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                    # noqa: BLE001
        return {"cuentas": {}, "operaciones": {}}


def guarda(d):
    tmp = ARCHIVO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ARCHIVO)                             # escritura atomica


def valida(op):
    """Devuelve (ok, motivo). Rechaza lo que no cuadre en vez de guardarlo mal."""
    for c in ("ticket", "symbol", "close_time"):
        if op.get(c) in (None, ""):
            return False, "falta " + c
    if str(op.get("type", "")).lower() not in ("buy", "sell"):
        return False, "tipo desconocido: %r" % op.get("type")
    for c in ("volume", "open_price", "close_price", "commission", "swap",
              "profit", "fee", "neto", "sl_pips", "riesgo", "r"):
        try:
            float(op.get(c, 0))
        except (TypeError, ValueError):
            return False, "%s no es un numero" % c
    if float(op.get("volume", 0)) <= 0:
        return False, "volumen cero"
    return True, ""


def neto(op):
    if op.get("neto") not in (None, ""):
        return float(op["neto"])
    return (float(op.get("profit", 0)) + float(op.get("commission", 0))
            + float(op.get("swap", 0)) + float(op.get("fee", 0)))


def resumen(ops):
    """Mismo criterio que metricas.js: resultado neto y las planas fuera del acierto."""
    res = [neto(o) for o in ops]
    g = [v for v in res if v > 0]
    p = [v for v in res if v < 0]
    bg, bp = sum(g), abs(sum(p))
    return {
        "operaciones": len(ops),
        "ganadoras": len(g),
        "perdedoras": len(p),
        "planas": len(res) - len(g) - len(p),
        "acierto": round(len(g) / (len(g) + len(p)) * 100, 2) if (g or p) else 0,
        "resultado": round(sum(res), 2),
        "media": round(sum(res) / len(res), 2) if res else 0,
        "factor": round(bg / bp, 2) if bp else (float("inf") if bg else 0),
    }


class Manejador(BaseHTTPRequestHandler):
    def _j(self, codigo, cuerpo):
        b = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass                                             # traza propia mas abajo

    def _alumno(self):
        return CODIGOS.get((self.headers.get("X-Journal-Codigo") or "").strip())

    def do_POST(self):
        alumno = self._alumno()
        if not alumno:
            print("  RECHAZADO  codigo de vinculacion no valido")
            return self._j(403, {"error": "codigo no valido"})

        try:
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:                           # noqa: BLE001
            print("  ERROR  cuerpo ilegible: %s" % e)
            return self._j(400, {"error": "cuerpo no valido"})

        base = carga()

        if self.path.rstrip("/") == "/ea/v1/cuenta":
            cta = str(d.get("cuenta"))
            base["cuentas"][cta] = dict(d, alumno=alumno,
                                        visto=datetime.now(timezone.utc).isoformat())
            guarda(base)
            print("\n  CUENTA  %s · %s · %s %s · apalancamiento 1:%s · %s · "
                  "servidor en UTC%+d · %s abiertas"
                  % (cta, d.get("broker"), d.get("saldo"), d.get("divisa"),
                     d.get("apalancamiento"), d.get("tipo"),
                     d.get("desfase_gmt") or 0, d.get("abiertas", "?")))
            return self._j(200, {"ok": True, "alumno": alumno})

        if self.path.rstrip("/") == "/ea/v1/operaciones":
            ops = d.get("operaciones") or []
            cta = str(d.get("cuenta"))
            guardadas = base["operaciones"].setdefault(cta, {})
            nuevas, repetidas, malas = 0, 0, []
            for op in ops:
                ok, motivo = valida(op)
                if not ok:
                    malas.append(motivo)
                    continue
                k = str(op.get("idExterno") or op["ticket"])
                if k in guardadas:                       # el EA puede reenviar: no se duplica
                    repetidas += 1
                    continue
                guardadas[k] = {c: op.get(c) for c in CAMPOS}
                nuevas += 1
            guarda(base)

            todas = list(guardadas.values())
            r = resumen(todas)
            print("  LOTE    %d recibidas · %d nuevas · %d repetidas · %d rechazadas"
                  % (len(ops), nuevas, repetidas, len(malas)))
            for m in malas[:5]:
                print("          rechazada: %s" % m)
            print("  TOTAL   %d operaciones · resultado %s · acierto %s%% · factor %s"
                  % (r["operaciones"], r["resultado"], r["acierto"], r["factor"]))
            return self._j(200, {"ok": True, "nuevas": nuevas,
                                 "repetidas": repetidas, "rechazadas": len(malas)})

        return self._j(404, {"error": "ruta desconocida"})

    def do_GET(self):
        if self.path.rstrip("/") in ("/ea/v1/estado", "/ea/v1"):
            base = carga()
            return self._j(200, {
                "cuentas": list(base["cuentas"].keys()),
                "resumen": {c: resumen(list(o.values()))
                            for c, o in base["operaciones"].items()}})
        return self._j(404, {"error": "ruta desconocida"})


def main():
    print("")
    print("  Receptor de pruebas del EA")
    print("  ==========================")
    print("  Escuchando en http://localhost:%d" % PUERTO)
    print("  Codigo de vinculacion para la prueba: PRUEBA01")
    print("  Los datos se guardan en: %s" % ARCHIVO)
    print("")
    print("  En el EA pon ServidorURL = http://localhost:%d" % PUERTO)
    print("  y autoriza esa direccion en Herramientas > Opciones > Asesores expertos.")
    print("")
    print("  Control+C para parar.")
    print("")
    try:
        ThreadingHTTPServer(("127.0.0.1", PUERTO), Manejador).serve_forever()
    except KeyboardInterrupt:
        print("\n  Detenido.\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
