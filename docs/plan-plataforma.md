# Plataforma de la academia — plan técnico

Documento de decisión. Lo que aparece marcado como **[DECIDIR]** necesita tu
respuesta antes de que se escriba código, porque cambiarlo después es caro.

---

## 1. Qué cambia respecto a hoy

Hoy tienes una herramienta local: servidor de biblioteca estándar en
`localhost:8787`, sin usuarios, sin base de datos, y los HTML se reparten a
mano. Funciona bien para ti y para tu equipo, y **se queda como está**: seguirá
siendo tu terminal interno.

Lo que se construye es una cosa distinta que vive en internet:

```
                      ┌─────────────────────────┐
   Alumno ─── web ───▶│  analyst.dynastia.es      │
                      │  FastAPI + PostgreSQL   │
   MT5 del alumno     │                         │
     └── EA ── HTTPS ─▶│  /ea/v1/…              │
                      │                         │
   Discord OAuth ─────▶│  identidad y rol       │
   Stripe webhook ────▶│  estado de suscripción │
                      └─────────────────────────┘
```

Los dos terminales actuales (cripto y divisas) se sirven desde ahí, detrás del
login, en vez de repartirse como archivos.

---

## 2. El Expert Advisor (la pieza que decide todo lo demás)

Un EA en MQL5 que el alumno instala una vez en su MT5. No pide contraseñas: el
alumno pega un **código de vinculación** de 8 caracteres que genera la
plataforma, y el EA queda atado a su cuenta.

### Protocolo

| Momento | Envío |
|---|---|
| Al arrancar | Alta: número de cuenta, bróker, divisa, apalancamiento, saldo |
| Cada 30 s | Latido: saldo, equity, margen, operaciones abiertas |
| Al cerrar una operación | La operación completa |
| Primera vez | Volcado del histórico completo, en tandas de 200 |

Cada operación viaja así:

```json
{
  "ticket": 123456789,
  "symbol": "EURUSD",
  "type": "buy",
  "volume": 0.10,
  "open_time": "2026-08-16T09:14:03Z",
  "open_price": 1.16407,
  "close_time": "2026-08-16T11:42:55Z",
  "close_price": 1.16812,
  "sl": 1.16100, "tp": 1.16900,
  "commission": -0.80, "swap": 0.00, "profit": 40.50,
  "comment": "", "magic": 0
}
```

**Seguridad.** El EA solo envía. No recibe órdenes, no opera, no puede
modificar nada. Aunque alguien reventara el servidor, lo máximo que obtendría
es el historial de operaciones, nunca acceso a las cuentas. Esto es lo que
hace viable la opción del EA frente a un puente con credenciales.

**[DECIDIR]** ¿El EA envía también las operaciones **abiertas** en tiempo real,
o solo las cerradas? Enviar las abiertas permite un panel en vivo y avisos de
riesgo (por ejemplo, «llevas tres operaciones abiertas en la misma divisa»),
pero implica que ves la operativa del alumno mientras ocurre. Es una decisión
de producto y de confianza, no técnica.

---

## 3. El Journal

Falta tu captura de referencia para cerrar las métricas exactas. Con lo que se
puede calcular a partir de los datos del EA, la base sería:

**Resumen**: resultado neto, número de operaciones, tasa de acierto, factor de
beneficio, media de ganancia frente a media de pérdida, esperanza matemática
por operación, racha máxima ganadora y perdedora.

**Riesgo**: caída máxima (en dinero y en porcentaje), riesgo medio por
operación, exposición máxima simultánea, ratio de recuperación.

**Gráficas**: curva de capital, resultado por día y por mes, distribución de
resultados, resultado por símbolo, por día de la semana y por franja horaria,
y duración media de las operaciones.

**Calendario**: rejilla mensual coloreada por resultado diario, cada día
desplegable con sus operaciones.

**Valoración con IA**: fortalezas y debilidades a partir de los patrones
reales — si corta ganancias antes de tiempo, si promedia a la baja, si opera
peor en determinadas horas, si el riesgo por operación es inconsistente. Se
apoya en los números calculados, no en impresiones.

**[DECIDIR]** ¿Los alumnos ven el Journal de otros o solo el suyo? ¿Y tú y
Claudia veis el de todos? Esto define los permisos desde la primera tabla.

---

## 4. Identidad y cobro

