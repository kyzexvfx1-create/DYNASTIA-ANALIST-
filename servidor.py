#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crypto Ops Terminal — servidor local en tiempo real.

Cambio de arquitectura frente a la version anterior: el sondeo ya no lo hace
el navegador, lo hace este proceso en segundo plano, y los hallazgos se
EMPUJAN al panel por Server-Sent Events. Consecuencias practicas:

  · Los titulares aparecen en cuanto la fuente los publica, no en la siguiente
    recarga. Latencia real: entre 20 y 60 segundos segun la prioridad.
  · Los nuevos listados se vigilan cada 60 segundos.
  · Se usan peticiones condicionales (ETag / If-Modified-Since): si la fuente
    no ha cambiado responde 304 y no se descarga nada. Eso permite sondear
    con frecuencia sin castigar a los medios ni acabar bloqueado.
  · Ya no dependes de proxies publicos gratuitos para nada.

Funciones expuestas:
  /                     el panel
  /api/stream           flujo SSE con novedades en vivo
  /api/snapshot         estado completo al conectar
  /api/live?ch=         estado de emision de un canal de YouTube
  /api/exchange  POST   firma HMAC contra Binance o Kraken (solo lectura)
  /proxy?url=           intermediario puntual, para lo que pida el panel

Solo biblioteca estandar. Escucha unicamente en 127.0.0.1.
"""

import base64
import datetime
import gzip
import hashlib
import hmac
import io
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# PUERTO: en local se usa 8787 de siempre. En Render/Railway viene dado por
# la variable de entorno PORT, así que se respeta si existe.
PUERTO = int(os.environ.get("PORT", 8787))
# HOST: 127.0.0.1 en local (como siempre). En Render/Railway hace falta
# 0.0.0.0 para que la plataforma pueda alcanzar el proceso desde fuera;
# se detecta automáticamente, o se puede forzar a mano con HOST=0.0.0.0.
HOST = os.environ.get(
    "HOST",
    "0.0.0.0" if os.environ.get("RENDER") or os.environ.get("RAILWAY_ENVIRONMENT")
    else "127.0.0.1"
)
RAIZ = os.path.dirname(os.path.abspath(__file__))
PAGINA = "crypto-ops-center.html"
PAGINA_FX = "forex-ops-center.html"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PERMITIDOS = (
    "cointelegraph.com", "coindesk.com", "theblock.co", "decrypt.co", "blockworks.co",
    "dlnews.com", "cryptoslate.com", "beincrypto.com", "bitcoinmagazine.com",
    "cryptopotato.com", "newsbtc.com", "bitcoinist.com", "ambcrypto.com", "u.today",
    "cryptobriefing.com", "thedefiant.io", "protos.com", "coinjournal.net", "coingape.com",
    "zycrypto.com", "coinspeaker.com", "finbold.com", "cryptonews.com", "news.bitcoin.com",
    "coinpedia.org", "blog.ethereum.org", "bitcoinops.org", "a16zcrypto.com",
    "criptonoticias.com", "observatorioblockchain.com", "bit2me.com", "uecdn.es",
    "elpais.com", "expansion.com", "news.google.com", "federalreserve.gov", "sec.gov",
    "cftc.gov", "ecb.europa.eu", "imf.org", "bis.org", "treasury.gov",
    "finance.yahoo.com", "dowjones.io", "investing.com", "reddit.com", "hnrss.org",
    "mempool.space", "coingecko.com", "binance.com", "alternative.me", "blockscout.com",
    "youtube.com", "kraken.com", "coinbase.com", "okx.com", "bybit.com",
    "faireconomy.media", "tradingeconomics.com", "bls.gov",
    "open.er-api.com", "er-api.com", "frankfurter.app", "frankfurter.dev",
    "translate.googleapis.com", "tradingview.com", "stooq.com", "stooq.pl",
    "api.anthropic.com", "api.openai.com",
)

# Credenciales del modelo de lenguaje. Igual que las de exchange: en memoria,
# nunca en disco, y se borran al cerrar la ventana del lanzador.
IA = {"proveedor": "anthropic", "clave": "", "modelo": ""}
_LOCK_IA = threading.Lock()
MODELO_POR_DEFECTO = {"anthropic": "claude-sonnet-5", "openai": "gpt-4o-mini"}

# Un modelo por tarea, decidido por donde escala el coste:
#   compartido = se calcula UNA vez para todos -> se puede pagar el mejor
#   por alumno = se multiplica por cada uno    -> el mas ligero que valga
MODELOS = {
    "sesgo":       "claude-opus-5",              # compartido · maxima precision
    "comparativa": "claude-sonnet-5",            # compartido
    "titulares":   "claude-sonnet-5",            # compartido
    "chat":        "claude-haiku-4-5-20251001",  # por alumno
    "journal":     "claude-sonnet-5",            # por alumno, puntual
}

# La busqueda NO se fuerza: se le da la herramienta y decide ella. Prioriza
# los datos del terminal, que son los que el alumno ve en pantalla, y solo
# sale a internet cuando le falta algo o sospecha que ha cambiado.
POLITICA_BUSQUEDA = (
    "\nTIENES BUSQUEDA EN INTERNET DISPONIBLE, pero no es tu fuente principal.\n"
    "Los datos del terminal son la referencia: son los que el alumno tiene delante "
    "y los que se han calculado con su misma metodologia. Trabaja con ellos.\n"
    "Busca en internet SOLO en estos casos:\n"
    "  - falta un dato que necesitas y el terminal no te lo da;\n"
    "  - hay indicios de que algo ha cambiado despues de la ultima lectura;\n"
    "  - un titular apunta a un suceso cuyo desenlace no conoces.\n"
    "Si buscas y lo que encuentras contradice al terminal, prevalece lo que "
    "encuentras y lo dices explicitamente. Si no necesitas buscar, no busques: "
    "responde con lo que tienes.")


def cargar_clave():
    """Lee la clave de un archivo aparte, nunca del panel.

    Se busca 'clave.txt' junto a este servidor, o la variable de entorno
    ANTHROPIC_API_KEY / OPENAI_API_KEY. Mantenerla fuera del HTML es
    deliberado: el panel se comparte, se copia y se presenta; la clave no.

    Formato de clave.txt (una linea basta):
        anthropic sk-ant-...
        modelo claude-sonnet-5        <- opcional
    """
    ruta = os.path.join(RAIZ, "clave.txt")
    prov = clave = modelo = ""
    if os.path.exists(ruta):
        try:
            for linea in open(ruta, encoding="utf-8"):
                linea = linea.strip()
                if not linea or linea.startswith("#"):
                    continue
                partes = linea.split(None, 1)
                if len(partes) != 2:
                    if linea.startswith("sk-ant-"):
                        prov, clave = "anthropic", linea
                    elif linea.startswith("sk-"):
                        prov, clave = "openai", linea
                    continue
                k, v = partes[0].lower(), partes[1].strip()
                if k in ("anthropic", "openai"):
                    prov, clave = k, v
                elif k == "modelo":
                    modelo = v
                elif k == "discord":
                    # Se descartan las etiquetas de ejemplo que la gente deja
                    # pegadas del manual: ID_DEL_SERVIDOR, EL_CLIENT_SECRET...
                    partes = [x for x in v.split()
                              if not re.fullmatch(r"[A-Z_]{4,}|<[^>]*>|\.{3}", x)]
                    DISCORD["sobrantes"] = max(0, len(partes) - 3)
                    if len(partes) >= 3:
                        DISCORD["id"], DISCORD["secreto"], DISCORD["guild"] = partes[:3]
                    elif len(partes) == 2:
                        DISCORD["id"], DISCORD["secreto"] = partes
                    elif len(partes) == 1:
                        DISCORD["id"] = partes[0]
                elif k == "discord_roles":
                    DISCORD["roles"] = [x.strip() for x in v.split(",") if x.strip()]
                elif k == "dominio":
                    DISCORD["dominio"] = v.strip()
                elif k == "tickets":
                    SOPORTE["webhook"] = v.strip()
                elif k == "soporte_correo":
                    SOPORTE["correo"] = v.strip()
                elif k == "stripe_enlace":
                    STRIPE["enlace"] = v.strip()
                elif k == "stripe_webhook":
                    STRIPE["webhook"] = v.strip()
        except Exception as e:                           # noqa: BLE001
            print("  aviso: no se pudo leer clave.txt (%s)" % e)
    if not clave:
        if os.environ.get("ANTHROPIC_API_KEY"):
            prov, clave = "anthropic", os.environ["ANTHROPIC_API_KEY"]
        elif os.environ.get("OPENAI_API_KEY"):
            prov, clave = "openai", os.environ["OPENAI_API_KEY"]
    if clave:
        with _LOCK_IA:
            IA["proveedor"] = prov or "anthropic"
            IA["clave"] = clave
            IA["modelo"] = modelo
        return True
    return False


# Herramienta de busqueda del lado del servidor de Anthropic. El identificador
# lleva version; si cambiara, la llamada se reintenta sin busqueda en lugar de
# fallar, de modo que el asistente nunca se queda mudo por esto.
TIPO_BUSQUEDA = "web_search_20250305"


def _texto_y_fuentes(d):
    """Extrae el texto y las paginas citadas de una respuesta de Anthropic."""
    partes, fuentes = [], []
    for c in d.get("content", []):
        if c.get("type") == "text":
            partes.append(c.get("text", ""))
            for cit in (c.get("citations") or []):
                u = cit.get("url")
                if u and u not in [f["url"] for f in fuentes]:
                    fuentes.append({"url": u, "titulo": cit.get("title") or u})
        elif c.get("type") == "web_search_tool_result":
            for r in (c.get("content") or []):
                u = r.get("url") if isinstance(r, dict) else None
                if u and u not in [f["url"] for f in fuentes]:
                    fuentes.append({"url": u, "titulo": (r.get("title") or u)})
    return "".join(partes), fuentes


# --------------------------------------------------------------------------
#  JOURNAL · RECEPCION DE OPERACIONES DE METATRADER 5
#
#  El experto instalado en el MT5 del alumno envia aqui las posiciones ya
#  cerradas. Solo entra informacion: no hay ninguna ruta que devuelva ordenes.
#  Se guarda en un archivo junto al servidor; en la plataforma esto sera una
#  base de datos, pero el contrato JSON es el mismo y no habra que tocar el EA.
# --------------------------------------------------------------------------
JOURNAL_ARCHIVO = os.path.join(RAIZ, "journal.json")
_LOCK_J = threading.Lock()

# En local basta un codigo fijo. En la plataforma sera codigo -> alumno.
CODIGOS_EA = {"PRUEBA01": "local"}

CAMPOS_OP = ("idExterno", "ticket", "symbol", "type", "volume",
             "open_time", "close_time", "open_time_broker", "close_time_broker",
             "open_price", "close_price", "sl", "tp", "sl_pips", "tp_pips",
             "profit", "commission", "swap", "fee", "neto", "saldo_antes",
             "riesgo", "riesgo_pct", "resultado_pct", "r", "rr_previsto",
             "duracion_min", "motivo", "origen", "magic", "comment")


def journal_carga():
    if not os.path.exists(JOURNAL_ARCHIVO):
        return {"cuentas": {}, "operaciones": {}}
    try:
        with open(JOURNAL_ARCHIVO, encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("cuentas", {})
        d.setdefault("operaciones", {})
        return d
    except Exception:                                    # noqa: BLE001
        return {"cuentas": {}, "operaciones": {}}


def journal_guarda(d):
    tmp = JOURNAL_ARCHIVO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, JOURNAL_ARCHIVO)                     # escritura atomica


def valida_op(op):
    for c in ("ticket", "symbol", "close_time"):
        if op.get(c) in (None, ""):
            return False, "falta " + c
    if str(op.get("type", "")).lower() not in ("buy", "sell"):
        return False, "tipo desconocido"
    for c in ("volume", "open_price", "close_price", "profit", "commission",
              "swap", "fee", "neto", "sl_pips", "riesgo", "r"):
        try:
            float(op.get(c, 0) or 0)
        except (TypeError, ValueError):
            return False, "%s no es un numero" % c
    if float(op.get("volume", 0) or 0) <= 0:
        return False, "volumen cero"
    return True, ""


# --------------------------------------------------------------------------
#  ACCESO CON DISCORD
#
#  Solo entra quien esta en el servidor de Discord de la academia. No se
#  gestionan contrasenas: no hay nada que filtrar, ni recuperacion de cuenta,
#  ni base de datos de credenciales.
#
#  Se configura en clave.txt, que NUNCA se comparte:
#      discord <client_id> <client_secret> <guild_id>
#      discord_roles <id_rol>,<id_rol>        (opcional)
#      dominio https://analyst.dynastia.es  (para la URL de retorno)
#
#  Si no esta configurado, el acceso queda abierto: asi el uso local de
#  siempre sigue funcionando sin tocar nada.
# --------------------------------------------------------------------------
DISCORD = {"id": "", "secreto": "", "guild": "", "roles": [], "dominio": "",
           "sobrantes": 0}
SESIONES = {}                    # token -> ficha del usuario
_LOCK_SES = threading.Lock()
VIDA_SESION = 30 * 86400
ESTADOS_OAUTH = {}               # state -> caducidad

RUTAS_LIBRES = ("/login", "/auth/discord", "/auth/discord/callback", "/logout",
                "/api/ping", "/api/acceso", "/favicon.ico",
                "/api/registro", "/api/login", "/stripe/webhook",
                "/pago", "/pago/completado")


def discord_activo():
    return bool(DISCORD["id"] and DISCORD["secreto"] and DISCORD["guild"])


def _base_url(handler):
    """URL publica del servicio. Detras de un proxy manda la cabecera.

    Se normaliza 'localhost' a '127.0.0.1' por dos motivos, y los dos dan
    errores desconcertantes si no se hace:
      - Discord compara la direccion de retorno como texto: localhost y
        127.0.0.1 no coinciden, y responde "Invalid OAuth2 redirect_uri".
      - Las cookies de uno no se envian al otro, asi que entrarias y
        apareceria que no has entrado.
    """
    if DISCORD["dominio"]:
        return DISCORD["dominio"].rstrip("/")
    proto = handler.headers.get("X-Forwarded-Proto") or "http"
    host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or "127.0.0.1:8787"
    if host.split(":")[0].lower() in ("localhost", "0.0.0.0", "[::1]", "::1"):
        puerto = host.split(":")[1] if ":" in host else str(PUERTO)
        host = "127.0.0.1:" + puerto
    return "%s://%s" % (proto, host)


def retorno_local():
    """La direccion de retorno que hay que registrar en Discord para probar."""
    return "http://127.0.0.1:%d/auth/discord/callback" % PUERTO


def nueva_sesion(ficha):
    tok = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    with _LOCK_SES:
        SESIONES[tok] = dict(ficha, creada=time.time())
        # Limpieza de las caducadas: la memoria no crece indefinidamente.
        for k in [k for k, v in SESIONES.items() if time.time() - v["creada"] > VIDA_SESION]:
            SESIONES.pop(k, None)
    return tok


def sesion_de(handler):
    galleta = handler.headers.get("Cookie") or ""
    tok = ""
    for parte in galleta.split(";"):
        parte = parte.strip()
        if parte.startswith("dyn_sesion="):
            tok = parte[11:]
            break
    if not tok:
        return None
    with _LOCK_SES:
        d = SESIONES.get(tok)
        if d and time.time() - d["creada"] > VIDA_SESION:
            SESIONES.pop(tok, None)
            return None
    return d


# --------------------------------------------------------------------------
#  CUENTAS PROPIAS + PAGO CON STRIPE (30E/mes)
#
#  Registro e inicio de sesion con correo y contrasena, sin depender de
#  Discord. Al entrar sin una suscripcion activa, se manda a /pago, que
#  lleva al enlace de Stripe ya creado. En cuanto Stripe confirma el
#  cobro, el webhook activa el acceso 31 dias; si cancela o falla un
#  cobro, se retira solo. Se guarda junto al servidor, en usuarios.json y
#  pagos.json, asi que sobrevive a reinicios.
#
#  Se configura en clave.txt, que NUNCA se comparte:
#      stripe_enlace https://buy.stripe.com/xxxxx
#      stripe_webhook whsec_...
#
#  Si no hay ni Discord ni Stripe configurados y no existe ninguna cuenta
#  todavia, el acceso queda abierto: el uso local de siempre sigue
#  funcionando sin tocar nada.
# --------------------------------------------------------------------------
import uuid as _uuid                                     # noqa: E402

STRIPE = {"enlace": "", "webhook": ""}
USUARIOS_ARCHIVO = os.path.join(RAIZ, "usuarios.json")
PAGOS_ARCHIVO = os.path.join(RAIZ, "pagos.json")
_LOCK_CUENTAS = threading.Lock()


def _carga_json(ruta, por_defecto):
    if not os.path.exists(ruta):
        return por_defecto
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                    # noqa: BLE001
        return por_defecto


def _guarda_json(ruta, datos):
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False)
    os.replace(tmp, ruta)                                # escritura atomica


def usuarios_carga():
    return _carga_json(USUARIOS_ARCHIVO, {})             # correo -> ficha


def pagos_carga():
    return _carga_json(PAGOS_ARCHIVO, {})                # id_usuario -> ficha


def stripe_activo():
    return bool(STRIPE["enlace"] and STRIPE["webhook"])


def login_activo():
    """Hay puerta de entrada si Discord o Stripe estan configurados, o si
    ya existe alguna cuenta creada (para no dejarla huerfana si se borran
    las claves despues)."""
    return discord_activo() or stripe_activo() or bool(usuarios_carga())


def _hash_clave(clave, sal=None):
    sal = sal or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), sal, 200_000)
    return sal.hex(), h.hex()


def _verifica_clave(clave, sal_hex, hash_hex):
    try:
        sal = bytes.fromhex(sal_hex)
    except ValueError:
        return False
    _, h = _hash_clave(clave, sal)
    return hmac.compare_digest(h, hash_hex)


def pago_activo(id_usuario):
    ficha = pagos_carga().get(id_usuario or "", {})
    return bool(ficha.get("activo") and ficha.get("hasta", 0) > time.time())


def _requiere_pago(ficha_sesion):
    """Solo las cuentas propias pasan por el muro de pago. El acceso via
    Discord sigue su propia logica de siempre (servidor/rol), sin tocarla."""
    return bool(ficha_sesion) and ficha_sesion.get("tipo") == "nativo" \
        and stripe_activo() and not pago_activo(ficha_sesion.get("id", ""))


def _uid_por_customer(customer_id):
    if not customer_id:
        return ""
    for uid, ficha in pagos_carga().items():
        if ficha.get("customer") == customer_id:
            return uid
    return ""


def _verifica_firma_stripe(cuerpo, cabecera_firma, secreto, tolerancia=300):
    """Verifica la cabecera Stripe-Signature: 't=<epoch>,v1=<hmac_hex>'.

    La firma valida es HMAC-SHA256 de '<epoch>.<cuerpo>' con el secreto
    del webhook (whsec_...). Si no coincide ninguna firma v1, o el
    instante es demasiado viejo, se rechaza.
    """
    if not secreto or not cabecera_firma:
        return False
    pares = [p.split("=", 1) for p in cabecera_firma.split(",") if "=" in p]
    ts = next((v for k, v in pares if k == "t"), "")
    firmas = [v for k, v in pares if k == "v1"]
    if not ts or not firmas:
        return False
    try:
        if abs(time.time() - int(ts)) > tolerancia:
            return False
    except ValueError:
        return False
    esperado = hmac.new(secreto.encode("utf-8"),
                         (ts + "." + cuerpo.decode("utf-8", "replace")).encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(esperado, f) for f in firmas)


def _procesa_evento_stripe(evento):
    tipo = evento.get("type", "")
    obj = (evento.get("data") or {}).get("object") or {}
    uid = obj.get("client_reference_id") or _uid_por_customer(obj.get("customer", ""))
    if not uid:
        return
    with _LOCK_CUENTAS:
        pagos = pagos_carga()
        ficha = pagos.get(uid, {})
        if tipo in ("checkout.session.completed", "invoice.paid"):
            ficha["activo"] = True
            ficha["hasta"] = time.time() + 31 * 86400
            if obj.get("customer"):
                ficha["customer"] = obj["customer"]
        elif tipo in ("customer.subscription.deleted", "invoice.payment_failed"):
            ficha["activo"] = False
        pagos[uid] = ficha
        _guarda_json(PAGOS_ARCHIVO, pagos)
    print("  pago      %s -> %s" % (tipo, uid))
    sys.stdout.flush()


PAGINA_CUENTA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dynastia Analyst</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    background:#0a0e0f;color:#d9e4e2;font-family:-apple-system,sans-serif}
  .caja{width:340px;padding:28px}
  h1{font-size:16px;margin:0 0 4px}
  p.sub{color:#5f7876;font-size:12.5px;margin:0 0 22px}
  .pestanas{display:flex;gap:6px;margin-bottom:18px}
  .pestanas button{flex:1;background:none;border:1px solid #1b2426;color:#5f7876;
    padding:8px;border-radius:8px;cursor:pointer;font-size:13px;font-family:inherit}
  .pestanas button.on{background:#0e1416;color:#d9e4e2;border-color:#e8b64f}
  input{width:100%;box-sizing:border-box;background:#0e1416;border:1px solid #1b2426;
    color:#d9e4e2;padding:10px 12px;border-radius:8px;font-size:13.5px;margin-bottom:10px}
  button.env{width:100%;background:#e8b64f;color:#0a0e0f;border:none;padding:10px;
    border-radius:8px;font-size:13.5px;font-weight:600;cursor:pointer;font-family:inherit}
  .e{color:#e8615c;font-size:12.5px;margin-bottom:10px;min-height:16px}
</style></head><body>
<div class="caja">
  <h1>Dynastia Analyst</h1>
  <p class="sub">Terminal de mercado &middot; acceso por suscripcion</p>
  <div class="pestanas">
    <button id="tIn" class="on">Iniciar sesion</button>
    <button id="tUp">Crear cuenta</button>
  </div>
  <div class="e" id="err"></div>
  <input id="email" type="email" placeholder="Correo electronico" autocomplete="username">
  <input id="clave" type="password" placeholder="Contrasena" autocomplete="current-password">
  <button class="env" id="ir">Entrar</button>
</div>
<script>
let modo = "login";
const $ = s => document.querySelector(s);
$("#tIn").onclick = () => { modo = "login"; $("#tIn").classList.add("on"); $("#tUp").classList.remove("on"); $("#ir").textContent = "Entrar"; };
$("#tUp").onclick = () => { modo = "registro"; $("#tUp").classList.add("on"); $("#tIn").classList.remove("on"); $("#ir").textContent = "Crear cuenta"; };
$("#ir").onclick = async () => {
  $("#err").textContent = "";
  const email = $("#email").value.trim(), clave = $("#clave").value;
  if (!email || clave.length < 6) { $("#err").textContent = "Correo valido y contrasena de 6 caracteres o mas."; return; }
  const ruta = modo === "login" ? "/api/login" : "/api/registro";
  try {
    const r = await fetch(ruta, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, clave }) });
    const d = await r.json();
    if (!r.ok) { $("#err").textContent = d.error || "No se pudo continuar."; return; }
    location.href = "/";
  } catch (e) { $("#err").textContent = "Fallo de conexion. Intentalo de nuevo."; }
};
</script></body></html>"""


