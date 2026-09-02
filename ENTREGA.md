# Nota de entrega

Para el programador que recoge el proyecto. Estado real, sin adornos: qué
funciona, qué está a medias y qué hay que resolver antes de cobrar a nadie.

---

## 1. Lo primero, y no es negociable

**Las credenciales que se usaron durante el desarrollo están comprometidas** y
hay que rotarlas antes de desplegar:

| Credencial | Dónde se rota |
|---|---|
| Clave de la API de Anthropic | console.anthropic.com → revocar y crear otra |
| Client Secret de Discord | Portal de desarrolladores → **Reset Secret** |
| Webhook del canal Tickets | Discord → canal Tickets → borrar y crear otro |

Los valores nuevos van **solo** a `clave.txt`, que está en `.gitignore`.
No van a este repositorio, ni a un chat, ni a un documento.

---

## 2. Qué funciona hoy

- Los dos terminales, con datos en vivo por SSE.
- Sesgo diario con IA, calculado una vez y compartido entre todos los alumnos.
- Comparativa de economías con puntuación proporcional al margen.
- Noticias clasificadas por impacto, con explicación breve y traducción.
- Calendario macro a tres semanas.
- Login con Discord y comprobación de pertenencia al servidor.
- Journal completo: EA de MT5, endpoints de recepción, motor de métricas con
  91 pruebas, y la vista con KPIs, curva de capital, calendario mensual,
  sesiones y desglose semanal.
- Marcadores por alumno.
- Tickets de soporte al canal de Discord, con persistencia si el envío falla.

---

## 3. Lo que hay que resolver antes de abrir

Ordenado por lo que más duele si se ignora.

### 3.1 La fuente de datos de mercado (bloqueante, legal)

Las cotizaciones y el calendario se obtienen **raspando endpoints internos de
TradingView**. Sus condiciones prohíben el scraping, la redistribución de datos
de mercado y el uso comercial. Como herramienta interna es zona gris; como
producto de pago para ~92 alumnos, no se sostiene.

El widget de gráficos de TradingView **sí es legítimo** y se queda. Lo que hay
que sustituir es la fuente de precios y de calendario.

Camino recomendado, de menor a mayor coste:

| Pieza | Solución |
|---|---|
| Gráficos | Widget de TradingView. Sin cambios |
| Titulares | RSS de medios, con atribución y enlace. Sin cambios |
| Fechas del calendario | Calendarios de publicación de BLS, Eurostat, INE, ONS, BCE y Fed. Públicos y gratuitos |
| Datos macro del sesgo | BCE, Eurostat, FRED. Públicos |
| **Precio en vivo** | Proveedor licenciado con permiso de *external display*. Es la única pieza que obliga a pagar |

Hay presupuestos pedidos a proveedores de datos. Orden de magnitud: 50–80 $/mes
para el feed de precios.

**El volumen no es el problema.** El servidor consulta una vez y reparte por
SSE, así que el consumo es el mismo con 5 alumnos que con 300. Lo que se
contrata es la *clase de licencia*, no el caudal.

### 3.2 Cuotas de IA por usuario (bloqueante, económico)

No hay límite de gasto por alumno. Hoy da igual porque solo lo usa el equipo.
Con el registro abierto, un solo usuario puede vaciar el presupuesto del mes en
una tarde.

Hace falta: contador por usuario y día, corte al llegar al límite con un mensaje
claro, y un panel donde se vea el gasto acumulado. El análisis compartido ya
cubre la parte cara; esto es solo para el chat.

### 3.3 Concurrencia en el almacenamiento (bloqueante, técnico)

El estado se guarda en archivos JSON en la raíz (`journal.json`,
`marcadores.json`, `memoria_dyn.json`, `tickets.json`). Para lectura con ~100
alumnos aguanta. Lo que no aguanta es **escritura concurrente**: dos alumnos
guardando a la vez pueden pisarse.

Mínimo viable: un candado de escritura y escritura atómica con archivo temporal
más `os.replace` (el patrón ya está en `journal/receptor-ea.py`).
A partir de unos cientos de usuarios, SQLite o PostgreSQL.

### 3.4 Protección de datos (bloqueante, legal)

Se guarda el historial de operaciones y las preguntas de personas
identificadas. Eso es dato personal. Falta política de privacidad, base legal
del tratamiento y un procedimiento de borrado a petición.

La memoria del asistente ya guarda solo un hash del ID de Discord, nunca el
nombre. El Journal, en cambio, sí está asociado a una persona.

### 3.5 Descargo de responsabilidad

Debe aparecer en las dos vistas del sesgo y en el pie de todas las páginas: no
es recomendación de inversión, es un sesgo generado con IA con fines formativos.

Conviene tenerlo claro: **el descargo ayuda pero no es un escudo.** Si la
actividad se considera asesoramiento en inversión, la etiqueta no la cambia.
Hay asesoría revisándolo.

---

## 4. Despliegue

No está desplegado. El plan acordado:

| Pieza | Elección | Coste |
|---|---|---|
| Servidor | Hetzner Cloud CX22, Ubuntu 24.04 LTS, Falkenstein o Helsinki | 4,49 €/mes |
| Dominio | `analyst.dynastia.es`, registro A en GoDaddy | ~1 €/mes |
| HTTPS y proxy inverso | Caddy con Let's Encrypt | 0 € |
| Arranque permanente | Servicio systemd con reinicio automático | 0 € |
| Copias | Snapshot del proveedor + copia de los `.json` | ~1 €/mes |

Aviso sobre Hetzner: subieron precios dos veces en 2026 y la línea AMD (CPX,
CCX) se encareció entre el 113 % y el 175 %. **Usar CX o CAX, no CPX.**

Dos detalles del proxy que suelen morder:

- El endpoint `/api/stream` es **SSE**: hay que desactivar el buffering y subir
  el tiempo de espera, o la conexión se corta cada minuto.
- Las llamadas a la IA pueden tardar más de 60 segundos. El tiempo de espera del
  proxy tiene que contemplarlo.

La cookie de sesión se marca `Secure` en cuanto detecta HTTPS, y Discord no
acepta direcciones de retorno sin cifrar salvo en `127.0.0.1`.

---

## 5. Lo siguiente en el plan

Suscripción con Stripe: Checkout más Billing, webhook que marca la suscripción
activa o vencida, y comprobación en cada carga. **El acceso se concede por
suscripción activa en Stripe**, no por el rol de Discord; el rol se usa como
cortesía para equipo e invitados.

Cuota prevista: 25–30 €/mes por alumno. 92 alumnos actuales.

Antes de crear los productos en Stripe hay dos decisiones de negocio abiertas:
un nivel o dos, y si hay pago anual. Cambiar un producto con suscripciones
vivas obliga a migrar cliente a cliente.

---

## 6. Decisiones abiertas de producto

Están en `docs/plan-plataforma.md` marcadas como **[DECIDIR]**:

- ¿El EA envía también las operaciones abiertas, o solo las cerradas?
  (Decidido: solo cerradas, pero conviene confirmarlo.)
- ¿Los alumnos ven el Journal de otros, o solo el suyo?
- ¿Un nivel de suscripción o dos? ¿Pago anual con descuento?

---

## 7. Convenciones del código

- **Todo en español**: nombres de variables, funciones y comentarios.
- Los comentarios explican **por qué**, no qué. Varios documentan un fallo real
  que se corrigió; conviene leerlos antes de "simplificar" algo.
- El front no tiene compilación. Se edita el HTML y se recarga.
- `metricas.js` es puro y está cubierto por pruebas: si se toca, se ejecutan.