**Discord OAuth2** para entrar. Al volver de Discord se comprueba que el
usuario está en tu servidor y qué roles tiene. Ventaja: no gestionas
contraseñas, no hay recuperación de cuenta, no hay filtraciones de claves.

**Stripe Checkout + Billing** para cobrar. Un webhook marca la suscripción
activa o vencida. Cada carga de página comprueba el estado.

El acceso se concede si **hay suscripción activa en Stripe**. El rol de
Discord se usa como cortesía (equipo, invitados), nunca como fuente de verdad
del pago.

**Decidido:** cuota mensual de 25 a 30 € por alumno. 92 alumnos actuales.

**[DECIDIR]** ¿Un solo nivel o dos (básico y completo)? ¿Se ofrece pago anual
con descuento? Hay que fijarlo antes de crear los productos en Stripe, porque
cambiar un producto con suscripciones vivas obliga a migrar a cada cliente.

---

## 5. Coste mensual estimado

| Concepto | Coste |
|---|---|
| Servidor y base de datos gestionada | 25 – 50 € |
| Dominio | ~1 € |
| Correo transaccional | 0 – 15 € |
| Comisión de Stripe | ~1,5 % + 0,25 € por cobro |
| Consumo de IA | 150 – 300 € |

### Cuenta con los 92 alumnos actuales

| Concepto | Mensual |
|---|---|
| Ingreso bruto (92 × 25 €) | 2.300 € |
| IVA 21 % si facturas B2C en España | −399 € |
| Comisión de Stripe (~1,5 % + 0,25 €) | −58 € |
| Servidor, base de datos y dominio | −50 € |
| Consumo de IA | −150 a −300 € |
| **Margen aproximado** | **≈ 1.500 €** |

Las cifras de servidor e IA son **estimación**, no dato: dependen del proveedor
que se contrate y del uso real. El resto es aritmética.

### La decisión que hace que el margen aguante

El sesgo y las explicaciones de noticias se calculan **una sola vez en el
servidor** y se sirven a los 92 alumnos. No una vez por alumno. Si cada uno
dispara su propio análisis, el coste se multiplica por 92 y se come el margen
entero.

Con cálculo compartido, el coste de IA es **casi independiente del número de
alumnos**: 300 alumnos costarían prácticamente lo mismo que 92. Lo único que
escala con usuarios es el chat del asistente, y ahí va una **cuota diaria por
usuario**, con un panel donde ves el gasto acumulado.

Esto hay que fijarlo ahora. Rehacerlo cuando ya haya alumnos pagando es caro.

---

## 6. Orden de trabajo

1. Base: servidor, base de datos, sesiones, y los dos terminales servidos
   detrás del login.
2. Discord OAuth y alta de usuarios. Registro de quién entra y cuándo.
3. EA de MT5 y los endpoints que lo reciben. Volcado histórico y latido.
4. Journal: cálculo de métricas y gráficas.
5. Valoración con IA y cuotas de consumo.
6. Stripe: productos, Checkout, webhook y control de acceso.
7. Despliegue, dominio, copias de seguridad.

Los pasos 1 a 3 son los que no admiten cambios posteriores baratos. Del 4 en
adelante se puede iterar sin dolor.

---

## 7. Lo que sigue siendo tu responsabilidad

- **Validación legal del cobro.** Me dices que tu asesoría lo cubre. Que quede
  por escrito de ellos antes del primer cobro, no de palabra. Un descargo de
  responsabilidad en letra pequeña **ayuda pero no es un escudo**: si la
  actividad se considera asesoramiento en inversión, el texto de abajo no la
  convierte en otra cosa. Lo que cuenta es qué haces, no cómo lo etiquetas.
- **Descargo visible.** En las dos vistas del sesgo, y en el pie de todas las
  páginas: no es recomendación de inversión, es un sesgo generado con
  inteligencia artificial con fines formativos.
- **IVA de servicios digitales.** Stripe Tax lo calcula; declararlo por
  ventanilla única es tuyo.
- **Residencia en Dubái el año que viene.** Decide **antes** de crear los
  productos en Stripe qué sociedad factura. Migrar suscripciones activas de
  una entidad a otra es caro y molesta al cliente.
- **Protección de datos.** Vas a guardar la operativa de personas
  identificadas: eso es dato personal. Hace falta política de privacidad,
  base legal y un procedimiento de borrado.