PAGINA_PAGO = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dynastia Analyst - Suscripcion</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    background:#0a0e0f;color:#d9e4e2;font-family:-apple-system,sans-serif;text-align:center}
  .caja{width:360px;padding:28px}
  h1{font-size:17px;margin:0 0 8px}
  p{color:#8b93a3;font-size:13px;line-height:1.5;margin:0 0 22px}
  a.btn{display:inline-block;background:#e8b64f;color:#0a0e0f;padding:12px 26px;
    border-radius:8px;text-decoration:none;font-size:14px;font-weight:600}
  a.salir{display:block;margin-top:18px;color:#5f7876;font-size:12px}
</style></head><body>
<div class="caja">
  <h1>Falta activar tu suscripcion</h1>
  <p>El acceso al terminal es de 30&euro; al mes. En cuanto Stripe confirma el
     pago, entras al instante, sin tener que recargar nada.</p>
  <a class="btn" href="__ENLACE__">Suscribirme por 30&euro;/mes</a>
  <a class="salir" href="/logout">Cerrar sesion</a>
</div>
</body></html>"""


def _post_form(url, datos):
    cuerpo = urllib.parse.urlencode(datos).encode()
    b, _ = traer(url, headers={"Content-Type": "application/x-www-form-urlencoded"},
                 data=cuerpo, timeout=20)
    return json.loads(b)


def discord_intercambia(codigo, redirect):
    return _post_form("https://discord.com/api/oauth2/token", {
        "client_id": DISCORD["id"], "client_secret": DISCORD["secreto"],
        "grant_type": "authorization_code", "code": codigo, "redirect_uri": redirect})


def discord_yo(token):
    b, _ = traer("https://discord.com/api/users/@me",
                 headers={"Authorization": "Bearer " + token}, timeout=20)
    return json.loads(b)


def discord_miembro(token):
    """Ficha del usuario DENTRO del servidor de la academia.

    Si no pertenece, Discord responde 404 y se traduce a "no eres miembro".
    Requiere el permiso guilds.members.read, que es el que permite leer los
    roles sin necesidad de un bot.
    """
    try:
        b, _ = traer("https://discord.com/api/users/@me/guilds/%s/member" % DISCORD["guild"],
                     headers={"Authorization": "Bearer " + token}, timeout=20)
        return json.loads(b)
    except HTTPError as e:
        if e.code in (403, 404):
            return None
        raise


PAGINA_LOGIN = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dynastia Analyst</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Cpath d='M20 18 L50 93 L80 18 L50 52 Z M35 46 L50 81 L65 46 L50 62 Z' fill='%23a99bf0' fill-rule='evenodd'/%3E%3C/svg%3E"><style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#08090b;
 color:#c3c8cf;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.c{width:100%;max-width:420px;padding:38px 34px;background:#0d0f12;border:1px solid #1c2027;
 border-radius:14px;text-align:center}
@keyframes lgF{0%,100%{transform:rotateY(-13deg) rotateX(5deg) translateY(0)}
 50%{transform:rotateY(13deg) rotateX(-3deg) translateY(-3px)}}
@keyframes lgG{from{transform:rotateY(0)}to{transform:rotateY(360deg)}}
.lg{width:58px;height:58px;margin:0 auto 18px;perspective:300px}
.lg svg{width:100%;height:100%;display:block;animation:lgF 5s ease-in-out infinite;
 filter:drop-shadow(0 6px 20px rgba(169,155,240,.42))}
.c:hover .lg svg{animation:lgG 1.25s cubic-bezier(.42,0,.35,1)}
h1{margin:0 0 4px;font-size:25px;font-weight:700;letter-spacing:-.02em;color:#fff}
.s{font-size:11px;font-weight:700;letter-spacing:.22em;text-transform:uppercase;color:#6b7280;margin-bottom:26px}
p{font-size:13.5px;color:#8b929c;margin:0 0 24px}
a.b{display:flex;align-items:center;justify-content:center;gap:10px;background:#5865F2;color:#fff;
 text-decoration:none;font-weight:600;font-size:14.5px;padding:13px 18px;border-radius:9px;transition:.15s}
a.b:hover{background:#4752c4}
.e{margin-top:20px;padding:12px 14px;border-radius:9px;background:rgba(224,82,96,.09);
 border:1px solid rgba(224,82,96,.28);color:#ff9b9b;font-size:12.5px;text-align:left;line-height:1.5}
.f{margin-top:26px;font-size:10.5px;color:#565d66;line-height:1.5}
</style></head><body><div class="c">
<div class="lg"><svg viewBox="0 0 100 100">
<defs><linearGradient id="lg1" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#d6cdff"/><stop offset=".45" stop-color="#a99bf0"/>
<stop offset="1" stop-color="#5f4fb8"/></linearGradient></defs>
<path d="M20 18 L50 93 L80 18 L50 52 Z M35 46 L50 81 L65 46 L50 62 Z" fill="#3a2f78" fill-rule="evenodd" transform="translate(3,3.5)"/>
<path d="M20 18 L50 93 L80 18 L50 52 Z M35 46 L50 81 L65 46 L50 62 Z" fill="url(#lg1)" fill-rule="evenodd"/>
</svg></div>
<h1>Dynastia Analyst</h1><div class="s">Terminal de la academia</div>
<p>El acceso es para alumnos de la academia. Entra con la misma cuenta de Discord
con la que estas en nuestro servidor.</p>
<a class="b" href="/auth/discord">
<svg width="20" height="20" viewBox="0 0 127 96" fill="currentColor"><path d="M107 8A105 105 0 0 0 81 0l-2 4a78 78 0 0 0-32 0l-2-4a105 105 0 0 0-26 8C4 41-2 72 1 84a106 106 0 0 0 32 16l4-6a68 68 0 0 1-11-5l3-2a75 75 0 0 0 64 0l3 2a68 68 0 0 1-11 5l4 6a106 106 0 0 0 32-16c4-14-3-45-14-76ZM42 66c-6 0-12-6-12-13s5-13 12-13 12 6 12 13-5 13-12 13Zm43 0c-6 0-12-6-12-13s5-13 12-13 12 6 12 13-5 13-12 13Z"/></svg>
Entrar con Discord</a>
__ERROR__
<div class="f">No pedimos contrasenas. Solo comprobamos que perteneces al servidor.</div>
</div></body></html>"""


# --------------------------------------------------------------------------
#  MEMORIA DE DYN
#
#  Un modelo por API no se reentrena con el uso: cada llamada empieza en
#  blanco. Lo que si se puede es acumular conocimiento y darselo como
#  contexto. Eso es esto.
#
#  Tres piezas:
#    1. Se registra QUE se pregunta, clasificado por tema. No se guarda el
#       nombre del alumno, solo una huella: sirve para contar sin identificar.
#    2. Cuando un tema acumula preguntas suficientes, un proceso de fondo pide
#       a la IA que destile una NOTA DE MANUAL: la explicacion que mejor
#       resuelve esa duda recurrente, en lenguaje de academia.
#    3. En cada consulta se inyectan las notas del manual pertinentes.
#
#  El efecto es que Dyn mejora en lo que de verdad se pregunta aqui. El
#  mecanismo es un almacen que crece, no un modelo que aprende.
# --------------------------------------------------------------------------
MEM_ARCHIVO = os.path.join(RAIZ, "memoria_dyn.json")
_LOCK_MEM = threading.Lock()
MEM_TOPE_PREGUNTAS = 4000
MEM_MINIMO_NOTA = 6              # preguntas de un tema antes de destilar

TEMAS_DYN = [
    ("tipos",     r"tipo(s)? de inter|banco central|\bfed\b|fomc|\bbce\b|\bboe\b|\bboj\b|powell|lagarde|politica monetaria|recorte|subida de tipos"),
    ("inflacion", r"\bipc\b|inflaci|subyacente|\bcpi\b|\bppi\b|deflaci"),
    ("empleo",    r"\bnfp\b|nomina|payroll|paro|desempleo|empleo|jobless"),
    ("sesgo",     r"sesgo|direccion|alcista|bajista|neutro|reversion"),
    ("comparativa", r"comparativa|fuerza|que economia|mas fuerte|fondo macro"),
    ("niveles",   r"soporte|resistencia|pivote|nivel|rango|ruptura|media movil"),
    ("journal",   r"journal|mi operativa|mis operaciones|mi cuenta|drawdown|caida maxima|factor de beneficio|acierto|racha"),
    ("riesgo",    r"riesgo|stop|\bsl\b|\btp\b|apalancamiento|lotaje|tamano de posicion|\br:r\b|\bratio\b"),
    ("sesiones",  r"sesion|asia|londres|nueva york|solape|horario|a que hora"),
    ("bonos",     r"bono|rentabilidad|yield|deuda|diferencial|spread"),
    ("metales",   r"\boro\b|\bxau\b|plata|\bxag\b|metal"),
    ("terminal",  r"terminal|panel|pestana|boton|como funciona|donde veo|no me sale|no carga"),
    ("geopolitica", r"guerra|arancel|elecci|geopolit|conflicto|sancion|trump"),
]


def tema_de(texto):
    t = (texto or "").lower()
    for nombre, patron in TEMAS_DYN:
        if re.search(patron, t):
            return nombre
    return "otros"


def memoria_carga():
    if not os.path.exists(MEM_ARCHIVO):
        return {"preguntas": [], "temas": {}, "manual": []}
    try:
        with open(MEM_ARCHIVO, encoding="utf-8") as f:
            d = json.load(f)
        for k, v in (("preguntas", []), ("temas", {}), ("manual", [])):
            d.setdefault(k, v)
        return d
    except Exception:                                    # noqa: BLE001
        return {"preguntas": [], "temas": {}, "manual": []}


def memoria_guarda(d):
    tmp = MEM_ARCHIVO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, MEM_ARCHIVO)


def huella(alumno):
    """Identificador estable pero no reversible: contar sin identificar."""
    if not alumno:
        return ""
    return hashlib.sha256(("dyn:" + str(alumno)).encode()).hexdigest()[:12]


def memoria_registra(pregunta, alumno=""):
    p = (pregunta or "").strip()
    if len(p) < 8:
        return ""
    tema = tema_de(p)
    with _LOCK_MEM:
        d = memoria_carga()
        d["preguntas"].append({"t": int(time.time()), "tema": tema,
                               "q": p[:400], "a": huella(alumno)})
        if len(d["preguntas"]) > MEM_TOPE_PREGUNTAS:
            d["preguntas"] = d["preguntas"][-MEM_TOPE_PREGUNTAS:]
        c = d["temas"].setdefault(tema, {"n": 0, "ultima": 0})
        c["n"] += 1
        c["ultima"] = int(time.time())
        memoria_guarda(d)
    return tema


def memoria_contexto(pregunta, tope=4):
    """Notas del manual pertinentes para esta pregunta."""
    with _LOCK_MEM:
        d = memoria_carga()
    if not d["manual"]:
        return ""
    tema = tema_de(pregunta)
    propias = [x for x in d["manual"] if x.get("tema") == tema]
    otras = sorted([x for x in d["manual"] if x.get("tema") != tema],
                   key=lambda x: -(x.get("n") or 0))
    sel = (propias + otras)[:tope]
    if not sel:
        return ""
    return ("\n\nMANUAL DE LA ACADEMIA (destilado de lo que ya han preguntado otros "
            "alumnos; usalo si encaja, y si no, ignoralo):\n"
            + "\n".join("- [%s] %s" % (x.get("tema", ""), x.get("nota", "")) for x in sel))


SIS_DESTILAR = (
    "Eres el responsable de formacion de una academia de trading en Espana. Te dan "
    "un grupo de preguntas reales que han hecho los alumnos sobre un mismo tema. "
    "Escribes UNA nota de manual: la explicacion que resuelve de raiz esa duda, en "
    "dos o tres frases, en castellano llano, sin jerga sin aclarar y sin recomendar "
    "operaciones. La nota se usara como contexto para responder a futuros alumnos, "
    "asi que debe ser general y util, no la respuesta a una pregunta concreta. "
    "Responde solo con la nota, sin encabezados ni comillas.")


def destila_tema(tema, preguntas):
    lista = "\n".join("- " + p for p in preguntas[:30])
    r = llamar_ia(SIS_DESTILAR,
                  [{"role": "user", "content": "TEMA: %s\n\nPREGUNTAS DE ALUMNOS:\n%s\n\n"
                    "Escribe la nota de manual." % (tema, lista)}],
                  maxtok=400, buscar=False, modelo_pedido=MODELOS["titulares"], busquedas=0)
    return (r.get("texto") or "").strip()


def bucle_memoria():
    """Destila lo recurrente en notas de manual. Compartido: se paga una vez."""
    PARAR.wait(300)
    while not PARAR.is_set():
        with _LOCK_IA:
            hay = bool(IA["clave"])
        if hay:
            with _LOCK_MEM:
                d = memoria_carga()
            hechas = {x.get("tema"): x for x in d["manual"]}
            for tema, c in sorted(d["temas"].items(), key=lambda kv: -kv[1]["n"]):
                if PARAR.is_set():
                    break
                if tema == "otros" or c["n"] < MEM_MINIMO_NOTA:
                    continue
                ya = hechas.get(tema)
                # Se rehace cuando el tema ha crecido a la mitad otra vez.
                if ya and c["n"] < (ya.get("n") or 0) * 1.5:
                    continue
                qs = [x["q"] for x in d["preguntas"] if x["tema"] == tema][-30:]
                if len(qs) < MEM_MINIMO_NOTA:
                    continue
                try:
                    nota = destila_tema(tema, qs)
                except Exception as e:                   # noqa: BLE001
                    print("  memoria   %s no se pudo destilar: %s" % (tema, str(e)[:60]))
                    sys.stdout.flush()
                    continue
                if not nota:
                    continue
                with _LOCK_MEM:
                    d2 = memoria_carga()
                    d2["manual"] = [x for x in d2["manual"] if x.get("tema") != tema]
                    d2["manual"].append({"tema": tema, "nota": nota[:600],
                                         "t": int(time.time()), "n": c["n"]})
                    memoria_guarda(d2)
                print("  memoria   nota nueva sobre '%s' (%d preguntas)" % (tema, c["n"]))
                sys.stdout.flush()
                PARAR.wait(4)
            with _lock:
                ESTADO["diag"]["memoria"] = ("%d preguntas · %d temas · %d notas de manual"
                                             % (len(d["preguntas"]), len(d["temas"]),
                                                len(d["manual"])))
        PARAR.wait(3600)


def mensajes_previos(d):
    m = d.get("mensajes")
    return m if isinstance(m, list) else []


def _troceado(texto, ancho):
    palabras, linea, out = texto.split(), "", []
    for p in palabras:
        if len(linea) + len(p) + 1 > ancho:
            out.append(linea)
            linea = p
        else:
            linea = (linea + " " + p).strip()
    if linea:
        out.append(linea)
    return out


def revisa_discord():
    """Avisos de configuracion ANTES de que alguien intente entrar.

    Los tres errores tipicos son: poner un ID de aplicacion donde va el del
    servidor, dejar los roles de ejemplo del manual (con lo que no entra
    nadie), y olvidarse de una de las tres piezas. Se detectan aqui.
    """
    avisos = []
    if not discord_activo() and DISCORD["id"] and DISCORD["secreto"] and not DISCORD["guild"]:
        avisos.append("Falta el ID DEL SERVIDOR de Discord, que es el tercer valor. No lo "
                      "genera el portal de desarrolladores: se copia desde Discord con el "
                      "modo desarrollador activado, clic derecho en el icono del servidor.")
        return avisos
    if not discord_activo():
        if DISCORD["id"] or DISCORD["secreto"] or DISCORD["guild"]:
            faltan = [n for n, v in (("client_id", DISCORD["id"]),
                                     ("client_secret", DISCORD["secreto"]),
                                     ("id del servidor", DISCORD["guild"])) if not v]
            avisos.append("La linea 'discord' esta incompleta: falta %s. "
                          "El formato es: discord <client_id> <client_secret> "
                          "<id_del_servidor>" % " y ".join(faltan))
        return avisos

    for nombre, valor in (("client_id", DISCORD["id"]), ("id del servidor", DISCORD["guild"])):
        if not valor:
            avisos.append("Falta el %s. La linea 'discord' lleva tres valores: "
                          "client_id, client_secret e id_del_servidor." % nombre)
        elif not valor.isdigit():
            avisos.append("El %s no es un numero: %r" % (nombre, valor[:24]))
        elif not (17 <= len(valor) <= 20):
            avisos.append("El %s tiene %d digitos; los de Discord tienen entre 17 y 20. "
                          "Comprueba que no falta ningun caracter." % (nombre, len(valor)))
    if DISCORD["id"] and DISCORD["id"] == DISCORD["guild"]:
        avisos.append("El client_id y el id del servidor son el MISMO numero. El tercer "
                      "valor debe ser el de tu servidor de Discord: activa el modo "
                      "desarrollador y usa 'Copiar ID del servidor'.")
    else:
        # Error tipico: escribir el id de la aplicacion tambien en el tercer
        # hueco, con algun digito de menos. Comparten un prefijo largo.
        pref = 0
        for x, y in zip(DISCORD["id"], DISCORD["guild"]):
            if x != y:
                break
            pref += 1
        if pref >= 5 and DISCORD["id"] != DISCORD["guild"]:
            avisos.append("El client_id (%s) y el id del servidor (%s) empiezan por los "
                          "mismos %d digitos. Suele significar que en el tercer hueco se "
                          "ha copiado otra vez el de la aplicacion. El tercer valor es el "
                          "ID DEL SERVIDOR de Discord, no de la app."
                          % (DISCORD["id"], DISCORD["guild"], pref))
    if any(t in DISCORD["secreto"] for t in ("EL_CLIENT_SECRET", "TU_", "<", "...")):
        avisos.append("El client_secret sigue siendo el texto de ejemplo.")
    if DISCORD["secreto"].isdigit() and len(DISCORD["secreto"]) >= 15:
        avisos.append("El client_secret es un numero largo, y los secretos de Discord "
                      "son texto con letras. Parece que has puesto ahi otro ID.")
    if DISCORD["guild"] and not DISCORD["guild"].isdigit() and len(DISCORD["guild"]) > 20:
        avisos.append("El tercer valor (%s...) parece un client_secret, no el ID del "
                      "servidor. El orden es: client_id, client_secret, id_del_servidor, "
                      "los tres seguidos y SIN etiquetas."
                      % DISCORD["guild"][:10])
    if DISCORD.get("sobrantes"):
        avisos.append("La linea 'discord' tiene %d valor(es) de mas. Debe llevar "
                      "exactamente tres: client_id, client_secret e id_del_servidor, "
                      "separados por espacios y sin escribir para que es cada uno."
                      % DISCORD["sobrantes"])

    malos = [r for r in DISCORD["roles"] if not r.isdigit() or len(set(r)) <= 2]
    if malos:
        avisos.append("Estos roles parecen de ejemplo y NO EXISTEN: %s. Con la linea "
                      "'discord_roles' puesta se exige tener alguno, asi que no entraria "
                      "nadie. Borra la linea o pon los ID reales."
                      % ", ".join(m[:20] for m in malos))
    if DISCORD["dominio"]:
        avisos.append("Con 'dominio' puesto, el retorno de Discord ira a %s. Si ese "
                      "dominio todavia no apunta a este servidor, la entrada acabara en "
                      "la pagina del registrador. Para probar en tu ordenador, COMENTA o "
                      "borra la linea 'dominio' y usa http://127.0.0.1:8787"
                      % DISCORD["dominio"])
    if "dynastia.com" in DISCORD["dominio"]:
        avisos.append("El dominio de la academia es dynastia.ES, no .com. dynastia.com es "
                      "de otro y esta en venta: la entrada acabaria en su pagina.")
    if DISCORD["dominio"] and not DISCORD["dominio"].startswith("https://") \
            and "127.0.0.1" not in DISCORD["dominio"] and "localhost" not in DISCORD["dominio"]:
        avisos.append("El dominio deberia empezar por https:// para que la cookie de "
                      "sesion viaje protegida.")
    return avisos


# --------------------------------------------------------------------------
#  MARCADORES
#
#  Cada alumno guarda los titulares que quiera. Van atados a su cuenta de
#  Discord, asi que se conservan al cambiar de ordenador y nadie ve los de
#  otro. Sin sesion no hay marcadores: la vista lo dice en vez de fallar.
# --------------------------------------------------------------------------
MARC_ARCHIVO = os.path.join(RAIZ, "marcadores.json")
_LOCK_MARC = threading.Lock()
MARC_TOPE = 400


def marc_carga():
    if not os.path.exists(MARC_ARCHIVO):
        return {}
    try:
        with open(MARC_ARCHIVO, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                    # noqa: BLE001
        return {}


def marc_guarda(d):
    tmp = MARC_ARCHIVO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, MARC_ARCHIVO)


def marc_clave(t):
    return hashlib.sha256((t or "").strip().lower().encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
#  SOPORTE
#
#  Los alumnos reportan fallos desde el terminal y el aviso cae en el canal
#  Tickets de Discord. Se usa un WEBHOOK, no un bot: es una direccion que se
#  crea desde los ajustes del propio canal, no necesita permisos ni programa
#  aparte, y si se filtra lo unico que permite es escribir en ese canal.
#
#  En clave.txt:
#      tickets https://discord.com/api/webhooks/...
#      soporte_correo tu@correo.com        (opcional, solo informativo)
# --------------------------------------------------------------------------
SOPORTE = {"webhook": "", "correo": ""}
TICKETS_ARCHIVO = os.path.join(RAIZ, "tickets.json")
_LOCK_TK = threading.Lock()
_ULTIMO_TK = {}                  # id de alumno -> momento del ultimo envio


def tickets_guarda(t):
    with _LOCK_TK:
        try:
            d = json.load(open(TICKETS_ARCHIVO, encoding="utf-8")) if os.path.exists(TICKETS_ARCHIVO) else []
        except Exception:                                # noqa: BLE001
            d = []
        d.append(t)
        d = d[-500:]
        tmp = TICKETS_ARCHIVO + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, TICKETS_ARCHIVO)


TIPOS_TICKET = {"fallo": ("Fallo", 15548997), "dato": ("Dato incorrecto", 15844367),
                "idea": ("Sugerencia", 5793266), "otro": ("Otro", 10070709)}


def manda_ticket(t):
    """Publica en el canal Tickets. Devuelve (ok, motivo)."""
    if not SOPORTE["webhook"]:
        return False, "sin webhook configurado"
    etiqueta, color = TIPOS_TICKET.get(t.get("tipo"), TIPOS_TICKET["otro"])
    campos = [
        {"name": "Alumno", "value": t.get("usuario") or "sin identificar", "inline": True},
        {"name": "Tipo", "value": etiqueta, "inline": True},
        {"name": "Sección", "value": t.get("vista") or "—", "inline": True},
    ]
    if t.get("par"):
        campos.append({"name": "Par en pantalla", "value": t["par"], "inline": True})
    if t.get("contacto"):
        campos.append({"name": "Contacto", "value": t["contacto"][:120], "inline": True})
    tecnico = " · ".join(x for x in [t.get("navegador", "")[:110], t.get("pantalla", "")] if x)
    if tecnico:
        campos.append({"name": "Entorno", "value": tecnico, "inline": False})
    cuerpo = {"username": "Dynastia Analyst",
              "embeds": [{"title": "%s · %s" % (etiqueta, (t.get("titulo") or "Sin título")[:200]),
                          "description": (t.get("detalle") or "")[:3500],
                          "color": color, "fields": campos,
                          "footer": {"text": "Ticket %s" % t.get("id", "")},
                          "timestamp": datetime.datetime.utcnow().isoformat() + "Z"}]}
    try:
        traer(SOPORTE["webhook"], headers={"Content-Type": "application/json"},
              data=json.dumps(cuerpo).encode(), timeout=20)
        return True, ""
    except HTTPError as e:
        return False, "Discord respondio %s" % e.code
    except Exception as e:                               # noqa: BLE001
        return False, str(e)[:120]


def diagnostico_ia():
    """Comprueba la clave de verdad, con una llamada minima.

    Distingue los cuatro casos que se confunden entre si: no hay clave, la
    clave no vale, la clave vale pero no hay saldo, y no hay red. Sin esto el
    panel solo sabe decir «sin activar», que no explica nada.
    """
    with _LOCK_IA:
        prov, clave, modelo = IA["proveedor"], IA["clave"], IA["modelo"]
    if not clave:
        return {"ok": False, "estado": "sin_clave",
                "msg": "No hay ninguna clave cargada. Escribela en clave.txt, junto al "
                       "lanzador, con el formato: anthropic sk-ant-...",
                "pista": "Recuerda que clave.txt se lee AL ARRANCAR: si la acabas de "
                         "cambiar, cierra el servidor con Control+C y vuelve a lanzarlo."}

    cola = clave[-6:] if len(clave) > 10 else "?"
    modelo = modelo or MODELO_POR_DEFECTO.get(prov, "")
    if prov != "anthropic":
        return {"ok": True, "estado": "sin_comprobar",
                "msg": "Clave de %s cargada (termina en ...%s). La comprobacion "
                       "automatica solo esta hecha para Anthropic." % (prov, cola)}

    cuerpo = json.dumps({"model": modelo, "max_tokens": 1,
                         "messages": [{"role": "user", "content": "ok"}]}).encode()
    try:
        b, _ = traer("https://api.anthropic.com/v1/messages",
                     headers={"x-api-key": clave, "anthropic-version": "2023-06-01",
                              "Content-Type": "application/json"},
                     data=cuerpo, timeout=25)
        return {"ok": True, "estado": "correcta",
                "msg": "Clave correcta y con saldo. Modelo %s, clave ...%s." % (modelo, cola)}
    except HTTPError as e:
        try:
            det = json.loads(e.read().decode("utf-8", "ignore"))
            texto = ((det.get("error") or {}).get("message") or "")
        except Exception:                                # noqa: BLE001
            texto = ""
        bajo = texto.lower()
        if e.code == 401:
            return {"ok": False, "estado": "clave_invalida",
                    "msg": "La clave no es valida o esta revocada (clave ...%s)." % cola,
                    "pista": "Si revocaste la anterior, genera una nueva en "
                             "console.anthropic.com y pegala en clave.txt. Anadir saldo "
                             "no revive una clave revocada.",
                    "detalle": texto}
        if e.code == 403:
            return {"ok": False, "estado": "sin_permiso",
                    "msg": "La clave existe pero no tiene permiso sobre ese modelo o "
                           "espacio de trabajo.",
                    "pista": "Comprueba en la consola que la clave pertenece al espacio "
                             "de trabajo donde cargaste el saldo.",
                    "detalle": texto}
        if "credit" in bajo or "balance" in bajo or "quota" in bajo:
            return {"ok": False, "estado": "sin_saldo",
                    "msg": "La clave es valida pero la cuenta no tiene saldo disponible.",
                    "pista": "El saldo se carga POR ESPACIO DE TRABAJO. Si tienes varios, "
                             "comprueba que el dinero esta en el mismo al que pertenece "
                             "esta clave. Tambien puede tardar unos minutos en reflejarse.",
                    "detalle": texto}
        if e.code == 429:
            return {"ok": False, "estado": "limite",
                    "msg": "Demasiadas peticiones ahora mismo. Reintenta en un minuto.",
                    "detalle": texto}
        if "model" in bajo and ("not found" in bajo or "does not exist" in bajo):
            return {"ok": False, "estado": "modelo",
                    "msg": "El modelo %s no esta disponible para esta clave." % modelo,
                    "pista": "Cambia el modelo en clave.txt con una linea: modelo claude-sonnet-5",
                    "detalle": texto}
        return {"ok": False, "estado": "error_%d" % e.code,
                "msg": "La API respondio %d." % e.code, "detalle": texto}
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "estado": "sin_red",
                "msg": "No se pudo contactar con la API: %s" % str(e)[:120],
                "pista": "Comprueba la conexion o si un cortafuegos bloquea "
                         "api.anthropic.com."}


