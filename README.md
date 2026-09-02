# Dynastia Analyst

Terminal de análisis de mercado para los alumnos de la academia. Dos vistas
—divisas y cripto—, sesgo diario generado con IA, comparativa de economías,
calendario macro, noticias clasificadas por impacto, y un Journal que lee las
operaciones reales del alumno desde MetaTrader 5.

Hoy funciona en local. El objetivo es publicarlo en `analyst.dynastia.es`
detrás de acceso con Discord y suscripción de pago.

---

## Cómo arrancarlo

Requiere **Python 3.10 o superior**. Nada más: el servidor usa solo biblioteca
estándar, sin `pip install`, sin `requirements.txt`, sin entorno virtual.

```bash
cp clave.ejemplo.txt clave.txt     # y rellena los valores
python3 servidor.py
```

Abre `http://127.0.0.1:8787` para cripto y `http://127.0.0.1:8787/forex` para
divisas.

Sin `clave.txt` el terminal arranca igual, pero sin las funciones de IA. Sin la
línea `discord`, arranca sin login: es el modo cómodo para desarrollar.

---

## Estructura

```
servidor.py                  Todo el backend: datos, IA, sesiones, endpoints
forex-ops-center.html        Terminal de divisas (Dynastia Analyst)
crypto-ops-center.html       Terminal de cripto
clave.ejemplo.txt            Plantilla de configuración
journal/
  JournalAcademia.mq5        Expert Advisor de MetaTrader 5
  metricas.js                Motor de métricas del Journal (91 pruebas)
  pruebas-metricas.js        Pruebas del motor
  pruebas-metricas-avanzadas.js
  receptor-ea.py             Servidor de pruebas del EA, independiente
  INSTALAR-EN-MT5.txt        Guía para el alumno
docs/
  CONFIGURAR-DISCORD-Y-DOMINIO.md
  plan-plataforma.md         Plan técnico y decisiones abiertas
lanzadores/                  Atajos de macOS
```

**Los HTML tienen que estar junto a `servidor.py`.** El servidor resuelve todas
las rutas respecto a su propia carpeta (`RAIZ`), así que moverlos a un `web/`
rompe el arranque.

---

## Arquitectura

Servidor Python de biblioteca estándar (`ThreadingHTTPServer`) que hace tres
cosas: sondear fuentes de datos, hablar con la API de Anthropic, y servir dos
archivos HTML autocontenidos. Cada terminal es **un solo archivo**: HTML, CSS y
JavaScript juntos, sin compilación ni dependencias de front.

### Análisis compartido, la decisión que sostiene el margen

El sesgo y las comparativas **se calculan una vez en el servidor y se sirven a
todos los alumnos**, no una vez por alumno. Hay bloqueo de vuelo único: si
treinta alumnos piden el mismo análisis a la vez, se hace una sola llamada a la
API y los treinta reciben el mismo texto.

Esto hace que el coste de IA sea casi independiente del número de alumnos.
Lo único que escala con usuarios es el chat.

```
ANALISIS = {}                      caché por clave
_VUELOS  = {}                      cálculos en curso
analisis_compartido(clave)         un solo vuelo por clave
_receta(clave)                     el prompt vive en el servidor
```

**El prompt nunca viaja al cliente.** El navegador manda solo una clave
(`sesgo:EURUSD`) y el servidor decide qué se pregunta y con qué modelo.

### Reparto de modelos

| Tarea | Modelo | Por qué |
|---|---|---|
| Sesgo | Opus | Es el producto. Tiene que ser preciso |
| Comparativa, titulares, journal | Sonnet | Volumen medio, calidad suficiente |
| Chat de Dyn | Haiku | Muchas peticiones, respuestas cortas |

El bucle de llamada gestiona `pause_turn`, `tool_use` y `max_tokens`, y detecta
respuestas que se quedan en el preámbulo («voy a contrastar…») para continuar
en vez de devolver una respuesta vacía.

### Endpoints

| Ruta | Para qué |
|---|---|
| `/api/stream` | Server-Sent Events: precios y titulares en vivo |
| `/api/analisis` | Análisis compartido por clave |
| `/api/ia` | Chat de Dyn, por alumno |
| `/api/journal` | Lectura de operaciones del alumno |
| `/api/marcadores` | Guardados del alumno |
| `/api/soporte` | Tickets al canal de Discord |
| `/auth/discord`, `/auth/discord/callback` | Login |
| `/ea/v1/cuenta`, `/ea/v1/operaciones` | Recepción desde MetaTrader 5 |

### El Expert Advisor

El alumno instala `JournalAcademia.mq5` en su MetaTrader 5 y pega un código de
vinculación. **Solo envía; no contiene ni una llamada a `OrderSend`.** No pide
contraseñas y no puede operar. Manda las posiciones cerradas en tandas de 40,
con hora UTC y hora del bróker más el desfase GMT, distancia del stop en pips,
riesgo en dinero y en porcentaje, resultado en R y motivo de cierre.

### Pruebas

```bash
node journal/pruebas-metricas.js
node journal/pruebas-metricas-avanzadas.js
```

91 pruebas sobre el motor de métricas. `metricas.js` es puro: sin DOM, sin red.

---

## Seguridad

- **`clave.txt` no se sube nunca.** Está en `.gitignore`. Contiene la clave de
  Anthropic, el secreto de Discord y el webhook de tickets.
- Los archivos `.json` de la raíz son **datos de alumnos**: preguntas, historial
  de operaciones, marcadores. Son datos personales. No se versionan y no salen
  del servidor.
- La memoria del asistente guarda un **hash SHA-256 truncado** del ID de
  Discord, nunca el nombre.
- El EA usa contraseña de solo lectura. Nunca la maestra.
- La IA no ejecuta órdenes ni mueve fondos, por diseño.

---

## Estado

Funciona en local y está probado a diario. **No está desplegado.** Lo que falta
antes de abrirlo a alumnos está en `ENTREGA.md`, con los riesgos ordenados por
gravedad.