RE_PREAMBULO = re.compile(
    r"^(voy a|vamos a|dejame|dejame que|permiteme|ahora (voy|busco)|primero (voy|busco)|"
    r"te (voy a )?(hago|preparo|busco)|un momento|enseguida|"
    r"buscare|consultare|revisare|comprobare|contrastare|analizare)\b", re.I)


def _solo_preambulo(t):
    """True si lo unico que hay es el anuncio de lo que iba a hacer.

    Se retiran las frases que solo anuncian y se mira lo que queda. Asi
    «Voy a contrastar... El sesgo se sostiene mientras 1,1560 aguante...»
    cuenta como respuesta, y «Voy a contrastar los eventos citados.» a secas
    no. Sin esto el panel enseñaba el anuncio y nada mas.
    """
    t = (t or "").strip().replace("*", "")
    if not t:
        return True
    frases = re.split(r"(?<=[.:!?])(?=\s|[A-Z\u00c1\u00c9\u00cd\u00d3\u00da\u00d1])", t)
    resto = " ".join(f for f in frases if not RE_PREAMBULO.match(f.strip())).strip()
    if resto.endswith(":") and len(resto) < 60:
        return True                                      # solo un titulillo
    return len(resto) < 80


def llamar_ia(sistema, mensajes, maxtok=1100, buscar=False, modelo_pedido="", busquedas=6):
    with _LOCK_IA:
        prov, clave, modelo = IA["proveedor"], IA["clave"], IA["modelo"]
    if not clave:
        raise RuntimeError("No hay clave configurada. Añádela en Ajustes.")
    # El panel puede pedir un modelo mas capaz para las tareas criticas.
    modelo = modelo_pedido or modelo or MODELO_POR_DEFECTO.get(prov, "")

    if prov == "anthropic":
        def pide(con_busqueda, msgs):
            # El bloque de sistema es identico en todas las llamadas de la
            # misma tarea: marcarlo como cacheable recorta la mayor parte del
            # coste de entrada, que es la partida gorda cuando hay 92 alumnos.
            sis = [{"type": "text", "text": sistema}]
            if len(sistema) > 2000:
                sis[0]["cache_control"] = {"type": "ephemeral"}
            cuerpo = {"model": modelo, "max_tokens": maxtok, "system": sis, "messages": msgs}
            if con_busqueda:
                cuerpo["tools"] = [{"type": TIPO_BUSQUEDA, "name": "web_search",
                                    "max_uses": max(1, min(20, int(busquedas)))}]
            b, _ = traer("https://api.anthropic.com/v1/messages",
                         headers={"x-api-key": clave, "anthropic-version": "2023-06-01",
                                  "Content-Type": "application/json"},
                         data=json.dumps(cuerpo).encode(), timeout=240 if con_busqueda else 90)
            return json.loads(b)

        msgs = list(mensajes)
        texto, fuentes, motivo = "", [], ""
        con = buscar
        # Una respuesta con busqueda llega en varios tramos: el modelo anuncia
        # que va a buscar, busca, y el turno se PAUSA. Hay que continuarlo o el
        # panel se queda con el anuncio y sin la respuesta, que es justo lo que
        # pasaba. Tambien se continua cuando el turno se corta por longitud.
        for intento in range(8):
            try:
                d = pide(con, msgs)
            except HTTPError as e:
                if not con:
                    raise
                print("  ia        busqueda no disponible, se responde sin internet")
                sys.stdout.flush()
                con = False
                continue
            if d.get("error"):
                raise RuntimeError(d["error"].get("message", "error del proveedor"))
            t, f = _texto_y_fuentes(d)
            if texto and t and not texto.endswith((" ", "\n")):
                texto += " "                             # sin esto los tramos se pegan
            texto += t
            for x in f:
                if x["url"] not in [y["url"] for y in fuentes]:
                    fuentes.append(x)
            motivo = d.get("stop_reason") or ""

            # pause_turn: el servidor de Anthropic detuvo el turno tras usar la
            # herramienta. tool_use: queda una llamada pendiente. En ambos se
            # devuelve el turno tal cual y se pide continuacion.
            if motivo in ("pause_turn", "tool_use") and d.get("content"):
                msgs = msgs + [{"role": "assistant", "content": d["content"]}]
                continue

            # Cortado por longitud con la respuesta a medias: se continua.
            if motivo == "max_tokens" and d.get("content") and intento < 5:
                msgs = msgs + [{"role": "assistant", "content": d["content"]},
                               {"role": "user", "content":
                                "Continua exactamente donde lo dejaste, sin repetir lo ya escrito "
                                "y sin volver a presentarte."}]
                continue

            # A veces termina limpiamente pero solo ha escrito el anuncio de lo
            # que va a hacer. Para el usuario eso es una respuesta vacia.
            if _solo_preambulo(texto) and intento < 5:
                print("  ia        solo llego el preambulo, se pide la respuesta")
                sys.stdout.flush()
                msgs = msgs + [{"role": "assistant", "content": d.get("content") or texto},
                               {"role": "user", "content":
                                "Da ya la respuesta completa. No anuncies lo que vas a hacer: hazlo."}]
                con = False                              # sin busqueda, para que no se vuelva a ir
                continue
            break

        # Si tras todo el modelo no ha escrito nada, se reintenta sin busqueda:
        # es la causa habitual de agotar el presupuesto de salida.
        if not texto.strip() and buscar:
            print("  ia        respuesta sin texto (%s), reintento sin internet" % (motivo or "?"))
            sys.stdout.flush()
            d = pide(False, list(mensajes))
            if not d.get("error"):
                texto, _f = _texto_y_fuentes(d)
                motivo = d.get("stop_reason") or motivo

        return {"texto": texto, "fuentes": fuentes, "motivo": motivo,
                "busqueda": bool(fuentes) and buscar}

    # OpenAI: la busqueda vive en la interfaz de respuestas, distinta de la de
    # conversacion. Se usa esa cuando se pide internet.
    if buscar:
        entrada = [{"role": "system", "content": sistema}] + mensajes
        cuerpo = {"model": modelo, "input": entrada, "tools": [{"type": "web_search"}]}
        try:
            b, _ = traer("https://api.openai.com/v1/responses",
                         headers={"Authorization": "Bearer " + clave, "Content-Type": "application/json"},
                         data=json.dumps(cuerpo).encode(), timeout=180)
            d = json.loads(b)
            if d.get("error"):
                raise RuntimeError(d["error"].get("message", "error del proveedor"))
            partes, fuentes = [], []
            for it in d.get("output", []):
                for c in (it.get("content") or []):
                    if c.get("type") in ("output_text", "text"):
                        partes.append(c.get("text", ""))
                        for an in (c.get("annotations") or []):
                            u = an.get("url")
                            if u and u not in [f["url"] for f in fuentes]:
                                fuentes.append({"url": u, "titulo": an.get("title") or u})
            if partes:
                return {"texto": "".join(partes), "fuentes": fuentes, "busqueda": True}
        except Exception:                                # noqa: BLE001
            print("  ia        busqueda de OpenAI no disponible, se responde sin internet")
            sys.stdout.flush()

    cuerpo = {"model": modelo, "max_tokens": maxtok,
              "messages": [{"role": "system", "content": sistema}] + mensajes}
    b, _ = traer("https://api.openai.com/v1/chat/completions",
                 headers={"Authorization": "Bearer " + clave, "Content-Type": "application/json"},
                 data=json.dumps(cuerpo).encode(), timeout=90)
    d = json.loads(b)
    if d.get("error"):
        raise RuntimeError(d["error"].get("message", "error del proveedor"))
    return {"texto": d["choices"][0]["message"]["content"], "fuentes": [], "busqueda": False}

# Traducciones ya resueltas. Un titular se traduce una sola vez por sesion.
_TRAD = {}
_LOCK_TRAD = threading.Lock()


def traducir(texto, destino="es"):
    texto = (texto or "").strip()
    if not texto:
        return ""
    k = destino + "|" + texto
    with _LOCK_TRAD:
        if k in _TRAD:
            return _TRAD[k]
    try:
        url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl="
               + destino + "&dt=t&q=" + urllib.parse.quote(texto[:1200]))
        b, _ = traer(url, headers={"Accept": "application/json"}, timeout=12)
        datos = json.loads(b.decode("utf-8", "ignore"))
        out = "".join(t[0] for t in datos[0] if t and t[0])
        origen = datos[2] if len(datos) > 2 else ""
        # Si ya venia en el idioma destino no se devuelve traduccion.
        if origen == destino:
            out = ""
    except Exception:                                    # noqa: BLE001
        out = ""
    with _LOCK_TRAD:
        _TRAD[k] = out
        if len(_TRAD) > 4000:
            _TRAD.clear()
    return out

# --------------------------------------------------------------------------
#  Fuentes. El nivel marca cada cuanto se sondea:
#    1 -> cada 45 s   (agencias, reguladores y medios que publican rapido)
#    2 -> cada 3 min
#    3 -> cada 10 min (fondo, foros, busquedas tematicas)
# --------------------------------------------------------------------------
def gn(q, es=False):
    base = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
    return base + ("&hl=es&gl=ES&ceid=ES:es" if es else "&hl=en-US&gl=US&ceid=US:en")


FUENTES = [
    # nivel 1
    ("Cointelegraph", "https://cointelegraph.com/rss", None, 3, 1),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", None, 3, 1),
    ("The Block", "https://www.theblock.co/rss.xml", None, 3, 1),
    ("Blockworks", "https://blockworks.co/feed", None, 3, 1),
    ("Decrypt", "https://decrypt.co/feed", None, 2, 1),
    ("Reserva Federal", "https://www.federalreserve.gov/feeds/press_all.xml", "eco", 3, 1),
    ("Fed · política monetaria", "https://www.federalreserve.gov/feeds/press_monetary.xml", "eco", 3, 1),
    ("SEC · notas", "https://www.sec.gov/news/pressreleases.rss", "reg", 3, 1),
    ("SEC · litigios", "https://www.sec.gov/rss/litigation/litreleases.xml", "reg", 3, 1),
    ("CFTC", "https://www.cftc.gov/RSS/RSSGP/rssgp.xml", "reg", 3, 1),
    ("BCE", "https://www.ecb.europa.eu/rss/press.html", "eco", 3, 1),
    ("Tesoro EE. UU.", "https://home.treasury.gov/rss/press.xml", "eco", 2, 1),
    ("DL News", "https://www.dlnews.com/arc/outboundfeeds/rss/", None, 2, 1),
    ("Cointelegraph ES", "https://es.cointelegraph.com/rss", None, 2, 1),
    ("CriptoNoticias", "https://www.criptonoticias.com/feed/", None, 2, 1),
    # nivel 2
    ("CryptoSlate", "https://cryptoslate.com/feed/", None, 2, 2),
    ("BeInCrypto", "https://beincrypto.com/feed/", None, 2, 2),
    ("Bitcoin Magazine", "https://bitcoinmagazine.com/feed", None, 2, 2),
    ("Crypto Briefing", "https://cryptobriefing.com/feed/", None, 2, 2),
    ("The Defiant", "https://thedefiant.io/api/feed", None, 2, 2),
    ("Protos", "https://protos.com/feed/", None, 2, 2),
    ("Cryptonews", "https://cryptonews.com/news/feed/", None, 1, 2),
    ("Bitcoin.com", "https://news.bitcoin.com/feed/", None, 1, 2),
    ("CryptoPotato", "https://cryptopotato.com/feed/", None, 1, 2),
    ("NewsBTC", "https://www.newsbtc.com/feed/", None, 1, 2),
    ("Bitcoinist", "https://bitcoinist.com/feed/", None, 1, 2),
    ("AMBCrypto", "https://ambcrypto.com/feed/", None, 1, 2),
    ("U.Today", "https://u.today/rss", None, 1, 2),
    ("CoinGape", "https://coingape.com/feed/", None, 1, 2),
    ("CoinJournal", "https://coinjournal.net/feed/", None, 1, 2),
    ("BeInCrypto ES", "https://es.beincrypto.com/feed/", None, 1, 2),
    ("Observatorio Blockchain", "https://observatorioblockchain.com/feed/", None, 2, 2),
    ("Expansión Mercados", "https://e00-expansion.uecdn.es/rss/mercados.xml", "eco", 2, 2),
    ("El País Economía", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/economia/portada", "eco", 2, 2),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex", "eco", 1, 2),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "eco", 1, 2),
    ("Investing · cripto", "https://www.investing.com/rss/news_301.rss", None, 1, 2),
    ("Google News ES", gn("criptomonedas OR bitcoin OR ethereum", True), None, 1, 2),
    ("Flujos institucionales", gn("bitcoin ETF inflows outflows BlackRock Fidelity institutional treasury"), "ins", 2, 2),
    ("Ballenas", gn("whale bitcoin ethereum wallet transfer millions moved exchange"), "ins", 2, 2),
    ("Regulación global", gn("crypto regulation MiCA SEC lawsuit ban approval stablecoin law"), "reg", 2, 2),
    ("Seguridad", gn("crypto hack exploit stolen funds bridge drained protocol"), "sec", 2, 2),
    ("Macro y cripto", gn("Federal Reserve rate decision inflation bitcoin correlation"), "eco", 2, 2),
    # nivel 3
    ("FMI", "https://www.imf.org/en/News/RSS?language=eng", "eco", 2, 3),
    ("BIS", "https://www.bis.org/list/press_rlsepub/rss.xml", "eco", 2, 3),
    ("Ethereum Foundation", "https://blog.ethereum.org/en/feed.xml", "pro", 3, 3),
    ("Bitcoin Optech", "https://bitcoinops.org/feed.xml", "pro", 3, 3),
    ("a16z crypto", "https://a16zcrypto.com/feed/", "ins", 2, 3),
    ("CoinPedia", "https://coinpedia.org/feed/", None, 1, 3),
    ("ZyCrypto", "https://zycrypto.com/feed/", None, 1, 3),
    ("Coinspeaker", "https://www.coinspeaker.com/feed/", None, 1, 3),
    ("Finbold", "https://finbold.com/feed/", None, 1, 3),
    ("Bit2Me", "https://blog.bit2me.com/es/feed/", None, 1, 3),
    ("r/CryptoCurrency", "https://www.reddit.com/r/CryptoCurrency/hot/.rss?limit=25", "soc", 1, 3),
    ("r/Bitcoin", "https://www.reddit.com/r/Bitcoin/hot/.rss?limit=20", "soc", 1, 3),
    ("r/ethereum", "https://www.reddit.com/r/ethereum/hot/.rss?limit=20", "soc", 1, 3),
    ("r/CryptoMarkets", "https://www.reddit.com/r/CryptoMarkets/hot/.rss?limit=20", "soc", 1, 3),
    ("Hacker News", "https://hnrss.org/newest?q=bitcoin+OR+ethereum+OR+crypto&count=25", "soc", 1, 3),
    ("Lanzamientos", gn("crypto mainnet launch token generation event airdrop listing"), "pro", 1, 3),
    # Declaraciones de figuras cuyo mensaje mueve mercado. No son sus tuits:
    # son lo que las agencias recogen de lo que dicen, que es lo unico
    # accesible sin la interfaz de pago de X.
    ("Trump", gn("Trump says statement tariffs OR crypto OR Federal Reserve"), "dec", 3, 2),
    # Truth Social no publica canal de sindicacion ni interfaz publica: lo que
    # se recoge es lo que las agencias citan de sus publicaciones alli.
    ("Truth Social", gn("Trump Truth Social post wrote platform statement"), "dec", 2, 2),
    ("Reserva Federal · voces", gn("Powell OR Fed official says speech remarks rates"), "dec", 3, 2),
    ("Tesoro y regulación", gn("Treasury Secretary OR SEC chair says crypto statement"), "dec", 2, 2),
    ("Musk", gn("Elon Musk says bitcoin OR dogecoin OR tesla"), "dec", 1, 3),
    ("Saylor y tesorerías", gn("Michael Saylor OR Strategy bitcoin statement purchase"), "dec", 2, 3),
    ("Voces del sector", gn("Binance CEO OR Coinbase CEO OR BlackRock executive says crypto"), "dec", 2, 3),
]

# --------------------------------------------------------------------------
#  FOREX: fuentes propias del mercado de divisas y metales.
#  Las agencias mandan; lo demas complementa.
# --------------------------------------------------------------------------
FUENTES_FX = [
    # nivel 1 · agencias y bancos centrales
    ("Reuters Mercados", gn("Reuters forex dollar euro currency markets"), "fx", 3, 1),
    ("Bloomberg Divisas", gn("Bloomberg currency dollar euro yen markets"), "fx", 3, 1),
    ("Wall Street Journal", gn("Wall Street Journal dollar currency Fed markets"), "fx", 3, 1),
    ("Reserva Federal", "https://www.federalreserve.gov/feeds/press_all.xml", "cb", 3, 1),
    ("Fed · política monetaria", "https://www.federalreserve.gov/feeds/press_monetary.xml", "cb", 3, 1),
    ("BCE", "https://www.ecb.europa.eu/rss/press.html", "cb", 3, 1),
    ("Tesoro EE. UU.", "https://home.treasury.gov/rss/press.xml", "cb", 3, 1),
    ("ForexFactory · noticias", gn("ForexFactory forex news calendar release"), "fx", 2, 1),
    ("FXStreet", gn("FXStreet EURUSD analysis forecast"), "fx", 2, 1),
    ("Investing · Forex", "https://www.investing.com/rss/news_1.rss", "fx", 2, 1),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "fx", 2, 1),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex", "fx", 1, 1),
    ("Expansión Mercados", "https://e00-expansion.uecdn.es/rss/mercados.xml", "fx", 2, 1),
    ("El País Economía", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/economia/portada", "fx", 2, 1),
    # nivel 2 · pares y bloques
    ("EUR/USD", gn("EURUSD euro dollar forecast analysis"), "par", 2, 2),
    ("GBP/USD", gn("GBPUSD pound sterling dollar forecast"), "par", 2, 2),
    ("USD/JPY", gn("USDJPY yen dollar Bank of Japan intervention"), "par", 2, 2),
    ("Dólar", gn("dollar index DXY strength weakness outlook"), "fx", 3, 2),
    ("Oro", gn("gold price XAUUSD forecast safe haven"), "met", 3, 2),
    ("Plata y metales", gn("silver platinum palladium price forecast"), "met", 2, 2),
    ("Petróleo", gn("oil price WTI Brent OPEC forecast"), "mat", 2, 2),
    ("Banco de Japón", gn("Bank of Japan BOJ policy yen rates"), "cb", 3, 2),
    ("Banco de Inglaterra", gn("Bank of England BoE rates pound policy"), "cb", 3, 2),
    ("Banco de Canadá y Australia", gn("Bank of Canada Reserve Bank Australia rates"), "cb", 2, 2),
    ("Banco Nacional Suizo", gn("Swiss National Bank franc intervention rates"), "cb", 2, 2),
    ("Banco Central Europeo · voces", gn("ECB Lagarde official speech rates euro"), "cb", 3, 2),
    ("Fed · voces", gn("Fed official speech Powell remarks rates outlook"), "cb", 3, 2),
    ("Inflación EE. UU.", gn("US CPI inflation report data release"), "dato", 3, 2),
    ("Empleo EE. UU.", gn("US nonfarm payrolls jobs report unemployment"), "dato", 3, 2),
    ("PIB y crecimiento", gn("GDP growth data eurozone United States"), "dato", 2, 2),
    ("Aranceles y comercio", gn("tariffs trade war deal negotiations impact currency"), "geo", 3, 2),
    ("Geopolítica", gn("geopolitical risk conflict markets safe haven"), "geo", 2, 2),
    ("Bonos y tipos", gn("treasury yields bond market rates curve"), "fx", 2, 2),
    ("China y yuan", gn("China yuan PBOC economy data"), "fx", 2, 2),
    ("Emergentes", gn("emerging markets currency peso lira real"), "fx", 1, 3),
    ("Analistas técnicos", gn("EURUSD technical analysis support resistance levels"), "tec", 2, 2),
    ("Oro técnico", gn("gold technical analysis support resistance level"), "tec", 2, 3),
    ("DailyFX", gn("DailyFX analysis forex outlook"), "fx", 1, 3),
    ("Action Forex", gn("Action Forex daily outlook currencies"), "fx", 1, 3),
    ("Kitco metales", gn("Kitco gold silver market news"), "met", 1, 3),
    ("Commodities", gn("commodities market copper natural gas prices"), "mat", 1, 3),
    ("FMI", "https://www.imf.org/en/News/RSS?language=eng", "cb", 2, 3),
    ("BIS", "https://www.bis.org/list/press_rlsepub/rss.xml", "cb", 2, 3),
    ("Banco de España", gn("Banco de España informe economía española"), "cb", 2, 3),
    ("Eurostat y datos UE", gn("eurozone inflation PMI retail sales data"), "dato", 2, 3),
    ("Empleo global", gn("employment data jobless claims Europe Japan"), "dato", 1, 3),
    ("Riesgo soberano", gn("sovereign debt rating downgrade spread"), "geo", 2, 3),
    ("Energía y divisas", gn("energy prices impact currencies inflation"), "mat", 1, 3),
    ("Sentimiento de mercado", gn("risk appetite market sentiment volatility VIX"), "fx", 2, 3),
    ("Posicionamiento COT", gn("CFTC commitment of traders positioning currency"), "fx", 2, 3),
    ("Intervención cambiaria", gn("currency intervention central bank verbal warning"), "cb", 3, 3),
    ("Cripto y dólar", gn("bitcoin dollar correlation risk assets"), "fx", 1, 3),
]

# Seguimiento especifico del presidente de Estados Unidos: agenda, actos y
# declaraciones. En Forex su calendario mueve el dolar tanto como un dato.
FUENTES_TRUMP = [
    ("Agenda presidencial", gn("Trump schedule today meeting agenda White House"), "agenda", 3, 1),
    ("Declaraciones", gn("Trump says statement remarks today"), "decl", 3, 1),
    ("Aranceles", gn("Trump tariffs announcement trade order"), "arancel", 3, 1),
    ("Fed y Trump", gn("Trump Federal Reserve Powell pressure rates"), "fed", 3, 1),
    ("Truth Social", gn("Trump Truth Social post wrote"), "decl", 2, 2),
    ("Órdenes ejecutivas", gn("Trump executive order signed White House"), "orden", 3, 2),
    ("Política exterior", gn("Trump foreign policy summit meeting leader"), "exterior", 2, 2),
    ("Reacción de mercados", gn("markets react Trump announcement dollar"), "mercado", 2, 2),
]

FUENTES_LISTADOS = [
    ("Binance", "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
                "?type=1&catalogId=48&pageNo=1&pageSize=20", "json"),
    ("Binance futuros", "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
                        "?type=1&catalogId=161&pageNo=1&pageSize=15", "json"),
    (None, gn("Binance will list new cryptocurrency listing announcement"), "rss"),
    (None, gn("Coinbase listing roadmap adds asset support"), "rss"),
    (None, gn("Upbit new listing KRW market"), "rss"),
    (None, gn("Kraken lists new token trading launch"), "rss"),
    (None, gn("OKX Bybit Bitget new listing spot trading"), "rss"),
]

# --------------------------------------------------------------------------
ESTADO = {"noticias": [], "listados": [], "calendario": [], "tesorerias": [],
          "ballenas": [], "fx": [], "trump": [], "cotiz": {}, "macro": {}, "diag": {}, "arranque": time.time()}
TRASPASO = {"datos": None, "ts": 0}
_lock = threading.Lock()
_condicional = {}          # url -> (etag, last_modified)
SUSCRIPTORES = []
_lock_subs = threading.Lock()
CLAVES = {}
_lock_claves = threading.Lock()
PARAR = threading.Event()


def permitido(url):
    try:
        h = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(h == d or h.endswith("." + d) for d in PERMITIDOS)


def traer(url, headers=None, data=None, timeout=20, condicional=False):
    """Descarga. Con condicional=True usa ETag/Last-Modified: si la fuente no
    ha cambiado devuelve (None, 304) sin transferir el cuerpo."""
    h = {"User-Agent": UA, "Accept-Encoding": "gzip",
         "Accept": "application/rss+xml, application/xml, text/xml, application/json, text/html, */*",
         "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    if condicional:
        prev = _condicional.get(url)
        if prev:
            if prev[0]:
                h["If-None-Match"] = prev[0]
            if prev[1]:
                h["If-Modified-Since"] = prev[1]
    req = Request(url, headers=h, data=data)
    try:
        with urlopen(req, timeout=timeout) as r:
            b = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                try:
                    b = gzip.GzipFile(fileobj=io.BytesIO(b)).read()
                except OSError:
                    pass
            if condicional:
                _condicional[url] = (r.headers.get("ETag"), r.headers.get("Last-Modified"))
            return b, r.status
    except HTTPError as e:
        if e.code == 304:
            return None, 304
        raise


# --------------------------------------------------------------------------
#  Difusion por Server-Sent Events
# --------------------------------------------------------------------------
def publicar(evento, datos):
    msg = "event: %s\ndata: %s\n\n" % (evento, json.dumps(datos, ensure_ascii=False))
    with _lock_subs:
        muertos = []
        for q in SUSCRIPTORES:
            try:
                q.put_nowait(msg)
            except queue.Full:
                muertos.append(q)
        for q in muertos:
            SUSCRIPTORES.remove(q)


# --------------------------------------------------------------------------
#  Noticias
# --------------------------------------------------------------------------
def _txt(nodo_txt):
    return (nodo_txt or "").replace("&amp;", "&").replace("&quot;", '"') \
        .replace("&#39;", "'").replace("&apos;", "'").replace("&lt;", "<").replace("&gt;", ">")


RE_ITEM = re.compile(r"<(?:item|entry)[ >].*?</(?:item|entry)>", re.S)
RE_TIT = re.compile(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
RE_ENL = re.compile(r'<link[^>]*href="([^"]+)"|<link[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>', re.S)
RE_FEC = re.compile(r"<(?:pubDate|published|updated)>(.*?)</(?:pubDate|published|updated)>", re.S)

MESES = {"jan": 0, "feb": 1, "mar": 2, "apr": 3, "may": 4, "jun": 5,
         "jul": 6, "aug": 7, "sep": 8, "oct": 9, "nov": 10, "dec": 11}


def fecha_ms(s):
    s = (s or "").strip()
    if not s:
        return int(time.time() * 1000)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", s)
    if m:
        try:
            import calendar
            t = calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))
            off = re.search(r"([+-])(\d{2}):?(\d{2})$", s)
            if off:
                d = (int(off.group(2)) * 3600 + int(off.group(3)) * 60) * (1 if off.group(1) == "+" else -1)
                t -= d
            return t * 1000
        except Exception:                                # noqa: BLE001
            pass
    m = re.search(r"(\d{1,2}) (\w{3})\w* (\d{4}) (\d{2}):(\d{2}):(\d{2})", s)
    if m and m.group(2).lower() in MESES:
        try:
            import calendar
            t = calendar.timegm((int(m.group(3)), MESES[m.group(2).lower()] + 1, int(m.group(1)),
                                 int(m.group(4)), int(m.group(5)), int(m.group(6)), 0, 0, 0))
            return t * 1000
        except Exception:                                # noqa: BLE001
            pass
    return int(time.time() * 1000)


def parsear_feed(xml, nombre, cat, peso):
    out = []
    for bloque in RE_ITEM.findall(xml)[:25]:
        mt = RE_TIT.search(bloque)
        if not mt:
            continue
        titulo = re.sub(r"\s+", " ", _txt(mt.group(1))).strip()
        if len(titulo) < 12:
            continue
        enlace = ""
        me = RE_ENL.search(bloque)
        if me:
            enlace = (me.group(1) or me.group(2) or "").strip()
        mf = RE_FEC.search(bloque)
        out.append({"title": titulo, "link": enlace, "ts": fecha_ms(mf.group(1) if mf else ""),
                    "src": nombre, "c": cat, "w": peso})
    return out


def clave(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower())[:55]


def integrar_noticias(nuevas):
    """Devuelve solo lo que no habiamos visto nunca."""
    frescas = []
    with _lock:
        vistas = {clave(n["title"]) for n in ESTADO["noticias"]}
        for n in nuevas:
            k = clave(n["title"])
            if k in vistas:
                continue
            vistas.add(k)
            frescas.append(n)
        if frescas:
            ESTADO["noticias"] = (frescas + ESTADO["noticias"])[:700]
            ESTADO["noticias"].sort(key=lambda x: -x["ts"])
    return frescas


def sondear_nivel(nivel, primera):
    ok = fallos = sin_cambio = 0
    lote = []
    for nombre, url, cat, peso, niv in FUENTES:
        if niv != nivel or PARAR.is_set():
            continue
        try:
            b, code = traer(url, timeout=18, condicional=True)
            if code == 304:
                sin_cambio += 1
                ok += 1
                continue
            lote.extend(parsear_feed(b.decode("utf-8", "ignore"), nombre, cat, peso))
            ok += 1
        except Exception:                                # noqa: BLE001
            fallos += 1
        time.sleep(0.15)                                 # cortesia con las fuentes
    frescas = integrar_noticias(lote)
    with _lock:
        ESTADO["diag"]["noticias"] = ("%d fuentes activas · %d sin cambios · %d con error"
                                      % (ok, sin_cambio, fallos))
    if frescas and not primera:
        # Lo mas reciente primero: es lo que el panel va a mostrar arriba.
        frescas.sort(key=lambda x: -x["ts"])
        publicar("noticias", frescas)
        print("  noticias  %d nuevas (nivel %d)" % (len(frescas), nivel))
        sys.stdout.flush()
    return len(frescas)


# Cada fuente tiene su propio reloj. El bucle consulta la que toque, una tras
# otra sin pausa apreciable, en vez de barrer las 59 de golpe cada 45 s. Con
# ~59 fuentes y estos intervalos se consulta algo practicamente cada segundo,
# y en cuanto aparece un titular sale disparado hacia el panel.
INTERVALO = {1: 20, 2: 90, 3: 300}


def bucle_generico(fuentes, almacen, evento, etiqueta):
    """Mismo motor de sondeo para cualquier conjunto de fuentes."""
    prox = {f[1]: 0.0 for f in fuentes}
    vueltas = {f[1]: 0 for f in fuentes}
    primera = True
    ok = fallos = sin_cambio = 0
    while not PARAR.is_set():
        ahora = time.time()
        pend = [f for f in fuentes if prox[f[1]] <= ahora]
        if not pend:
            PARAR.wait(0.5)
            if primera and all(v > 0 for v in vueltas.values()):
                with _lock:
                    inicial = ESTADO[almacen][:400]
                publicar(evento, inicial)
                print("  %-9s %d titulares tras la primera vuelta" % (etiqueta, len(inicial)))
                sys.stdout.flush()
                primera = False
            continue
        pend.sort(key=lambda f: (f[4], prox[f[1]]))
        nombre, url, cat, peso, niv = pend[0]
        try:
            b, code = traer(url, timeout=15, condicional=True)
            if code == 304:
                sin_cambio += 1
            elif b:
                lote = parsear_feed(b.decode("utf-8", "ignore"), nombre, cat, peso)
                frescas = []
                with _lock:
                    vistas = {clave(n["title"]) for n in ESTADO[almacen]}
                    for n in lote:
                        k = clave(n["title"])
                        if k in vistas:
                            continue
                        vistas.add(k)
                        frescas.append(n)
                    if frescas:
                        ESTADO[almacen] = (frescas + ESTADO[almacen])[:700]
                        ESTADO[almacen].sort(key=lambda x: -x["ts"])
                if frescas and not primera:
                    frescas.sort(key=lambda x: -x["ts"])
                    publicar(evento, frescas)
                    print("  %-9s %d de %s" % (etiqueta, len(frescas), nombre))
                    sys.stdout.flush()
            ok += 1
        except Exception:                                # noqa: BLE001
            fallos += 1
        prox[url] = time.time() + INTERVALO.get(niv, 120)
        vueltas[url] += 1
        with _lock:
            ESTADO["diag"][almacen] = ("%d consultas · %d sin cambios · %d con error · %d titulares"
                                       % (ok, sin_cambio, fallos, len(ESTADO[almacen])))
        PARAR.wait(0.3)


def bucle_fx():
    bucle_generico(FUENTES_FX, "fx", "fx", "forex")


def bucle_trump():
    bucle_generico(FUENTES_TRUMP, "trump", "trump", "trump")


def bucle_noticias():
    prox = {f[1]: 0.0 for f in FUENTES}
    vueltas = {f[1]: 0 for f in FUENTES}
    primera = True
    ok = fallos = sin_cambio = 0
    while not PARAR.is_set():
        ahora = time.time()
        pend = [f for f in FUENTES if prox[f[1]] <= ahora]
        if not pend:
            PARAR.wait(0.4)
            # Cuando ya se ha dado una vuelta completa se envia el estado inicial.
            if primera and all(v > 0 for v in vueltas.values()):
                with _lock:
                    inicial = ESTADO["noticias"][:400]
                publicar("noticias", inicial)
                print("  noticias  %d titulares tras la primera vuelta" % len(inicial))
                sys.stdout.flush()
                primera = False
            continue
        pend.sort(key=lambda f: (f[4], prox[f[1]]))
        nombre, url, cat, peso, niv = pend[0]
        try:
            b, code = traer(url, timeout=15, condicional=True)
            if code == 304:
                sin_cambio += 1
            elif b:
                frescas = integrar_noticias(parsear_feed(b.decode("utf-8", "ignore"), nombre, cat, peso))
                if frescas and not primera:
                    frescas.sort(key=lambda x: -x["ts"])
                    publicar("noticias", frescas)
                    print("  noticias  %d de %s" % (len(frescas), nombre))
                    sys.stdout.flush()
            ok += 1
        except Exception:                                # noqa: BLE001
            fallos += 1
        prox[url] = time.time() + INTERVALO.get(niv, 120)
        vueltas[url] += 1
        with _lock:
            ESTADO["diag"]["noticias"] = ("%d consultas · %d sin cambios · %d con error · %d titulares"
                                          % (ok, sin_cambio, fallos, len(ESTADO["noticias"])))
        PARAR.wait(0.3)


# --------------------------------------------------------------------------
#  Listados: cada 60 segundos
# --------------------------------------------------------------------------
def bucle_listados():
    primera = True
    while not PARAR.is_set():
        lote = []
        for exch, url, tipo in FUENTES_LISTADOS:
            if PARAR.is_set():
                break
            try:
                b, code = traer(url, timeout=18, condicional=True)
                if code == 304:
                    continue
                txt = b.decode("utf-8", "ignore")
                if tipo == "json":
                    j = json.loads(txt)
                    arts = []
                    for c in ((j.get("data") or {}).get("catalogs") or []):
                        arts.extend(c.get("articles") or [])
                    for a in arts:
                        lote.append({"title": a.get("title", ""), "ex": exch,
                                     "ts": a.get("releaseDate") or int(time.time() * 1000),
                                     "link": "https://www.binance.com/en/support/announcement/"
                                             + str(a.get("code", ""))})
                else:
                    for n in parsear_feed(txt, exch or "", None, 1):
                        lote.append({"title": n["title"], "ex": exch,
                                     "ts": n["ts"], "link": n["link"]})
            except Exception:                            # noqa: BLE001
                pass
            time.sleep(0.15)
        nuevos = []
        with _lock:
            vistos = {clave(x["title"]) for x in ESTADO["listados"]}
            for l in lote:
                k = clave(l["title"])
                if not l["title"] or k in vistos:
                    continue
                vistos.add(k)
                nuevos.append(l)
            if nuevos:
                ESTADO["listados"] = (nuevos + ESTADO["listados"])[:200]
            ESTADO["diag"]["listados"] = "%d anuncios" % len(ESTADO["listados"])
        if nuevos:
            publicar("listados", nuevos if not primera else ESTADO["listados"])
            if not primera:
                print("  listados  %d anuncios nuevos" % len(nuevos))
                sys.stdout.flush()
        primera = False
        PARAR.wait(60)


# --------------------------------------------------------------------------
#  Calendario economico: ForexFactory + Trading Economics (EE. UU.)
# --------------------------------------------------------------------------
PAIS_DIV = {
    "US": "USD", "EU": "EUR", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
    "NL": "EUR", "GB": "GBP", "UK": "GBP", "JP": "JPY", "CH": "CHF", "CA": "CAD",
    "AU": "AUD", "NZ": "NZD", "CN": "CNY", "MX": "MXN", "BR": "BRL", "IN": "INR",
    "ZA": "ZAR", "TR": "TRY", "SE": "SEK", "NO": "NOK", "RU": "RUB", "KR": "KRW",
}


def calendario_tradingview():
    """Calendario economico publico de TradingView.

    Se usa como fuente principal porque es el mismo origen que ya responde
    para las cotizaciones: si el terminal ve precios, ve calendario. El canal
    de ForexFactory queda de respaldo, que es donde estaba el corte.
    """
    hoy = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    lunes = hoy - datetime.timedelta(days=hoy.weekday())
    desde = (lunes - datetime.timedelta(days=7)).strftime("%Y-%m-%dT00:00:00.000Z")
    hasta = (lunes + datetime.timedelta(days=14)).strftime("%Y-%m-%dT00:00:00.000Z")
    paises = "US,EU,DE,FR,IT,ES,GB,JP,CH,CA,AU,NZ,CN"
    url = ("https://economic-calendar.tradingview.com/events"
           "?from=%s&to=%s&countries=%s" % (desde, hasta, paises))
    b, _ = traer(url, headers={"Accept": "application/json",
                               "Origin": "https://www.tradingview.com",
                               "Referer": "https://www.tradingview.com/"}, timeout=25)
    j = json.loads(b.decode("utf-8", "ignore"))
    filas = j.get("result") if isinstance(j, dict) else j
    out = []
    for e in (filas or []):
        titulo = (e.get("title") or e.get("indicator") or "").strip()
        if not titulo:
            continue
        # importance: 1 alto, 0 medio, -1 bajo.
        try:
            imp = int(e.get("importance", 0))
        except Exception:                                # noqa: BLE001
            imp = 0
        pais = (e.get("country") or "").upper()
        periodo = (e.get("period") or "").strip()
        out.append({"ts": fecha_ms(e.get("date") or ""),
                    "pais": PAIS_DIV.get(pais, pais),
                    "ev": titulo + ((" (" + periodo + ")") if periodo else ""),
                    "imp": 3 if imp >= 1 else 2 if imp == 0 else 1,
                    "prev": _num_cal(e.get("previous")),
                    "fc": _num_cal(e.get("forecast")),
                    "act": _num_cal(e.get("actual")),
                    "fuente": "TradingView"})
    return out


def _num_cal(v):
    if v is None or v == "":
        return ""
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".")
    return str(v)


def _imp(s):
    s = (s or "").lower()
    return 3 if "high" in s else 2 if "medium" in s else 1 if "low" in s else 0


def calendario_trading_economics():
    """Clave de invitado publica. Devuelve el calendario de Estados Unidos."""
    out = []
    # Horizonte de tres semanas, el mismo que publica ForexFactory. No se pide
    # rango largo: anade latencia y la clave de invitado lo rechaza a menudo.
    for url in ("https://api.tradingeconomics.com/calendar/country/united%20states?c=guest:guest&f=json",
                "https://api.tradingeconomics.com/calendar?c=guest:guest&f=json"):
        try:
            b, _ = traer(url, timeout=20)
            datos = json.loads(b.decode("utf-8", "ignore"))
            if not isinstance(datos, list):
                continue
            for e in datos:
                pais = (e.get("Country") or "").strip()
                if pais and pais.lower() not in ("united states", "estados unidos"):
                    continue
                fecha = e.get("Date") or ""
                ts = fecha_ms(fecha)
                imp = e.get("Importance")
                out.append({"ts": ts, "pais": "USD",
                            "ev": (e.get("Event") or "").strip(),
                            "imp": 3 if str(imp) == "3" else 2 if str(imp) == "2" else 1,
                            "prev": str(e.get("Previous") or ""),
                            "fc": str(e.get("Forecast") or e.get("TEForecast") or ""),
                            "act": str(e.get("Actual") or ""),
                            "ref": (e.get("Reference") or ""), "fuente": "Trading Economics"})
            if out:
                break
        except Exception:                                # noqa: BLE001
            continue
    return out


_CAL_CACHE = {}


def bucle_calendario():
    primera_cal = [True]
    while not PARAR.is_set():
        eventos, origenes = [], []
        try:
            tv = calendario_tradingview()
            eventos.extend(tv)
            origenes.append("TradingView %d" % len(tv))
        except Exception as e:                           # noqa: BLE001
            origenes.append("TradingView falla (%s)" % str(e)[:40])
        try:
            n0 = len(eventos)
            eventos.extend(calendario_trading_economics())
            if len(eventos) > n0:
                origenes.append("Trading Economics %d" % (len(eventos) - n0))
        except Exception:                                # noqa: BLE001
            pass
        te = len(eventos)
        # Tres semanas: la pasada, la actual y la siguiente. Es todo lo que
        # publica el canal abierto de ForexFactory.
        #
        # Aqui estaba el fallo del calendario vacio: se pide con cabecera
        # condicional, asi que a partir de la segunda vuelta el servidor
        # responde 304 «sin cambios» y no manda cuerpo. Antes se saltaba esa
        # semana y la tabla se reescribia vacia a los siete minutos. Ahora se
        # guarda lo ya descargado y en el 304 se reutiliza.
        fallos = []
        for url in ("https://nfs.faireconomy.media/ff_calendar_lastweek.json",
                    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                    "https://nfs.faireconomy.media/ff_calendar_nextweek.json"):
            try:
                b, code = traer(url, timeout=18)
                if code == 304 or not b:
                    eventos.extend(_CAL_CACHE.get(url, []))
                    continue
                semana = []
                for e in json.loads(b.decode("utf-8", "ignore")):
                    semana.append({"ts": fecha_ms(e.get("date", "")),
                                   "pais": (e.get("country") or "").upper(),
                                   "ev": e.get("title") or "", "imp": _imp(e.get("impact")),
                                   "prev": e.get("previous") or "", "fc": e.get("forecast") or "",
                                   "act": e.get("actual") or "", "fuente": "ForexFactory"})
                if semana:
                    _CAL_CACHE[url] = semana
                eventos.extend(_CAL_CACHE.get(url, []))
            except Exception as ex:                      # noqa: BLE001
                fallos.append("%s: %s" % (url.rsplit("_", 1)[-1][:-5], str(ex)[:40])) 
                eventos.extend(_CAL_CACHE.get(url, []))
        vistos, limpio = set(), []
        for e in sorted(eventos, key=lambda x: x["ts"]):
            if not e["ev"]:
                continue
            k = "%d|%s|%s" % (e["ts"] // 60000, e["pais"], e["ev"].lower()[:28])
            if k in vistos:
                continue
            vistos.add(k)
            limpio.append(e)
        # Nunca se pisa una tabla buena con una vuelta fallida.
        with _lock:
            previos = len(ESTADO.get("calendario") or [])
            if limpio or not previos:
                ESTADO["calendario"] = limpio
            alto = sum(1 for e in limpio if e["imp"] >= 3 and e["ts"] > time.time() * 1000)
            porf = {}
            for e in limpio:
                porf[e.get("fuente", "?")] = porf.get(e.get("fuente", "?"), 0) + 1
            ESTADO["diag"]["calendario"] = (
                "%d eventos · %d de alto impacto por delante · %s%s"
                % (len(limpio), alto,
                   ", ".join("%s %d" % (k, v) for k, v in sorted(porf.items())) or "sin fuentes",
                   (" · " + "; ".join(fallos)) if fallos else ""))
        if limpio:
            publicar("calendario", limpio)
            if primera_cal[0]:
                print("  calendario %d eventos · %d de alto impacto proximos · %s"
                      % (len(limpio), alto, " + ".join(origenes)))
                sys.stdout.flush()
                primera_cal[0] = False
        else:
            print("  calendario SIN DATOS · %s%s"
                  % (" + ".join(origenes), (" · " + "; ".join(fallos)) if fallos else ""))
            sys.stdout.flush()
        PARAR.wait(420)



# --------------------------------------------------------------------------
#  COTIZACIONES DE DIVISAS Y METALES
#
#  Se hacen aqui y no en el navegador por tres razones: se ve en la traza
#  que fuente responde y cual no, se puede reintentar sin castigar al panel,
#  y se evita que veintiseis peticiones simultaneas acaben limitadas.
# --------------------------------------------------------------------------
PARES_FX = [
    ("EUR/USD", "FX:EURUSD", "EURUSD=X", 5), ("GBP/USD", "FX:GBPUSD", "GBPUSD=X", 5),
    ("USD/JPY", "FX:USDJPY", "JPY=X", 3), ("USD/CHF", "FX:USDCHF", "CHF=X", 5),
    ("AUD/USD", "FX:AUDUSD", "AUDUSD=X", 5), ("USD/CAD", "FX:USDCAD", "CAD=X", 5),
    ("NZD/USD", "FX:NZDUSD", "NZDUSD=X", 5), ("EUR/GBP", "FX:EURGBP", "EURGBP=X", 5),
    ("EUR/JPY", "FX:EURJPY", "EURJPY=X", 3), ("GBP/JPY", "FX:GBPJPY", "GBPJPY=X", 3),
    ("EUR/CHF", "FX:EURCHF", "EURCHF=X", 5), ("AUD/JPY", "FX:AUDJPY", "AUDJPY=X", 3),
    ("CHF/JPY", "FX:CHFJPY", "CHFJPY=X", 3), ("EUR/AUD", "FX:EURAUD", "EURAUD=X", 5),
    ("GBP/CHF", "FX:GBPCHF", "GBPCHF=X", 5), ("XAU/USD", "OANDA:XAUUSD", "GC=F", 2),
    ("XAG/USD", "OANDA:XAGUSD", "SI=F", 3), ("XPT/USD", "OANDA:XPTUSD", "PL=F", 2),
    ("XCU/USD", "OANDA:XCUUSD", "HG=F", 4), ("DXY", "TVC:DXY", "DX-Y.NYB", 3),
    ("USD/MXN", "FX:USDMXN", "MXN=X", 4), ("USD/TRY", "FX:USDTRY", "TRY=X", 4),
    ("USD/ZAR", "FX:USDZAR", "ZAR=X", 4), ("USD/SEK", "FX:USDSEK", "SEK=X", 4),
    ("USD/NOK", "FX:USDNOK", "NOK=X", 4), ("USD/CNH", "FX_IDC:USDCNH", "CNH=X", 4),
]


def fx_tradingview():
    """Cotizaciones del explorador publico de TradingView.

    Cada mercado tiene su propio indice: /forex/scan solo conoce los simbolos
    del mercado de divisas, asi que los metales de OANDA y el indice dolar de
    TVC devolvian nada. Se consultan los dos mercados y se fusiona.
    """
    inv = {p[1]: p[0] for p in PARES_FX}
    pendientes = set(inv)
    out, errores = {}, []

    def escanear(mercado, tickers):
        cuerpo = {"symbols": {"tickers": sorted(tickers), "query": {"types": []}},
                  "columns": ["close", "change", "change_abs", "open", "high", "low"]}
        b, _ = traer("https://scanner.tradingview.com/%s/scan" % mercado,
                     headers={"Content-Type": "application/json",
                              "Accept": "application/json",
                              "Origin": "https://www.tradingview.com",
                              "Referer": "https://www.tradingview.com/"},
                     data=json.dumps(cuerpo).encode(), timeout=20)
        j = json.loads(b.decode("utf-8", "ignore"))
        for f in (j.get("data") or []):
            sim = f.get("s")
            par = inv.get(sim)
            d = f.get("d") or []
            if not par or len(d) < 6 or d[0] is None:
                continue
            cierre, chp, chabs, ap, hi, lo = d[0], d[1], d[2], d[3], d[4], d[5]
            out[par] = {"p": cierre,
                        "prev": (cierre - chabs) if chabs is not None else ap,
                        "ch": chabs, "chp": chp, "hi": hi, "lo": lo,
                        "fuente": "tradingview", "t": int(time.time() * 1000)}
            pendientes.discard(sim)

    # Orden de intento. El primero cubre los pares; el segundo, metales, DXY
    # y contratos por diferencia; los dos ultimos son red de seguridad por si
    # TradingView reclasifica algun simbolo.
    for mercado in ("forex", "cfd", "america", "global"):
        if not pendientes:
            break
        try:
            escanear(mercado, pendientes)
        except Exception as e:                           # noqa: BLE001
            errores.append("%s: %s" % (mercado, e))

    # Los metales y el indice dolar no viven en el indice de divisas y a veces
    # tampoco en el de contratos por diferencia. Para esos se pregunta uno a
    # uno por la ficha del simbolo, que responde para cualquier mercado, y se
    # prueban los nombres alternativos de cada instrumento.
    for sim in sorted(pendientes):
        par = inv.get(sim)
        if not par:
            continue
        for cand in [sim] + ALTERNOS_TV.get(par, []):
            try:
                d = fx_tv_ficha(cand)
            except Exception as e:                       # noqa: BLE001
                errores.append("%s: %s" % (cand, str(e)[:30]))
                continue
            if d:
                out[par] = d
                break

    if not out:
        raise RuntimeError("; ".join(errores) or "ningun simbolo reconocido")
    return out


# Nombres alternativos por si TradingView reclasifica o retira un simbolo.
ALTERNOS_TV = {
    "XAU/USD": ["TVC:GOLD", "FX_IDC:XAUUSD", "CAPITALCOM:GOLD", "FOREXCOM:XAUUSD"],
    "XAG/USD": ["TVC:SILVER", "FX_IDC:XAGUSD", "CAPITALCOM:SILVER", "FOREXCOM:XAGUSD"],
    "XPT/USD": ["TVC:PLATINUM", "FX_IDC:XPTUSD", "CAPITALCOM:PLATINUM"],
    "XCU/USD": ["TVC:COPPER", "CAPITALCOM:COPPER", "COMEX:HG1!"],
    "DXY": ["TVC:DXY", "CAPITALCOM:DXY", "ICEUS:DX1!", "INDEX:DXY"],
    "USD/CNH": ["FX:USDCNH", "OANDA:USDCNH", "FX_IDC:USDCNH"],
    "USD/TRY": ["FX_IDC:USDTRY", "OANDA:USDTRY"],
    "USD/ZAR": ["FX_IDC:USDZAR", "OANDA:USDZAR"],
    "USD/MXN": ["FX_IDC:USDMXN", "OANDA:USDMXN"],
    "USD/SEK": ["FX_IDC:USDSEK", "OANDA:USDSEK"],
    "USD/NOK": ["FX_IDC:USDNOK", "OANDA:USDNOK"],
}


def fx_tv_ficha(ticker):
    """Cotizacion de un solo simbolo. Sirve para cualquier mercado."""
    campos = "close,change,change_abs,open,high,low,lp,chp,ch,prev_close_price"
    b, _ = traer("https://scanner.tradingview.com/symbol?symbol=%s&fields=%s&no_404=true"
                 % (urllib.parse.quote(ticker, safe=""), campos),
                 headers={"Accept": "application/json",
                          "Origin": "https://www.tradingview.com",
                          "Referer": "https://www.tradingview.com/"}, timeout=15)
    if not b:
        return None
    j = json.loads(b.decode("utf-8", "ignore"))
    if not isinstance(j, dict):
        return None
    px = j.get("close", j.get("lp"))
    if px is None:
        return None
    chp = j.get("change", j.get("chp"))
    chabs = j.get("change_abs", j.get("ch"))
    prev = j.get("prev_close_price")
    if prev is None and chabs is not None:
        prev = px - chabs
    if prev is None:
        prev = j.get("open")
    if chabs is None and prev:
        chabs = px - prev
    if chp is None and prev:
        chp = (px - prev) / prev * 100
    return {"p": px, "prev": prev, "ch": chabs, "chp": chp,
            "hi": j.get("high"), "lo": j.get("low"),
            "fuente": "tradingview", "t": int(time.time() * 1000)}



# --------------------------------------------------------------------------
#  Fuente principal de divisas: tipos de cambio abiertos, sin clave ni
#  autenticacion. Yahoo dejo de servir los simbolos de divisas ("=X") sin
#  credencial, aunque si sirve los de futuros ("=F"): de ahi que solo el oro
#  apareciera. Desde aqui se construyen todos los pares.
# --------------------------------------------------------------------------
def _pares_desde_usd(hoy, ayer):
    """hoy/ayer: dict divisa -> unidades por 1 USD."""
    out = {}
    def cruce(tab, a, b):
        # unidades de b por 1 a, partiendo de tipos contra el dolar
        if a == "USD":
            return tab.get(b)
        if b == "USD":
            v = tab.get(a)
            return (1.0 / v) if v else None
        va, vb = tab.get(a), tab.get(b)
        return (vb / va) if va and vb else None
    for nombre, _st, _y, dec in PARES_FX:
        if "/" not in nombre or nombre in ("XAU/USD", "XAG/USD", "XPT/USD", "XCU/USD"):
            continue
        a, b = nombre.split("/")
        p = cruce(hoy, a, b)
        if p is None:
            continue
        q = cruce(ayer, a, b) if ayer else None
        out[nombre] = {"p": round(p, 8), "prev": (round(q, 8) if q else None),
                       "ch": (p - q) if q else None,
                       "chp": ((p - q) / q * 100) if q else None,
                       "hi": None, "lo": None, "fuente": "tipos abiertos",
                       "t": int(time.time() * 1000)}
    return out


def fx_abiertos():
    hoy = {}
    b, _ = traer("https://open.er-api.com/v6/latest/USD",
                 headers={"Accept": "application/json"}, timeout=18)
    j = json.loads(b.decode("utf-8", "ignore"))
    if j.get("result") != "success" or not j.get("rates"):
        raise RuntimeError("respuesta no valida")
    hoy = {k: float(v) for k, v in j["rates"].items() if isinstance(v, (int, float))}
    hoy["USD"] = 1.0
    # Cierre anterior para poder dar variacion del dia.
    ayer = {}
    try:
        b2, _ = traer("https://api.frankfurter.app/latest?from=USD",
                      headers={"Accept": "application/json"}, timeout=15)
        j2 = json.loads(b2.decode("utf-8", "ignore"))
        d = j2.get("date")
        b3, _ = traer("https://api.frankfurter.app/%s?from=USD" % d,
                      headers={"Accept": "application/json"}, timeout=15)
        j3 = json.loads(b3.decode("utf-8", "ignore"))
        ayer = {k: float(v) for k, v in (j3.get("rates") or {}).items()}
        ayer["USD"] = 1.0
    except Exception:                                    # noqa: BLE001
        ayer = {}
    return _pares_desde_usd(hoy, ayer)

def fx_stooq():
    """Un unico CSV con todos los instrumentos. Separador '+', no coma."""
    syms = "+".join(p[1] for p in PARES_FX)
    url = "https://stooq.com/q/l/?s=%s&f=sd2t2ohlcv&h&e=csv" % syms
    b, _ = traer(url, timeout=20)
    txt = b.decode("utf-8", "ignore")
    filas = [l for l in txt.splitlines() if l.strip()]
    if len(filas) < 2:
        raise RuntimeError("respuesta con %d filas" % len(filas))
    cab = [c.strip().lower() for c in filas[0].split(",")]
    def col(c, n):
        try:
            v = c[cab.index(n)].strip()
            return float(v)
        except Exception:                                # noqa: BLE001
            return None
    inv = {p[1]: p[0] for p in PARES_FX}
    out = {}
    for l in filas[1:]:
        c = l.split(",")
        try:
            sym = c[cab.index("symbol")].strip().lower()
        except Exception:                                # noqa: BLE001
            continue
        par = inv.get(sym)
        if not par:
            continue
        cierre, ap = col(c, "close"), col(c, "open")
        if cierre is None:
            continue
        out[par] = {"p": cierre, "prev": ap,
                    "ch": (cierre - ap) if ap is not None else None,
                    "chp": ((cierre - ap) / ap * 100) if ap else None,
                    "hi": col(c, "high"), "lo": col(c, "low"),
                    "fuente": "stooq", "t": int(time.time() * 1000)}
    return out


def fx_yahoo(ysym):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(ysym) + "?interval=5m&range=1d")
    b, _ = traer(url, headers={"Accept": "application/json"}, timeout=18)
    j = json.loads(b.decode("utf-8", "ignore"))
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError("sin resultado")
    m = res[0].get("meta") or {}
    q = ((res[0].get("indicators") or {}).get("quote") or [{}])[0]
    serie = [x for x in (q.get("close") or []) if x is not None]
    prev = m.get("chartPreviousClose", serie[0] if serie else None)
    ult = m.get("regularMarketPrice", serie[-1] if serie else None)
    if ult is None:
        raise RuntimeError("sin precio")
    return {"p": ult, "prev": prev,
            "ch": (ult - prev) if prev is not None else None,
            "chp": ((ult - prev) / prev * 100) if prev else None,
            "hi": m.get("regularMarketDayHigh") or (max(serie) if serie else None),
            "lo": m.get("regularMarketDayLow") or (min(serie) if serie else None),
            "serie": serie[-120:], "fuente": "yahoo", "t": int(time.time() * 1000)}


# --------------------------------------------------------------------------
#  COMPARATIVA DE ECONOMIAS
#
#  Los indicadores salen del canal publico de TradingView, que ya responde
#  para las cotizaciones. Dos familias de simbolos:
#    ECONOMICS:xxINTR  tipo oficial, IPC, paro, PIB, PMI, balanza...
#    TVC:xx10Y         rentabilidad de la deuda publica
#  El bono a dos anos es el mejor reflejo de lo que el mercado espera que
#  haga el banco central: por eso pesa mas que el resto en la puntuacion.
# --------------------------------------------------------------------------
ECONOMIAS = {
    "USD": {"n": "Estados Unidos", "cc": "US", "bono": "US"},
    "EUR": {"n": "Zona euro",      "cc": "EU", "bono": "DE"},
    "GBP": {"n": "Reino Unido",    "cc": "GB", "bono": "GB"},
    "JPY": {"n": "Japon",          "cc": "JP", "bono": "JP"},
    "CHF": {"n": "Suiza",          "cc": "CH", "bono": "CH"},
    "CAD": {"n": "Canada",         "cc": "CA", "bono": "CA"},
    "AUD": {"n": "Australia",      "cc": "AU", "bono": "AU"},
    "NZD": {"n": "Nueva Zelanda",  "cc": "NZ", "bono": "NZ"},
}

# clave -> (sufijo de TradingView, etiqueta, unidad, sentido)
# sentido +1: mas alto favorece a la divisa. -1: mas alto la perjudica.
INDICADORES = [
    ("tipo",    "INTR",   "Tipo de interes oficial",     "%",  1),
    ("ipc",     "IRYY",   "IPC interanual",              "%",  1),
    ("ipcsub",  "CORECPIRATE", "IPC subyacente",         "%",  1),
    ("paro",    "UR",     "Tasa de paro",                "%", -1),
    ("pib",     "GDPYY",  "PIB interanual",              "%",  1),
    ("pmiman",  "MPMI",   "PMI manufacturero",           "",   1),
    ("pmiser",  "SPMI",   "PMI de servicios",            "",   1),
    ("ventas",  "RSYY",   "Ventas minoristas interanual", "%", 1),
    ("balanza", "BOT",    "Balanza comercial",           "",   1),
]
BONOS = [("b2", "02Y", "Bono a 2 anos", "%", 1),
         ("b10", "10Y", "Bono a 10 anos", "%", 1)]

# Peso de cada indicador en la puntuacion de fuerza.
PESOS = {"b2": 3.0, "tipo": 2.5, "b10": 1.5, "ipc": 1.2, "ipcsub": 1.2,
         "pib": 1.2, "paro": 1.0, "pmiman": 0.8, "pmiser": 0.8,
         "ventas": 0.6, "balanza": 0.4}


def simbolos_comparativa():
    """Todos los tickers a pedir, con la clave a la que corresponden."""
    out = {}
    for div, e in ECONOMIAS.items():
        for clave, suf, _et, _u, _s in INDICADORES:
            out["ECONOMICS:%s%s" % (e["cc"], suf)] = (div, clave)
        for clave, suf, _et, _u, _s in BONOS:
            out["TVC:%s%s" % (e["bono"], suf)] = (div, clave)
    return out


def bucle_comparativa():
    """Se refresca despacio: son datos que cambian de mes en mes, no de tick."""
    primera = True
    while not PARAR.is_set():
        mapa = simbolos_comparativa()
        datos, fallos = {}, 0
        for ticker, (div, clave) in mapa.items():
            if PARAR.is_set():
                break
            try:
                d = fx_tv_ficha(ticker)
            except Exception:                            # noqa: BLE001
                d = None
            if d and d.get("p") is not None:
                datos.setdefault(div, {})[clave] = {
                    "v": d["p"], "prev": d.get("prev"), "ch": d.get("ch")}
            else:
                fallos += 1
            time.sleep(0.12)                             # sin prisa: no conviene saturar

        if datos:
            with _lock:
                ESTADO["macro"] = datos
                ESTADO["diag"]["macro"] = ("%d economias · %d indicadores · %d sin dato"
                                           % (len(datos),
                                              sum(len(v) for v in datos.values()), fallos))
            publicar("macro", datos)
            if primera:
                print("  macro     %d economias · %d indicadores leidos · %d sin dato"
                      % (len(datos), sum(len(v) for v in datos.values()), fallos))
                sys.stdout.flush()
        else:
            with _lock:
                ESTADO["diag"]["macro"] = "sin datos · %d simbolos fallan" % fallos
            print("  macro     SIN DATOS · %d simbolos fallan" % fallos)
            sys.stdout.flush()
        primera = False
        PARAR.wait(3600)


# --------------------------------------------------------------------------
#  ANALISIS COMPARTIDOS
#
#  Todo lo que no es una pregunta personal se calcula UNA sola vez aqui y se
#  sirve identico a todos. Dos motivos:
#
#    1. Docente. Si dos alumnos ven sesgos distintos para el mismo par, no se
#       puede dar clase con ello. El analisis debe ser el mismo objeto, con la
#       misma hora de calculo.
#    2. Coste. Con 92 alumnos, calcular por navegador multiplica la factura
#       por 92. Calculado aqui, el gasto no depende del numero de alumnos.
#
#  El contexto lo construye el servidor con SU estado, no lo manda el cliente:
#  asi es identico por construccion, no por confianza.
#
#  Un solo vuelo: si llegan cincuenta peticiones a la vez y el analisis esta
#  caducado, se calcula una vez y las cincuenta esperan ese mismo resultado.
# --------------------------------------------------------------------------
ANALISIS = {}                    # clave -> {"t", "texto", "modelo", "buscado"}
_LOCK_ANL = threading.Lock()
_VUELOS = {}                     # clave -> threading.Event

VIDA_ANALISIS = {"sesgo": 1800, "comparativa": 3600, "titulares": 900}


def _ctx_titulares(n=26, divisas=None):
    with _lock:
        L = list(ESTADO.get("fx") or [])
    if divisas:
        L = [x for x in L if set(x.get("div") or []) & set(divisas)]
    out = []
    for x in L[:n]:
        edad = int((time.time() * 1000 - x.get("ts", 0)) / 60000)
        out.append("- [hace %d min] (%s) %s" % (edad, "/".join(x.get("div") or []) or "-",
                                                x.get("title", "")))
    return "\n".join(out) or "sin titulares"


def _ctx_calendario(n=14, divisas=None):
    ahora = time.time() * 1000
    with _lock:
        L = [e for e in (ESTADO.get("calendario") or []) if e.get("ts", 0) > ahora]
    if divisas:
        L = [e for e in L if e.get("pais") in divisas]
    out = []
    for e in L[:n]:
        t = time.strftime("%d/%m %H:%M", time.localtime(e["ts"] / 1000))
        out.append("- %s [%s] %s (impacto %s/3%s%s)"
                   % (t, e.get("pais", ""), e.get("ev", ""), e.get("imp", 0),
                      ", previsto " + str(e["fc"]) if e.get("fc") else "",
                      ", anterior " + str(e["prev"]) if e.get("prev") else ""))
    return "\n".join(out) or "sin eventos"


def _ctx_macro(A, B):
    with _lock:
        m = dict(ESTADO.get("macro") or {})
    ET = {"tipo": "Tipo oficial", "b2": "Bono 2 anos", "b10": "Bono 10 anos",
          "ipc": "IPC interanual", "ipcsub": "IPC subyacente", "pib": "PIB interanual",
          "paro": "Tasa de paro", "pmiman": "PMI manufacturero", "pmiser": "PMI servicios",
          "ventas": "Ventas minoristas", "balanza": "Balanza comercial"}
    out = []
    for k, et in ET.items():
        va = (m.get(A) or {}).get(k, {}).get("v")
        vb = (m.get(B) or {}).get(k, {}).get("v")
        if va is None and vb is None:
            continue
        out.append("- %s: %s %s | %s %s" % (et, A, "-" if va is None else round(va, 3),
                                            B, "-" if vb is None else round(vb, 3)))
    return "\n".join(out) or "sin indicadores cargados"


def _ctx_precios(pares=None):
    with _lock:
        c = dict(ESTADO.get("cotiz") or {})
    out = []
    for k in (pares or list(c)[:14]):
        v = c.get(k)
        if not v or v.get("p") is None:
            continue
        out.append("- %s %s (%s%%)" % (k, v["p"],
                   ("+" if (v.get("chp") or 0) >= 0 else "") + str(round(v.get("chp") or 0, 2))))
    return "\n".join(out) or "sin cotizaciones"


SIS_COMPARATIVA = (
    "Eres profesor de macroeconomia aplicada al mercado de divisas en una academia espanola. "
    "Te dan la comparativa de dos economias, los titulares recientes y los datos que vienen. "
    "Explicas al alumno que economia esta mas fuerte y por que.\n"
    "Estructura, sin encabezados en mayusculas ni listas numeradas:\n"
    "1) Veredicto: que economia manda y por que mecanismo concreto.\n"
    "2) El diferencial de tipos y la deuda: es el canal principal, explicalo.\n"
    "3) Que contradice ese veredicto: que indicador o noticia va en contra.\n"
    "4) Que dato proximo puede darle la vuelta y en que direccion.\n"
    "Reglas: castellano llano; si usas un termino tecnico, aclaralo en la misma frase; "
    "apoyate en los numeros que te dan y citalos; no inventes datos; no des precios "
    "objetivo ni recomiendes operar. Maximo 320 palabras.")


SIS_SESGO = (
    "Eres el analista jefe de divisas de una mesa institucional. Respondes en "
    "espanol de Espana. Devuelves EXCLUSIVAMENTE un objeto JSON valido, sin texto "
    "antes ni despues y sin bloques de codigo.\n"
    "METODO OBLIGATORIO, en este orden:\n"
    "1. FILTRA los titulares: descarta opinion, promocion, resumenes de precio sin "
    "causa y todo lo que no altere la politica monetaria, los flujos o la prima de "
    "riesgo. Quedate con lo que iria en la nota de la manana de una mesa.\n"
    "2. RAZONA EL DIFERENCIAL: en un par lo que decide la direccion es la diferencia "
    "entre las dos economias y sus bancos centrales, no la fuerza de una sola. Un "
    "dato flojo en Estados Unidos es ALCISTA para EUR/USD aunque la noticia suene "
    "negativa.\n"
    "3. CONCLUYE con la direccion, con conviccion proporcional a la evidencia. Si los "
    "factores se contradicen, el sesgo es neutro y lo dices: no fuerces una direccion "
    "para parecer util.\n"
    "Cada afirmacion lleva la cifra o el nivel exacto. Sin adornos.\n"
    "IMPORTANTE: te damos el FONDO MACRO ESTRUCTURAL que calcula el terminal. Tu "
    "sesgo es TACTICO, del dia, y puede ir en contra del fondo: eso es legitimo si "
    "el fondo ya esta descontado en el precio o si hoy el riesgo del dato va en "
    "sentido contrario. Pero si tu sesgo contradice al fondo, estas OBLIGADO a "
    "explicarlo en el campo \"divergencia\" en una frase, y a incluir el fondo entre "
    "las razones contrarias. Si coinciden, deja \"divergencia\" vacio.")


def _pivotes(o):
    """Soportes y resistencias de referencia sobre la sesion anterior."""
    if not o or o.get("hi") is None or o.get("lo") is None or o.get("p") is None:
        return []
    H, L, C = float(o["hi"]), float(o["lo"]), float(o["p"])
    P = (H + L + C) / 3.0
    return [("Resistencia 2", P + (H - L)), ("Resistencia 1", 2 * P - L),
            ("Pivote", P), ("Soporte 1", 2 * P - H), ("Soporte 2", P - (H - L)),
            ("Maximo de la sesion", H), ("Minimo de la sesion", L)]


# Mismo reparto por margen que usa la pestana Comparativa: peso, umbral a
# partir del cual la ventaja se considera decisiva, y sentido (+1 = mas alto
# favorece a esa divisa).
IND_FUERZA = [
    ("b2", 3.0, 1.00, 1), ("tipo", 2.5, 1.00, 1), ("b10", 1.5, 1.00, 1),
    ("ipc", 1.2, 1.00, 1), ("ipcsub", 1.2, 0.80, 1), ("pib", 1.2, 1.50, 1),
    ("paro", 1.0, 1.50, -1), ("pmiman", 0.8, 3.00, 1), ("pmiser", 0.8, 3.00, 1),
    ("ventas", 0.6, 2.00, 1), ("balanza", 0.4, 0.00, 1),
]


def fuerza_macro(A, B):
    """Devuelve (fuerza de A en %, indicadores comparados)."""
    with _lock:
        m = dict(ESTADO.get("macro") or {})
    pa = pb = 0.0
    n = 0
    for clave, peso, umbral, sen in IND_FUERZA:
        va = (m.get(A) or {}).get(clave, {}).get("v")
        vb = (m.get(B) or {}).get(clave, {}).get("v")
        if va is None or vb is None:
            continue
        n += 1
        u = umbral if umbral > 0 else max(1e-9, (abs(va) + abs(vb)) / 2 * 0.5)
        f = min(1.0, abs(va - vb) / u)
        if va == vb:
            cuota = 0.5
        else:
            gana_a = (va > vb) if sen > 0 else (va < vb)
            cuota = 0.5 + 0.5 * f if gana_a else 0.5 - 0.5 * f
        pa += peso * cuota
        pb += peso * (1 - cuota)
    tot = pa + pb
    return (round(pa / tot * 100, 1) if tot else 50.0), n


def _ctx_sesgo(par):
    with _lock:
        o = dict((ESTADO.get("cotiz") or {}).get(par) or {})
        L = list(ESTADO.get("fx") or [])
    divs = set(par.split("/"))
    propias = [x for x in L if set(x.get("div") or []) & divs][:10]
    resto = [x for x in L if x not in propias and (x.get("imp") or 0) >= 1][:12]
    tit = propias + resto

    ahora = time.time() * 1000
    with _lock:
        ev = [e for e in (ESTADO.get("calendario") or [])
              if e.get("ts", 0) > ahora and (e.get("imp") or 0) >= 2][:10]

    t = "PAR: %s · cotizacion %s · variacion %s%% · rango del dia %s-%s\n" % (
        par, o.get("p", "n/d"),
        round(o.get("chp") or 0, 2), o.get("lo", "n/d"), o.get("hi", "n/d"))
    piv = _pivotes(o)
    t += "\nNIVELES CALCULADOS:\n" + ("\n".join("  %s: %s" % (n, round(v, 5)) for n, v in piv)
                                       or "  n/d")
    A, B = par.split("/")
    fa, nind = fuerza_macro(A, B)
    t += ("\n\nFONDO MACRO ESTRUCTURAL (calculado por el terminal sobre %d indicadores):\n"
          "  %s %s%% frente a %s %s%%\n"
          "  Es la foto de que economia esta mas fuerte AHORA. No es el sesgo del dia.\n"
          % (nind, A, fa, B, round(100 - fa, 1)))
    t += "\nINDICADORES DE LAS DOS ECONOMIAS:\n" + _ctx_macro(A, B)
    t += "\n\nOTRAS COTIZACIONES:\n" + _ctx_precios()
    t += "\n\nTITULARES NUMERADOS:\n" + ("\n".join(
        "[%d] %s (%s, hace %d min)" % (i, x.get("title", ""), x.get("src", ""),
                                       int((ahora - x.get("ts", 0)) / 60000))
        for i, x in enumerate(tit)) or "ninguno")
    t += "\n\nEVENTOS DE AGENDA NUMERADOS:\n" + ("\n".join(
        "(%d) %s · %s · %s%s" % (i, e.get("ev", ""), e.get("pais", ""),
                                 time.strftime("%d/%m %H:%M", time.localtime(e["ts"] / 1000)),
                                 (" · previsto " + str(e["fc"])) if e.get("fc") else "")
        for i, e in enumerate(ev)) or "ninguno")
    return t, tit, ev


PETICION_SESGO = (
    "Determina el sesgo de hoy para %s y devuelve este JSON exacto:\n"
    '{"sesgo":"alcista|bajista|neutro","conviccion":"alta|moderada|baja","reversion":true|false,'
    '"clave_hoy":"una frase con lo que decide la sesion",'
    '"sustentan":[{"texto":"...","clave":true,"tipo":"tecnico|macro|politico|flujo|noticia","refs":[0],"eventos":[0]}],'
    '"contrarias":[{"texto":"...","tipo":"tecnico|macro|politico|flujo|noticia","refs":[1],"eventos":[1]}],'
    '"niveles":[{"v":1.1520,"n":"soporte del rango","t":"s"},{"v":1.1610,"n":"resistencia semanal","t":"r"}],'
    '"descartados":0}\n\n'
    "REGLAS: de 4 a 6 entradas por lista, ordenadas de mayor a menor peso. Cada "
    '"texto" es UNA frase de 12 a 22 palabras que incluye la cifra o el nivel exacto. '
    '"refs" son indices de TITULARES NUMERADOS y "eventos" indices de EVENTOS DE AGENDA '
    "NUMERADOS: usa solo los que existan, y deja la lista vacia si no aplica. "
    '"descartados" es cuantos titulares has descartado por irrelevantes.')

def _receta(clave):
    """Traduce una clave a (sistema, mensaje, maxtok, buscar, modelo, busquedas).

    El prompt vive aqui, en el servidor: el cliente solo pide una clave. Asi
    dos alumnos no pueden recibir analisis distintos ni aunque lo intenten.
    """
    tipo, _, arg = clave.partition(":")

    if tipo == "comparativa":
        A, _, B = arg.partition("-")
        if A not in ECONOMIAS or B not in ECONOMIAS:
            raise ValueError("economias no validas")
        ctx = ("COMPARATIVA %s frente a %s\n\nINDICADORES:\n%s\n\n"
               "TITULARES RECIENTES DE ESAS DIVISAS:\n%s\n\n"
               "PROXIMOS DATOS DE ESAS ECONOMIAS:\n%s\n\nCOTIZACIONES:\n%s"
               % (A, B, _ctx_macro(A, B), _ctx_titulares(18, {A, B}),
                  _ctx_calendario(14, {A, B}), _ctx_precios()))
        return (SIS_COMPARATIVA + POLITICA_BUSQUEDA,
                "DATOS DEL TERMINAL:\n" + ctx + "\n\nExplica la comparativa siguiendo la estructura.",
                2400, True, MODELOS["comparativa"], 3, {})

    if tipo == "sesgo":
        par = arg.upper()
        with _lock:
            existe = par in (ESTADO.get("cotiz") or {})
        if not existe:
            raise ValueError("par sin cotizacion: %s" % par)
        ctx, tit, ev = _ctx_sesgo(par)
        A, B = par.split("/")
        fa, nind = fuerza_macro(A, B)
        return (SIS_SESGO + POLITICA_BUSQUEDA,
                "DATOS:\n" + ctx + "\n\n" + (PETICION_SESGO % par),
                8000, True, MODELOS["sesgo"], 4,
                {"tit": [{"title": x.get("title"), "src": x.get("src"),
                          "ts": x.get("ts"), "link": x.get("link"),
                          "div": x.get("div") or []} for x in tit],
                 "ev": [{"ev": e.get("ev"), "pais": e.get("pais"), "ts": e.get("ts"),
                         "fc": e.get("fc"), "prev": e.get("prev"), "imp": e.get("imp")}
                        for e in ev],
                 "sym": par, "fondo": {"a": A, "b": B, "fuerzaA": fa,
                                       "fuerzaB": round(100 - fa, 1), "n": nind}})

    raise ValueError("clave desconocida: %s" % clave)


def analisis_compartido(clave, forzar=False):
    """Devuelve el analisis de esa clave, calculandolo solo si hace falta."""
    tipo = clave.split(":")[0]
    vida = VIDA_ANALISIS.get(tipo, 1800)

    with _LOCK_ANL:
        d = ANALISIS.get(clave)
        if d and not forzar and (time.time() - d["t"]) < vida:
            return dict(d, cache=True)
        ev = _VUELOS.get(clave)
        if ev is None:
            ev = threading.Event()
            _VUELOS[clave] = ev
            mio = True
        else:
            mio = False

    if not mio:
        # Otro lo esta calculando: se espera su resultado en vez de pedir otro.
        ev.wait(timeout=180)
        with _LOCK_ANL:
            d = ANALISIS.get(clave)
        if d:
            return dict(d, cache=True, esperado=True)
        raise RuntimeError("el calculo en curso no devolvio nada")

    try:
        sistema, mensaje, maxtok, buscar, modelo, busq, extra = _receta(clave)
        r = llamar_ia(sistema, [{"role": "user", "content": mensaje}],
                      maxtok=maxtok, buscar=buscar, modelo_pedido=modelo, busquedas=busq)
        texto = (r.get("texto") or "").strip()
        if not texto:
            raise RuntimeError("respuesta vacia")
        d = {"t": time.time(), "clave": clave, "texto": texto,
             "modelo": r.get("modelo", ""), "fuentes": r.get("fuentes", []),
             "buscado": bool(buscar)}
        d.update(extra or {})
        # Si el analisis pedia JSON, se valida aqui: mejor fallar en el
        # servidor que enviar basura a noventa y dos paneles.
        if clave.startswith("sesgo:"):
            m = re.search(r"\{[\s\S]*\}", texto)
            if not m:
                raise RuntimeError("la respuesta no trae JSON")
            d["j"] = json.loads(m.group(0))
        with _LOCK_ANL:
            ANALISIS[clave] = d
        publicar("analisis", {"clave": clave, "texto": texto,
                              "t": int(d["t"] * 1000), "modelo": d.get("modelo", ""),
                              "j": d.get("j"), "tit": d.get("tit"),
                              "ev": d.get("ev"), "sym": d.get("sym"),
                              "fondo": d.get("fondo")})
        return dict(d, cache=False)
    finally:
        with _LOCK_ANL:
            _VUELOS.pop(clave, None)
        ev.set()


def bucle_analisis():
    """Precalienta lo que todos van a mirar, para que nadie espere.

    Sin clave de IA no hace nada: no es un fallo, es que el asistente no
    esta configurado.
    """
    PARAR.wait(90)                                       # deja que carguen los datos
    while not PARAR.is_set():
        with _LOCK_IA:
            hay = bool(IA["clave"])
        if hay:
            with _lock:
                hay_macro = len(ESTADO.get("macro") or {}) >= 2
            if hay_macro:
                for par in ("EUR-USD", "GBP-USD", "USD-JPY"):
                    if PARAR.is_set():
                        break
                    try:
                        analisis_compartido("comparativa:" + par)
                    except Exception as e:               # noqa: BLE001
                        print("  analisis  comparativa %s falla: %s" % (par, str(e)[:60]))
                        sys.stdout.flush()
                    PARAR.wait(5)
            for par in ("EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"):
                if PARAR.is_set():
                    break
                try:
                    analisis_compartido("sesgo:" + par)
                except Exception as e:                   # noqa: BLE001
                    print("  analisis  sesgo %s falla: %s" % (par, str(e)[:70]))
                    sys.stdout.flush()
                PARAR.wait(5)
                with _lock:
                    ESTADO["diag"]["analisis"] = "%d analisis compartidos en memoria" % len(ANALISIS)
        PARAR.wait(900)


def bucle_cotizaciones():
    primera = True
    while not PARAR.is_set():
        datos, via = {}, []
        try:
            datos = fx_tradingview()
            via.append("TradingView %d" % len(datos))
        except Exception as e:                           # noqa: BLE001
            via.append("TradingView falla (%s)" % str(e)[:38])
        if len(datos) < len(PARES_FX):
            try:
                ab = fx_abiertos()
                n_ab = 0
                for k, v in ab.items():
                    if k not in datos:
                        datos[k] = v
                        n_ab += 1
                if n_ab:
                    via.append("tipos abiertos %d" % n_ab)
            except Exception as e:                       # noqa: BLE001
                via.append("tipos abiertos fallan (%s)" % str(e)[:34])

        # Lo que falte, uno a uno y espaciado, para no acabar limitados.
        faltan = [p for p in PARES_FX if p[0] not in datos]
        ok_y = 0
        for nombre, _s, ysym, _d in faltan:
            if PARAR.is_set():
                break
            try:
                datos[nombre] = fx_yahoo(ysym)
                ok_y += 1
            except Exception:                            # noqa: BLE001
                pass
            time.sleep(0.35)
        if ok_y:
            via.append("Yahoo %d" % ok_y)

        # El par principal necesita la serie intradia para medias e impulso.
        try:
            d = fx_yahoo("EURUSD=X")
            datos.setdefault("EUR/USD", {}).update({k: v for k, v in d.items() if k == "serie"})
        except Exception:                                # noqa: BLE001
            pass

        if datos:
            with _lock:
                ESTADO["cotiz"] = datos
                sin = [p[0] for p in PARES_FX if p[0] not in datos]
                ESTADO["diag"]["cotiz"] = ("%d de %d · %s%s"
                    % (len(datos), len(PARES_FX), " + ".join(via),
                       (" · sin dato: " + ", ".join(sin)) if sin else ""))
            publicar("cotiz", datos)
            if primera or len(datos) < len(PARES_FX):
                sin = [p[0] for p in PARES_FX if p[0] not in datos]
                print("  cotiz     %d de %d instrumentos · %s%s"
                      % (len(datos), len(PARES_FX), " + ".join(via),
                         (" · sin dato: " + ", ".join(sin)) if sin else ""))
                sys.stdout.flush()
        else:
            with _lock:
                ESTADO["diag"]["cotiz"] = "sin datos · " + " + ".join(via)
            print("  cotiz     SIN DATOS · %s" % " + ".join(via))
            sys.stdout.flush()
        primera = False
        PARAR.wait(25)

# --------------------------------------------------------------------------
#  Vigilancia de ballenas: transferencias grandes en la red de Bitcoin
#
#  Se miran los bloques recien minados y tambien la memoria de transacciones
#  sin confirmar, que es donde aparecen antes. Umbral por defecto: 50 BTC.
# --------------------------------------------------------------------------
UMBRAL_BALLENA = 50.0
_vistas_bal = set()


def _extremos(t):
    de = []
    para = []
    for v in t.get("vin", [])[:3]:
        p = (v or {}).get("prevout") or {}
        if p.get("scriptpubkey_address"):
            de.append(p["scriptpubkey_address"])
    for v in t.get("vout", [])[:3]:
        if v.get("scriptpubkey_address"):
            para.append(v["scriptpubkey_address"])
    return de, para


def bucle_ballenas():
    primera = True
    while not PARAR.is_set():
        nuevas = []
        try:
            b, _ = traer("https://mempool.space/api/v1/blocks", timeout=15)
            bloques = json.loads(b.decode("utf-8", "ignore"))[:1]
            for bl in bloques:
                b2, _ = traer("https://mempool.space/api/block/%s/txs/0" % bl["id"], timeout=20)
                for t in json.loads(b2.decode("utf-8", "ignore")):
                    tid = t.get("txid")
                    if not tid or tid in _vistas_bal:
                        continue
                    val = sum(o.get("value", 0) for o in t.get("vout", [])) / 1e8
                    if val < UMBRAL_BALLENA:
                        continue
                    _vistas_bal.add(tid)
                    de, para = _extremos(t)
                    nuevas.append({"id": tid, "btc": round(val, 4), "ts": bl["timestamp"] * 1000,
                                   "bloque": bl["height"], "de": de, "para": para,
                                   "estado": "confirmada"})
        except Exception:                                # noqa: BLE001
            pass
        if len(_vistas_bal) > 6000:
            _vistas_bal.clear()
        if nuevas:
            nuevas.sort(key=lambda x: -x["btc"])
            with _lock:
                ESTADO["ballenas"] = (nuevas + ESTADO.get("ballenas", []))[:150]
                ESTADO["diag"]["ballenas"] = "%d movimientos ≥ %g BTC" % (
                    len(ESTADO["ballenas"]), UMBRAL_BALLENA)
            publicar("ballenas", ESTADO["ballenas"] if primera else nuevas)
            if not primera:
                print("  ballenas  %d movimientos (mayor: %.1f BTC)" % (len(nuevas), nuevas[0]["btc"]))
                sys.stdout.flush()
        primera = False
        PARAR.wait(20)


# --------------------------------------------------------------------------
#  Tesorerias institucionales (datos declarados, no atribucion en cadena)
# --------------------------------------------------------------------------
def bucle_tesorerias():
    while not PARAR.is_set():
        try:
            b, _ = traer("https://api.coingecko.com/api/v3/companies/public_treasury/bitcoin",
                         timeout=20)
            j = json.loads(b.decode("utf-8", "ignore"))
            with _lock:
                ESTADO["tesorerias"] = {
                    "total": j.get("total_holdings"),
                    "valor": j.get("total_value_usd"),
                    "pct": j.get("market_cap_dominance"),
                    "empresas": (j.get("companies") or [])[:60],
                }
                ESTADO["diag"]["tesorerias"] = "%d empresas" % len(j.get("companies") or [])
            publicar("tesorerias", ESTADO["tesorerias"])
        except Exception as e:                           # noqa: BLE001
            with _lock:
                ESTADO["diag"]["tesorerias"] = "no disponible"
        PARAR.wait(1800)


# --------------------------------------------------------------------------
#  YouTube
# --------------------------------------------------------------------------
_cache_live, _cache_emb = {}, {}
_LOCK_LIVE = threading.Lock()
COOKIES_YT = ("CONSENT=YES+cb.20240101-00-p0.en+FX+100; "
              "SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwMTAxLjAwX3AwGgJlbiACGgYIgLC_rgY")
CAB_YT = {"Cookie": COOKIES_YT, "Accept-Language": "en-US,en;q=0.9",
          "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "Sec-Fetch-Mode": "navigate", "Upgrade-Insecure-Requests": "1"}
MARCAS_VIVO = ('"isLive":true', "hlsManifestUrl", '"isLiveNow":true',
               "BADGE_STYLE_TYPE_LIVE_NOW", '"liveBroadcastDetails"', '"isLiveContent":true')
MARCAS_CONSENT = ("consent.youtube.com", "Before you continue to YouTube",
                  "Antes de ir a YouTube", 'action="https://consent.youtube')


def _analiza_pagina(txt):
    if any(m in txt for m in MARCAS_CONSENT):
        return "unknown", None, None, "pantalla de consentimiento"
    if len(txt) < 3000:
        return "unknown", None, None, "respuesta demasiado corta"
    vivo = any(m in txt for m in MARCAS_VIVO)
    can = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})"', txt)
    if not vivo and not can:
        if "/channel/" in txt or "channelId" in txt:
            return "off", None, None, "sin emision activa"
        return "unknown", None, None, "sin marcas reconocibles"
    vid = can.group(1) if can else None
    if not vid:
        m = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', txt)
        vid = m.group(1) if m else None
    tit = re.search(r'<meta name="title" content="([^"]{3,180})"', txt)
    if vivo:
        return "live", vid, (tit.group(1) if tit else None), "emision detectada"
    return "off", vid, (tit.group(1) if tit else None), "ultimo video, no directo"


def puede_insertarse(vid):
    if not vid:
        return False
    c = _cache_emb.get(vid)
    if c and time.time() - c[0] < 1800:
        return c[1]
    ok = False
    try:
        b, _ = traer("https://www.youtube.com/oembed?url=https%3A//www.youtube.com/watch%3Fv%3D"
                     + vid + "&format=json", headers={"Accept": "application/json"}, timeout=12)
        ok = b"title" in b
    except HTTPError:
        ok = False
    except Exception:                                    # noqa: BLE001
        ok = True
    _cache_emb[vid] = (time.time(), ok)
    return ok


def videos_recientes(cid, limite=8):
    out = []
    try:
        b, _ = traer("https://www.youtube.com/feeds/videos.xml?channel_id=%s" % cid, timeout=14)
        txt = b.decode("utf-8", "ignore")
        for m in re.finditer(r"<entry>.*?<yt:videoId>([A-Za-z0-9_-]{11})</yt:videoId>.*?"
                             r"<title>(.*?)</title>.*?<published>(.*?)</published>", txt, re.S):
            out.append({"id": m.group(1), "titulo": _txt(m.group(2)).strip(), "fecha": m.group(3)})
            if len(out) >= limite:
                break
    except Exception:                                    # noqa: BLE001
        pass
    return out


def canal_en_directo(cid):
    ahora = time.time()
    with _LOCK_LIVE:
        c = _cache_live.get(cid)
        if c and ahora - c[0] < 150:
            return c[1]
    estado, vid, tit, motivo = "unknown", None, None, "sin respuesta"
    for url in ("https://www.youtube.com/channel/%s/live?hl=en&gl=US&persist_gl=1" % cid,
                "https://www.youtube.com/embed/live_stream?channel=%s" % cid,
                "https://www.youtube.com/channel/%s/streams?hl=en&gl=US" % cid):
        try:
            b, _ = traer(url, headers=CAB_YT, timeout=16)
            e, v, t, mo = _analiza_pagina(b.decode("utf-8", "ignore"))
            if e == "live":
                estado, vid, tit, motivo = e, v, t, mo
                break
            if e == "off" and estado == "unknown":
                estado, vid, tit, motivo = e, v, t, mo
        except HTTPError as ex:
            motivo = "HTTP %s" % ex.code
        except Exception as ex:                          # noqa: BLE001
            motivo = str(ex)[:60]
    recientes = videos_recientes(cid)
    if estado != "live" and not vid and recientes:
        vid = recientes[0]["id"]
        tit = tit or recientes[0]["titulo"]
    emb = puede_insertarse(vid) if vid else False
    alt = None
    if vid and not emb:
        for v in recientes:
            if v["id"] != vid and puede_insertarse(v["id"]):
                alt = v
                break
    res = {"estado": estado, "live": estado == "live", "video": vid, "titulo": tit,
           "motivo": motivo, "embebible": emb, "alternativa": alt, "recientes": recientes}
    with _LOCK_LIVE:
        _cache_live[cid] = (ahora, res)
    return res


# --------------------------------------------------------------------------
#  Exchanges
# --------------------------------------------------------------------------
def binance_trades(key, secret, simbolos):
    ops, errores = [], []
    for sym in simbolos:
        par = sym + "USDT"
        q = "symbol=%s&limit=1000&timestamp=%d&recvWindow=20000" % (par, int(time.time() * 1000))
        firma = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        try:
            b, _ = traer("https://api.binance.com/api/v3/myTrades?%s&signature=%s" % (q, firma),
                         headers={"X-MBX-APIKEY": key}, timeout=20)
            for t in json.loads(b):
                ops.append({"id": "bin-%s" % t["id"], "type": "buy" if t["isBuyer"] else "sell",
                            "asset": sym, "qty": float(t["qty"]), "price": float(t["price"]),
                            "fee": float(t["commission"]) * (float(t["price"]) if t["commissionAsset"] == sym else 1.0),
                            "date": time.strftime("%Y-%m-%d", time.gmtime(t["time"] / 1000)),
                            "note": "Binance"})
        except HTTPError as e:
            cuerpo = e.read().decode("utf-8", "ignore")[:180]
            if e.code in (401, 403):
                raise RuntimeError("Binance rechaza la clave (%s): %s" % (e.code, cuerpo))
            errores.append("%s %s" % (par, e.code))
        except Exception as e:                           # noqa: BLE001
            errores.append("%s %s" % (par, e))
    return ops, errores


def kraken_trades(key, secret):
    ruta = "/0/private/TradesHistory"
    nonce = str(int(time.time() * 1000))
    post = urllib.parse.urlencode({"nonce": nonce})
    sha = hashlib.sha256((nonce + post).encode()).digest()
    mac = hmac.new(base64.b64decode(secret), ruta.encode() + sha, hashlib.sha512)
    cab = {"API-Key": key, "API-Sign": base64.b64encode(mac.digest()).decode(),
           "Content-Type": "application/x-www-form-urlencoded"}
    b, _ = traer("https://api.kraken.com" + ruta, headers=cab, data=post.encode(), timeout=25)
    d = json.loads(b)
    if d.get("error"):
        raise RuntimeError("Kraken: " + "; ".join(d["error"]))
    ops = []
    for tid, t in (d.get("result", {}).get("trades", {}) or {}).items():
        base = re.sub(r"(USD|EUR|USDT|ZUSD|ZEUR)$", "", t["pair"])
        base = re.sub(r"^[XZ]", "", base)
        base = {"XBT": "BTC", "XXBT": "BTC", "XETH": "ETH"}.get(base, base)
        ops.append({"id": "kr-%s" % tid, "type": "buy" if t["type"] == "buy" else "sell",
                    "asset": base, "qty": float(t["vol"]), "price": float(t["price"]),
                    "fee": float(t["fee"]),
                    "date": time.strftime("%Y-%m-%d", time.gmtime(float(t["time"]))),
                    "note": "Kraken"})
    return ops, []


# --------------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=RAIZ, **kw)

    def log_message(self, fmt, *args):
        p = self.path or ""
        if p.startswith("/api/exchange"):
            sys.stdout.write("  exchange  sincronizacion solicitada\n")
            sys.stdout.flush()

    def _cab(self, code, tipo, largo=None, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", tipo)
        if largo is not None:
            self.send_header("Content-Length", str(largo))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _t(self, code, msg):
        b = msg.encode("utf-8")
        self._cab(code, "text/plain; charset=utf-8", len(b))
        self.wfile.write(b)

    def _j(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._cab(code, "application/json; charset=utf-8", len(b))
        self.wfile.write(b)

    def _redir(self, destino):
        self.send_response(302)
        self.send_header("Location", destino)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _html(self, texto):
        b = texto.encode("utf-8")
        self._cab(200, "text/html; charset=utf-8", len(b))
        self.wfile.write(b)

    def do_OPTIONS(self):
        self._cab(204, "text/plain", 0)

    def do_POST(self):
        ruta = urlparse(self.path).path

        # Misma puerta en las escrituras. El EA entra con su propio codigo de
        # vinculacion, no con sesion de navegador.
        ses = sesion_de(self)
        if login_activo() and not ruta.startswith("/ea/v1/") \
                and ruta not in RUTAS_LIBRES and not ses:
            return self._j(401, {"error": "sesion no valida", "login": "/login"})
        if ses and _requiere_pago(ses) and ruta not in RUTAS_LIBRES:
            return self._j(402, {"error": "sin suscripcion activa", "pago": "/pago"})

        if ruta == "/api/registro":
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
            except Exception:                            # noqa: BLE001
                return self._j(400, {"error": "Cuerpo no valido"})
            email = (d.get("email") or "").strip().lower()
            clave = d.get("clave") or ""
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                return self._j(400, {"error": "Correo no valido"})
            if len(clave) < 6:
                return self._j(400, {"error": "La contrasena necesita 6 caracteres o mas"})
            with _LOCK_CUENTAS:
                usuarios = usuarios_carga()
                if email in usuarios:
                    return self._j(409, {"error": "Ya existe una cuenta con ese correo"})
                sal, h = _hash_clave(clave)
                uid = _uuid.uuid4().hex
                usuarios[email] = {"id": uid, "sal": sal, "hash": h, "creada": time.time()}
                _guarda_json(USUARIOS_ARCHIVO, usuarios)
            ficha = {"id": uid, "email": email, "nombre": email.split("@")[0], "tipo": "nativo"}
            tok = nueva_sesion(ficha)
            print("  cuenta    nueva %s" % email)
            sys.stdout.flush()
            b = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            seguro = "; Secure" if _base_url(self).startswith("https") else ""
            self.send_header("Set-Cookie", "dyn_sesion=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s"
                             % (tok, VIDA_SESION, seguro))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return

        if ruta == "/api/login":
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
            except Exception:                            # noqa: BLE001
                return self._j(400, {"error": "Cuerpo no valido"})
            email = (d.get("email") or "").strip().lower()
            clave = d.get("clave") or ""
            ficha_u = usuarios_carga().get(email)
            if not ficha_u or not _verifica_clave(clave, ficha_u["sal"], ficha_u["hash"]):
                return self._j(401, {"error": "Correo o contrasena incorrectos"})
            ficha = {"id": ficha_u["id"], "email": email, "nombre": email.split("@")[0], "tipo": "nativo"}
            tok = nueva_sesion(ficha)
            print("  cuenta    entra %s" % email)
            sys.stdout.flush()
            b = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            seguro = "; Secure" if _base_url(self).startswith("https") else ""
            self.send_header("Set-Cookie", "dyn_sesion=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s"
                             % (tok, VIDA_SESION, seguro))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return

        if ruta == "/stripe/webhook":
            try:
                n = int(self.headers.get("Content-Length", 0))
                cuerpo = self.rfile.read(n)
            except Exception:                            # noqa: BLE001
                return self._j(400, {"error": "Cuerpo no valido"})
            firma = self.headers.get("Stripe-Signature", "")
            if not _verifica_firma_stripe(cuerpo, firma, STRIPE["webhook"]):
                return self._j(400, {"error": "Firma no valida"})
            try:
                evento = json.loads(cuerpo)
            except Exception:                            # noqa: BLE001
                return self._j(400, {"error": "JSON no valido"})
            _procesa_evento_stripe(evento)
            return self._j(200, {"ok": True})

        if ruta == "/api/traspaso":
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 4_000_000:
                    return self._j(413, {"error": "Demasiados datos"})
                d = json.loads(self.rfile.read(n) or b"{}")
            except Exception:                            # noqa: BLE001
                return self._j(400, {"error": "Cuerpo no valido"})
            with _lock:
                TRASPASO["datos"] = d
                TRASPASO["ts"] = time.time()
            print("  traspaso  %d claves recibidas del origen local" % len(d or {}))
            return self._j(200, {"ok": True})
        if ruta == "/api/traducir":
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
            except Exception:                            # noqa: BLE001
                return self._j(400, {"error": "Cuerpo no valido"})
            textos = (d.get("textos") or [])[:60]
            destino = d.get("destino") or "es"
            res = {}
            pend = queue.Queue()
            for i, t in enumerate(textos):
                pend.put((i, t))

            # Seis obreros: lanzar sesenta peticiones a la vez hacia el mismo
            # servicio provoca limitacion y devuelve cadenas vacias.
            def obrero():
                while True:
                    try:
                        i, t = pend.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        res[i] = traducir(t, destino)
                    except Exception:                    # noqa: BLE001
                        res[i] = ""
            hilos = [threading.Thread(target=obrero) for _ in range(min(6, max(1, len(textos))))]
            for h in hilos:
                h.start()
            for h in hilos:
                h.join(timeout=40)
            hechas = sum(1 for v in res.values() if v)
            return self._j(200, {"trad": [res.get(i, "") for i in range(len(textos))],
                                 "traducidas": hechas, "total": len(textos)})

        if ruta == "/api/ia/clave":
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
            except Exception:                            # noqa: BLE001
                return self._j(400, {"error": "Cuerpo no valido"})
            with _LOCK_IA:
                IA["proveedor"] = (d.get("proveedor") or "anthropic").lower()
                IA["clave"] = d.get("clave") or ""
                IA["modelo"] = (d.get("modelo") or "").strip()
            print("  ia        credenciales de %s cargadas en memoria" % IA["proveedor"])
            sys.stdout.flush()
            return self._j(200, {"ok": True, "proveedor": IA["proveedor"],
                                 "modelo": IA["modelo"] or MODELO_POR_DEFECTO.get(IA["proveedor"], "")})

        if ruta.startswith("/ea/v1/"):
            codigo = (self.headers.get("X-Journal-Codigo") or "").strip()
            alumno = CODIGOS_EA.get(codigo)
            if not alumno:
                print("  journal   RECHAZADO · codigo de vinculacion no valido")
                sys.stdout.flush()
                return self._j(403, {"error": "codigo no valido"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
            except Exception:                            # noqa: BLE001
                return self._j(400, {"error": "cuerpo no valido"})

            with _LOCK_J:
                base = journal_carga()

                if ruta == "/ea/v1/cuenta":
                    cta = str(d.get("cuenta"))
                    ficha = dict(d)
                    ficha["alumno"] = alumno
                    ficha["visto"] = int(time.time() * 1000)
                    base["cuentas"][cta] = ficha
                    journal_guarda(base)
                    print("  journal   cuenta %s · %s · %s %s · UTC%+d · %s abiertas"
                          % (cta, d.get("broker", "?"), d.get("saldo", "?"),
                             d.get("divisa", ""), d.get("desfase_gmt") or 0,
                             d.get("abiertas", "?")))
                    sys.stdout.flush()
                    return self._j(200, {"ok": True})

                if ruta == "/ea/v1/operaciones":
                    cta = str(d.get("cuenta"))
                    guardadas = base["operaciones"].setdefault(cta, {})
                    nuevas = repetidas = malas = 0
                    for op in (d.get("operaciones") or []):
                        ok, _m = valida_op(op)
                        if not ok:
                            malas += 1
                            continue
                        k = str(op.get("idExterno") or op.get("ticket"))
                        if k in guardadas:
                            repetidas += 1
                            continue
                        guardadas[k] = {c: op.get(c) for c in CAMPOS_OP}
                        nuevas += 1
                    if cta in base["cuentas"] and d.get("saldo") is not None:
                        base["cuentas"][cta]["saldo"] = d.get("saldo")
                    journal_guarda(base)
                    total = len(guardadas)
                    print("  journal   %s · %d nuevas · %d repetidas · %d rechazadas · %d en total"
                          % (cta, nuevas, repetidas, malas, total))
                    sys.stdout.flush()
                    publicar("journal", {"cuenta": cta, "nuevas": nuevas, "total": total})
                    return self._j(200, {"ok": True, "nuevas": nuevas,
                                         "repetidas": repetidas, "rechazadas": malas})

            return self._j(404, {"error": "ruta desconocida"})

        if ruta == "/api/soporte":
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
            except Exception:                            # noqa: BLE001
                return self._j(400, {"error": "cuerpo no valido"})
            titulo = (d.get("titulo") or "").strip()
            detalle = (d.get("detalle") or "").strip()
            if len(titulo) < 5:
                return self._j(400, {"error": "Resume el problema en una frase."})
            if len(detalle) < 15:
                return self._j(400, {"error": "Cuenta un poco más: qué hacías y qué esperabas."})

            ses = sesion_de(self)
            uid = (ses or {}).get("id") or "local"
            # Un aviso cada 30 s por alumno: evita inundar el canal sin querer.
            ahora = time.time()
            if ahora - _ULTIMO_TK.get(uid, 0) < 30:
                return self._j(429, {"error": "Espera unos segundos antes de enviar otro."})

            t = {"id": base64.urlsafe_b64encode(os.urandom(5)).decode().rstrip("=").upper(),
                 "t": int(ahora * 1000), "uid": uid,
                 "usuario": (ses or {}).get("nombre") or "uso local",
                 "tipo": (d.get("tipo") or "otro"),
                 "titulo": titulo[:200], "detalle": detalle[:3500],
                 "vista": (d.get("vista") or "")[:40], "par": (d.get("par") or "")[:12],
                 "contacto": (d.get("contacto") or "")[:120],
                 "navegador": (self.headers.get("User-Agent") or "")[:200],
                 "pantalla": (d.get("pantalla") or "")[:20]}
            ok, motivo = manda_ticket(t)
            t["enviado"] = ok
            tickets_guarda(t)
            _ULTIMO_TK[uid] = ahora
            print("  soporte   ticket %s · %s · %s%s"
                  % (t["id"], t["usuario"], t["titulo"][:48],
                     "" if ok else " · NO SE PUDO ENVIAR: " + motivo))
            sys.stdout.flush()
            if not ok:
                # Aunque falle el envio, queda guardado: no se pierde nada.
                return self._j(200, {"ok": True, "id": t["id"], "entregado": False,
                                     "aviso": "Guardado, pero no se pudo avisar en Discord: "
                                              + motivo})
            return self._j(200, {"ok": True, "id": t["id"], "entregado": True})

        if ruta == "/api/marcadores":
            ses = sesion_de(self)
            uid = (ses or {}).get("id") or ("local" if not discord_activo() else "")
            if not uid:
                return self._j(401, {"error": "hace falta entrar para guardar marcadores"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
            except Exception:                            # noqa: BLE001
                return self._j(400, {"error": "cuerpo no valido"})
            titulo = (d.get("title") or "").strip()
            if not titulo:
                return self._j(400, {"error": "falta el titular"})
            k = marc_clave(titulo)
            with _LOCK_MARC:
                base = marc_carga()
                mios = base.setdefault(uid, {})
                if d.get("quitar") or (k in mios and d.get("alternar")):
                    mios.pop(k, None)
                    accion = "quitado"
                else:
                    mios[k] = {"k": k, "title": titulo[:400],
                               "link": (d.get("link") or "")[:600],
                               "src": (d.get("src") or "")[:80],
                               "imp": int(d.get("imp") or 0),
                               "div": [str(x)[:6] for x in (d.get("div") or [])][:5],
                               "ts": int(d.get("ts") or 0),
                               "nota": (d.get("nota") or "")[:600],
                               "t": int(time.time() * 1000)}
                    accion = "guardado"
                    if len(mios) > MARC_TOPE:
                        viejos = sorted(mios.values(), key=lambda x: x.get("t") or 0)
                        for v in viejos[:len(mios) - MARC_TOPE]:
                            mios.pop(v["k"], None)
                marc_guarda(base)
                total = len(mios)
            return self._j(200, {"ok": True, "accion": accion, "total": total,
                                 "guardado": accion == "guardado"})

        if ruta == "/api/memoria/nota":
            # Permite corregir o borrar una nota a mano: el manual es de la
            # academia, no de la maquina.
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
            except Exception:                            # noqa: BLE001
                return self._j(400, {"error": "cuerpo no valido"})
            tema = (d.get("tema") or "").strip()
            if not tema:
                return self._j(400, {"error": "falta el tema"})
            nota = (d.get("nota") or "").strip()
            with _LOCK_MEM:
                base = memoria_carga()
                base["manual"] = [x for x in base["manual"] if x.get("tema") != tema]
                if nota:
                    base["manual"].append({"tema": tema, "nota": nota[:600],
                                           "t": int(time.time()),
                                           "n": (base["temas"].get(tema) or {}).get("n", 0),
                                           "manual_humano": True})
                memoria_guarda(base)
            print("  memoria   nota de '%s' %s a mano" % (tema, "guardada" if nota else "borrada"))
            sys.stdout.flush()
            return self._j(200, {"ok": True})

        if ruta == "/api/ia/probar":
            try:
                self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            except Exception:                            # noqa: BLE001
                pass
            d = diagnostico_ia()
            print("  ia        prueba: %s · %s" % (d["estado"], d["msg"]))
            sys.stdout.flush()
            return self._j(200, d)

        if ruta == "/api/analisis":
            # Analisis compartido: el cliente pide una clave, nunca un prompt.
            # El contexto y las instrucciones los pone el servidor, asi que
            # todos los alumnos reciben exactamente lo mismo.
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
            except Exception:                            # noqa: BLE001
                return self._j(400, {"error": "Cuerpo no valido"})
            clave = (d.get("clave") or "").strip()
            if not clave:
                return self._j(400, {"error": "Falta la clave"})
            try:
                r = analisis_compartido(clave, forzar=bool(d.get("forzar")))
                return self._j(200, {"texto": r["texto"], "t": int(r["t"] * 1000),
                                     "modelo": r.get("modelo", ""), "clave": clave,
                                     "fuentes": r.get("fuentes", []),
                                     "j": r.get("j"), "tit": r.get("tit"),
                                     "ev": r.get("ev"), "sym": r.get("sym"),
                                     "fondo": r.get("fondo"),
                                     "compartido": True, "cache": bool(r.get("cache"))})
            except Exception as e:                       # noqa: BLE001
                return self._j(500, {"error": str(e)[:200]})

        if ruta == "/api/ia":
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
            except Exception:                            # noqa: BLE001
                return self._j(400, {"error": "Cuerpo no valido"})
            sistema = d.get("sistema") or "Eres un analista de mercados de criptoactivos."
            # El chat es por alumno: si nadie pide un modelo concreto, el ligero.
            if not (d.get("modelo") or "").strip():
                d["modelo"] = MODELOS["chat"]
            # Misma politica que el resto: la herramienta esta ahi, la usa si
            # le falta algo. No se fuerza una busqueda en cada pregunta.
            if d.get("buscar"):
                sistema += POLITICA_BUSQUEDA
                d.setdefault("busquedas", 4)
            # Memoria: se apunta la pregunta y se le pasa lo ya destilado.
            ultima = ""
            for m in reversed(mensajes_previos(d)):
                if m.get("role") == "user":
                    ultima = str(m.get("content") or "")
                    break
            if ultima:
                ses = sesion_de(self)
                memoria_registra(ultima[-600:], (ses or {}).get("id", ""))
                sistema += memoria_contexto(ultima)
            mensajes = d.get("mensajes") or []
            if not mensajes:
                return self._j(400, {"error": "Sin mensajes"})
            try:
                return self._j(200, llamar_ia(sistema, mensajes, int(d.get("maxtok") or 1100),
                                              bool(d.get("buscar")), (d.get("modelo") or "").strip(),
                                              int(d.get("busquedas") or 6)))
            except RuntimeError as e:
                return self._j(400, {"error": str(e)})
            except HTTPError as e:
                cuerpo = e.read().decode("utf-8", "ignore")[:220]
                return self._j(400, {"error": "El proveedor respondio %s: %s" % (e.code, cuerpo)})
            except Exception as e:                       # noqa: BLE001
                return self._j(502, {"error": str(e)})

        if ruta != "/api/exchange":
            return self._t(404, "No encontrado")
        try:
            n = int(self.headers.get("Content-Length", 0))
            c = json.loads(self.rfile.read(n) or b"{}")
        except Exception:                                # noqa: BLE001
            return self._j(400, {"error": "Cuerpo no valido"})
        ex = (c.get("exchange") or "").lower()
        key, sec = c.get("key") or "", c.get("secret") or ""
        with _lock_claves:
            if not key and ex in CLAVES:
                key, sec = CLAVES[ex]
            elif c.get("recordar") and key:
                CLAVES[ex] = (key, sec)
        if not key or not sec:
            return self._j(400, {"error": "Faltan las credenciales"})
        try:
            if ex == "binance":
                ops, av = binance_trades(key, sec, (c.get("simbolos") or ["BTC", "ETH"])[:40])
            elif ex == "kraken":
                ops, av = kraken_trades(key, sec)
            else:
                return self._j(400, {"error": "Exchange no soportado: " + ex})
            ops.sort(key=lambda o: o["date"])
            print("  exchange  %s -> %d operaciones" % (ex, len(ops)))
            return self._j(200, {"ops": ops, "avisos": av, "exchange": ex})
        except RuntimeError as e:
            return self._j(400, {"error": str(e)})
        except Exception as e:                           # noqa: BLE001
            return self._j(502, {"error": "%s" % e})

    def do_GET(self):
        ruta = urlparse(self.path)

        # La puerta. Sin Discord ni Stripe configurados, y sin ninguna
        # cuenta creada todavia, no hay puerta: el uso local de siempre
        # sigue funcionando igual.
        ses = sesion_de(self)
        if login_activo() and not ruta.path.startswith("/ea/v1/") \
                and ruta.path not in RUTAS_LIBRES and not ses:
            if ruta.path.startswith("/api/"):
                return self._j(401, {"error": "sesion no valida", "login": "/login"})
            return self._redir("/login")

        # El muro de pago. Solo afecta a las cuentas propias (no a Discord,
        # que sigue su logica de siempre): con sesion pero sin suscripcion
        # activa, todo el sitio menos /pago y logout queda bloqueado.
        if ses and _requiere_pago(ses) and ruta.path not in RUTAS_LIBRES:
            if ruta.path.startswith("/api/"):
                return self._j(402, {"error": "sin suscripcion activa", "pago": "/pago"})
            return self._redir("/pago")

        # ---------------- ACCESO ----------------
        if ruta.path == "/login" and not discord_activo():
            return self._html(PAGINA_CUENTA)

        if ruta.path == "/pago":
            if not ses:
                return self._redir("/login")
            enlace = STRIPE["enlace"] + (("&" if "?" in STRIPE["enlace"] else "?")
                                          + "client_reference_id=" + ses.get("id", ""))
            return self._html(PAGINA_PAGO.replace("__ENLACE__", enlace))

        if ruta.path == "/pago/completado":
            return self._redir("/")

        if ruta.path == "/login":
            err = (parse_qs(ruta.query).get("e") or [""])[0]
            MENS = {"nomiembro": "Tu cuenta de Discord no esta en el servidor de la "
                                 "academia. Pide la invitacion y vuelve a intentarlo.",
                    "norol": "Estas en el servidor pero no tienes el rol de alumno "
                             "asignado. Avisa por Discord y te lo damos.",
                    "estado": "La sesion de entrada caduco. Vuelve a pulsar el boton.",
                    "fallo": "Discord no completo la identificacion. Intentalo de nuevo."}
            html = PAGINA_LOGIN.replace("__ERROR__",
                   ('<div class="e">%s</div>' % MENS.get(err, "No se pudo entrar."))
                   if err else "")
            return self._html(html)

        if ruta.path == "/auth/discord":
            if not discord_activo():
                return self._redir("/")
            est = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
            ESTADOS_OAUTH[est] = time.time() + 600
            for k in [k for k, v in ESTADOS_OAUTH.items() if v < time.time()]:
                ESTADOS_OAUTH.pop(k, None)
            destino = "https://discord.com/api/oauth2/authorize?" + urllib.parse.urlencode({
                "client_id": DISCORD["id"], "response_type": "code",
                "redirect_uri": _base_url(self) + "/auth/discord/callback",
                "scope": "identify guilds.members.read", "state": est, "prompt": "none"})
            return self._redir(destino)

        if ruta.path == "/auth/discord/callback":
            q = parse_qs(ruta.query)
            codigo = (q.get("code") or [""])[0]
            est = (q.get("state") or [""])[0]
            if not codigo or est not in ESTADOS_OAUTH:
                return self._redir("/login?e=estado")
            ESTADOS_OAUTH.pop(est, None)
            try:
                tk = discord_intercambia(codigo, _base_url(self) + "/auth/discord/callback")
                acceso = tk.get("access_token")
                if not acceso:
                    return self._redir("/login?e=fallo")
                yo = discord_yo(acceso)
                miembro = discord_miembro(acceso)
            except Exception as e:                       # noqa: BLE001
                print("  acceso    fallo de Discord: %s" % str(e)[:120])
                sys.stdout.flush()
                return self._redir("/login?e=fallo")

            if miembro is None:
                print("  acceso    RECHAZADO · %s no esta en el servidor" % yo.get("username"))
                sys.stdout.flush()
                return self._redir("/login?e=nomiembro")

            roles = miembro.get("roles") or []
            if DISCORD["roles"] and not set(roles) & set(DISCORD["roles"]):
                print("  acceso    RECHAZADO · %s sin rol de alumno" % yo.get("username"))
                sys.stdout.flush()
                return self._redir("/login?e=norol")

            ficha = {"id": yo.get("id"), "usuario": yo.get("username"),
                     "nombre": miembro.get("nick") or yo.get("global_name") or yo.get("username"),
                     "roles": roles, "avatar": yo.get("avatar")}
            tok = nueva_sesion(ficha)
            print("  acceso    ENTRA %s (%s)" % (ficha["nombre"], ficha["id"]))
            sys.stdout.flush()
            self.send_response(302)
            seguro = "; Secure" if _base_url(self).startswith("https") else ""
            self.send_header("Set-Cookie",
                             "dyn_sesion=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s"
                             % (tok, VIDA_SESION, seguro))
            self.send_header("Location", "/forex")
            self.end_headers()
            return

        if ruta.path == "/logout":
            ses = sesion_de(self)
            if ses:
                with _LOCK_SES:
                    for k, v in list(SESIONES.items()):
                        if v is ses:
                            SESIONES.pop(k, None)
            self.send_response(302)
            self.send_header("Set-Cookie", "dyn_sesion=; Path=/; Max-Age=0")
            self.send_header("Location", "/login")
            self.end_headers()
            return

        if ruta.path == "/api/yo":
            ses = sesion_de(self)
            return self._j(200, {"entrado": bool(ses) or not discord_activo(),
                                 "puerta": discord_activo(),
                                 "usuario": (ses or {}).get("nombre", ""),
                                 "id": (ses or {}).get("id", "")})


        if ruta.path == "/api/ping":
            with _lock:
                d = dict(ESTADO["diag"])
            with _LOCK_IA:
                ia = {"configurada": bool(IA["clave"]), "proveedor": IA["proveedor"],
                      "modelo": IA["modelo"] or MODELO_POR_DEFECTO.get(IA["proveedor"], "")}
            return self._j(200, {"ok": True, "servidor": "crypto-ops", "version": 6,
                                 "tiempo_real": True, "diag": d, "ia": ia,
                                 "rutas": ["/", "/forex"],
                                 "forex": os.path.exists(os.path.join(RAIZ, PAGINA_FX)),
                                 "activo_desde": ESTADO["arranque"]})

        if ruta.path == "/api/traspaso":
            # Los datos del navegador van ligados al origen: lo guardado
            # abriendo el archivo suelto no se ve al entrar por localhost.
            # Este buzon temporal los traslada al redirigir.
            with _lock:
                d = TRASPASO.get("datos")
                if d and time.time() - TRASPASO.get("ts", 0) < 600:
                    return self._j(200, {"hay": True, "datos": d})
            return self._j(200, {"hay": False})

        if ruta.path == "/api/acceso":
            return self._j(200, {"activo": discord_activo(), "avisos": revisa_discord(),
                                 "guild": DISCORD["guild"], "roles": DISCORD["roles"],
                                 "dominio": DISCORD["dominio"],
                                 "retorno": (DISCORD["dominio"] or _base_url(self))
                                            + "/auth/discord/callback",
                                 "sesiones": len(SESIONES)})

        if ruta.path == "/api/marcadores":
            ses = sesion_de(self)
            uid = (ses or {}).get("id") or ("local" if not discord_activo() else "")
            if not uid:
                return self._j(200, {"sesion": False, "items": []})
            with _LOCK_MARC:
                d = marc_carga()
            items = sorted((d.get(uid) or {}).values(), key=lambda x: -(x.get("t") or 0))
            return self._j(200, {"sesion": True, "items": items})

        if ruta.path == "/api/memoria":
            with _LOCK_MEM:
                d = memoria_carga()
            temas = sorted(({"tema": k, "n": v["n"], "ultima": v["ultima"]}
                            for k, v in d["temas"].items()), key=lambda x: -x["n"])
            recientes = [{"t": x["t"], "tema": x["tema"], "q": x["q"]}
                         for x in d["preguntas"][-40:]][::-1]
            return self._j(200, {"temas": temas, "manual": d["manual"],
                                 "total": len(d["preguntas"]), "recientes": recientes})

        if ruta.path == "/api/journal":
            with _LOCK_J:
                base = journal_carga()
            cta = (parse_qs(ruta.query).get("cuenta") or [""])[0]
            cuentas = base["cuentas"]
            if cta and cta in base["operaciones"]:
                ops = list(base["operaciones"][cta].values())
            else:
                ops = [x for m in base["operaciones"].values() for x in m.values()]
                cta = ""
            return self._j(200, {"cuentas": cuentas, "cuenta": cta,
                                 "operaciones": ops,
                                 "totales": {k: len(v) for k, v in base["operaciones"].items()}})

        if ruta.path == "/api/snapshot":
            with _lock:
                return self._j(200, {"noticias": ESTADO["noticias"][:400],
                                     "listados": ESTADO["listados"],
                                     "calendario": ESTADO["calendario"],
                                     "tesorerias": ESTADO["tesorerias"],
                                     "ballenas": ESTADO.get("ballenas", []),
                                     "cotiz": ESTADO.get("cotiz", {}),
                                     "fx": ESTADO.get("fx", [])[:400],
                                     "trump": ESTADO.get("trump", [])[:200],
                                     "macro": ESTADO.get("macro", {}),
                                     "analisis": {k: {"t": int(v["t"] * 1000),
                                                      "texto": v["texto"],
                                                      "modelo": v.get("modelo", ""),
                                                      "j": v.get("j"), "tit": v.get("tit"),
                                                      "ev": v.get("ev"), "sym": v.get("sym"),
                                                      "fondo": v.get("fondo")}
                                                  for k, v in ANALISIS.items()},
                                     "diag": ESTADO["diag"]})

        if ruta.path == "/api/stream":
            q = queue.Queue(maxsize=200)
            with _lock_subs:
                SUSCRIPTORES.append(q)
            self._cab(200, "text/event-stream; charset=utf-8",
                      extra={"Connection": "keep-alive", "X-Accel-Buffering": "no"})
            try:
                self.wfile.write(b"event: hola\ndata: {\"ok\":true}\n\n")
                self.wfile.flush()
                while not PARAR.is_set():
                    try:
                        msg = q.get(timeout=20)
                    except queue.Empty:
                        msg = ": latido\n\n"
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
            except Exception:                            # noqa: BLE001
                pass
            finally:
                with _lock_subs:
                    if q in SUSCRIPTORES:
                        SUSCRIPTORES.remove(q)
            return

        if ruta.path == "/api/live":
            cid = parse_qs(ruta.query).get("ch", [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{10,40}", cid or ""):
                return self._j(400, {"error": "Canal no valido"})
            return self._j(200, canal_en_directo(cid))

        if ruta.path == "/proxy":
            destino = parse_qs(ruta.query).get("url", [""])[0]
            if not destino:
                return self._t(400, "Falta el parametro url")
            if not destino.startswith(("http://", "https://")):
                return self._t(400, "Esquema no admitido")
            if not permitido(destino):
                return self._t(403, "Dominio fuera de la lista permitida")
            try:
                datos, _ = traer(destino)
                self._cab(200, "application/xml; charset=utf-8", len(datos))
                self.wfile.write(datos)
            except HTTPError as e:
                self._t(502, "La fuente respondio %s" % e.code)
            except (URLError, TimeoutError) as e:
                self._t(504, "Sin respuesta: %s" % e)
            except Exception as e:                       # noqa: BLE001
                self._t(500, "Error interno: %s" % e)
            return

        # Se aceptan todas las formas razonables de pedir cada terminal.
        limpio = ruta.path.rstrip("/").lower() or "/"
        if limpio in ("/", ""):
            self.path = "/" + PAGINA
        elif limpio in ("/forex", "/fx", "/divisas", "/forex.html"):
            self.path = "/" + PAGINA_FX
        elif limpio in ("/crypto", "/cripto"):
            self.path = "/" + PAGINA

        # Si se pide un terminal que no está en la carpeta, se explica en vez
        # de devolver un 404 seco de servidor de archivos.
        for archivo, nombre in ((PAGINA_FX, "divisas"), (PAGINA, "criptoactivos")):
            if self.path == "/" + archivo and not os.path.exists(os.path.join(RAIZ, archivo)):
                b = ("<meta charset='utf-8'><body style='background:#0a0b0d;color:#eceff3;"
                     "font:14px/1.7 -apple-system,BlinkMacSystemFont,sans-serif;padding:48px'>"
                     "<h2 style='font-weight:600'>Falta el archivo del terminal de %s</h2>"
                     "<p style='color:#98a0ab'>El servidor busca <b style='color:#eceff3'>%s</b> en:<br>"
                     "<code style='color:#98a0ab'>%s</code></p>"
                     "<p style='color:#98a0ab'>Copia ese archivo a esa misma carpeta y recarga.</p>"
                     "<p><a href='/' style='color:#5b8def'>Volver al terminal de criptoactivos</a></p>"
                     "</body>" % (nombre, archivo, RAIZ)).encode("utf-8")
                self._cab(404, "text/html; charset=utf-8", len(b))
                self.wfile.write(b)
                return
        return super().do_GET()

    def guess_type(self, path):
        # Sin charset explicito el navegador puede interpretar mal los acentos.
        t = super().guess_type(path)
        if t in ("text/html", "text/plain", "text/css", "application/javascript", "text/javascript"):
            return t + "; charset=utf-8"
        return t

    def end_headers(self):
        if not (self.path or "").startswith(("/proxy", "/api")):
            self.send_header("Access-Control-Allow-Origin", "*")
            # Sin esto el navegador puede seguir sirviendo una version anterior
            # del panel desde su cache y parecer que nada funciona.
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()


def main():
    en_local = HOST == "127.0.0.1"
    if not os.path.exists(os.path.join(RAIZ, PAGINA)):
        print("\n  No encuentro %s en esta carpeta.\n" % PAGINA)
        if en_local:
            input("  Pulsa Intro para cerrar...")
        return
    try:
        srv = ThreadingHTTPServer((HOST, PUERTO), Handler)
    except OSError:
        print("\n  El puerto %d ya esta ocupado: el panel ya debe estar abierto.\n" % PUERTO)
        if en_local:
            webbrowser.open("http://127.0.0.1:%d/" % PUERTO)
            input("  Pulsa Intro para cerrar...")
        return

    srv.daemon_threads = True
    base = "http://127.0.0.1:%d" % PUERTO
    # El lanzador de divisas fija esta variable para que se abra su terminal.
    url = base + ("/forex" if os.environ.get("CRYPTO_OPS_ABRIR") == "forex" else "/")
    print("\n" + "=" * 62)
    print("  OPS TERMINAL  ·  tiempo real")
    print("=" * 62)
    print("  Criptoactivos:  %s/" % base)
    print("  Divisas:        %s/forex" % base)
    print("  Se abre ahora:  %s" % url)
    print("  Cripto:     %d fuentes · sondeo continuo" % len(FUENTES))
    print("  Forex:      %d fuentes · %d de seguimiento presidencial" % (len(FUENTES_FX), len(FUENTES_TRUMP)))
    print("  Listados:   cada 60 s")
    print("  Ballenas:   cada 20 s · umbral %g BTC" % UMBRAL_BALLENA)
    print("  Cotizacion: %d instrumentos cada 25 s · Stooq + Yahoo" % len(PARES_FX))
    print("  Calendario: Trading Economics (EE. UU.) + ForexFactory")
    if cargar_clave():
        print("  Asistente:  clave cargada · %s · %s"
              % (IA["proveedor"], IA["modelo"] or MODELO_POR_DEFECTO[IA["proveedor"]]))
        # Se comprueba de verdad, en segundo plano: tener clave no es tener
        # saldo, y una clave revocada tambien "esta cargada".
        def _probar():
            d = diagnostico_ia()
            print("\n  Asistente:  %s" % d["msg"])
            if d.get("pista"):
                print("              %s" % d["pista"])
            if d.get("detalle"):
                print("              respuesta de la API: %s" % d["detalle"][:160])
            print("")
            sys.stdout.flush()
            with _lock:
                ESTADO["diag"]["ia"] = d["msg"]
        threading.Thread(target=_probar, daemon=True).start()
    else:
        print("  Asistente:  SIN CLAVE · crea clave.txt junto a este archivo")
        print("              formato: una linea con  anthropic sk-ant-...")

    if SOPORTE["webhook"]:
        print("  Soporte:    los tickets van al canal de Discord por webhook")
    else:
        print("  Soporte:    SIN CONFIGURAR · anade en clave.txt una linea:")
        print("              tickets https://discord.com/api/webhooks/...")

    if stripe_activo():
        print("  Pago:       Stripe · 30E/mes · cuentas propias (sin Discord)")
        print("              webhook: %s/stripe/webhook" % (DISCORD["dominio"] or retorno_local().replace("/auth/discord/callback", "")))
    else:
        print("  Pago:       SIN CONFIGURAR · anade en clave.txt:")
        print("              stripe_enlace https://buy.stripe.com/...")
        print("              stripe_webhook whsec_...")
    avisos = revisa_discord()
    if discord_activo():
        print("  Acceso:     Discord · solo miembros del servidor %s" % DISCORD["guild"])
        print("              roles exigidos: %s"
              % (", ".join(DISCORD["roles"]) if DISCORD["roles"] else "ninguno, basta pertenecer"))
        print("              retorno que debe estar en Discord:")
        print("              %s" % (DISCORD["dominio"] + "/auth/discord/callback"
                                    if DISCORD["dominio"] else retorno_local()))
        if not DISCORD["dominio"]:
            print("              (copialo TAL CUAL en OAuth2 > Redirects: Discord lo")
            print("               compara como texto, y localhost no vale por 127.0.0.1)")
    else:
        print("  Acceso:     ABIERTO · sin Discord configurado, el terminal no pide entrar")
    for a in avisos:
        print("")
        print("  !! AVISO DE CONFIGURACION")
        for linea in _troceado(a, 70):
            print("     %s" % linea)
    if avisos:
        print("")
    if en_local:
        print("  Escucha:    solo 127.0.0.1 · la clave nunca sale hacia el panel")
    else:
        print("  Escucha:    %s:%d · modo remoto (HOST=%s)" % (HOST, PUERTO, HOST))
    print("-" * 62)
    print("  Deja esta ventana abierta. Para cerrar: Control+C\n")

    for f in (bucle_noticias, bucle_listados, bucle_calendario, bucle_tesorerias, bucle_ballenas,
              bucle_fx, bucle_trump, bucle_cotizaciones, bucle_comparativa, bucle_analisis, bucle_memoria):
        threading.Thread(target=f, daemon=True).start()
    if en_local:
        def abrir_cuando_listo():
            import urllib.request as _u
            for _ in range(40):
                try:
                    _u.urlopen(base + "/api/ping", timeout=1).read()
                    break
                except Exception:
                    time.sleep(0.25)
            webbrowser.open(url)
        threading.Thread(target=abrir_cuando_listo, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        PARAR.set()
        with _lock_claves:
            CLAVES.clear()
        print("\n  Servidor detenido. Claves borradas de memoria.\n")
        srv.shutdown()


if __name__ == "__main__":
    main()
