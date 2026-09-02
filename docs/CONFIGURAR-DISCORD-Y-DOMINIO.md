# Acceso con Discord y dominio de GoDaddy

Lo que tienes que hacer tú. Son dos cosas independientes: el acceso con
Discord ya funciona en local, y el dominio hace falta para que entren de fuera.

---

## 1. La aplicación de Discord

Diez minutos en el portal de desarrolladores de Discord.

1. Entra en **discord.com/developers/applications** y pulsa *New Application*.
   Ponle el nombre que verán los alumnos al entrar: **Dynastia Analyst**.
2. En **OAuth2** copia el **Client ID** y genera el **Client Secret**.
   Estos dos valores van directamente a `clave.txt`. No los pegues en ningún
   otro sitio, ni en documentos, ni en chats: con ambos se puede suplantar tu
   aplicación. Si alguna vez se te escapan, entra en la app y pulsa
   **Reset Secret**.
3. En esa misma pantalla, en **Redirects**, añade estas dos direcciones
   exactamente, una por línea:

   ```
   http://127.0.0.1:8787/auth/discord/callback
   https://analyst.dynastia.es/auth/discord/callback
   ```

   La primera es para probar en tu ordenador. La segunda, la definitiva.
   Discord es estricto: si no coincide carácter por carácter, rechaza la
   entrada.
4. No hace falta bot, ni permisos de bot, ni invitar nada al servidor.

### El ID de tu servidor de Discord

En Discord, **Ajustes de usuario → Avanzado → Modo desarrollador** activado.
Después, clic derecho sobre el icono de tu servidor → **Copiar ID del
servidor**.

### Si quieres exigir un rol concreto

Clic derecho sobre el rol en *Ajustes del servidor → Roles* → **Copiar ID**.
Puedes poner varios separados por comas. Si no pones ninguno, basta con
pertenecer al servidor.

---

## 2. Ponerlo en clave.txt

Al final del archivo `clave.txt`, que **no se comparte nunca**:

```
discord CLIENT_ID CLIENT_SECRET ID_DEL_SERVIDOR
```

Tres valores separados por espacios, **sin escribir para qué es cada uno**.
Sustituye las tres palabras por los números, sin dejar etiquetas dentro:

```
discord 1234567890123456789 aBcDeFgHiJkLmNoPqRsTuVwXyZ 9876543210987654321
```

La línea `dominio` **no la pongas todavía**: mientras pruebes en tu ordenador,
sin ella el retorno va a `127.0.0.1` y funciona. Cuando el dominio apunte de
verdad al servidor, la añades.

**Reinicia el servidor.** `clave.txt` se lee solo al arrancar.

En cuanto haya una línea `discord`, la puerta se activa: todo pide sesión
menos la página de entrada. Si la quitas, el terminal vuelve a abrirse
directo como hasta ahora, para trabajar en local.

---

## 3. El dominio de GoDaddy

Necesito una decisión y un dato tuyo.

**Decidido:** `analyst.dynastia.es`, en lugar del dominio raíz. Así la web de la academia y el terminal viven separados y puedes
mover uno sin tocar el otro.

**Dato: dónde se va a alojar.** Hasta que no haya servidor contratado no hay
IP a la que apuntar. En cuanto lo tengas, en GoDaddy es:

1. *Mis productos → DNS → Administrar zonas*.
2. Añadir registro:
   - Tipo **A**, Nombre `analyst`, Valor la IP del servidor, TTL 1 hora.
   - Si el proveedor te da un nombre en vez de una IP, entonces tipo **CNAME**
     con ese destino.
3. Guardar y esperar. Suele tardar minutos, pero puede llegar a una hora.

**No uses el reenvío de dominio de GoDaddy.** Rompe HTTPS y las cookies de
sesión: el login dejaría de funcionar. Tiene que ser un registro A o CNAME.

### Certificado HTTPS

Obligatorio, y no solo por buenas prácticas: la cookie de sesión se marca
como `Secure` cuando detecta HTTPS, y Discord no acepta direcciones de retorno
sin cifrar salvo en `127.0.0.1`. Con Caddy o Nginx más Let's Encrypt es
automático y gratis.

---

## 4. Los tres errores que dejan a todos fuera

El servidor los detecta al arrancar y te los dice, pero conviene conocerlos.

**El tercer valor no es de la aplicación.** Es el ID de tu servidor de Discord.
Se saca desde Discord, no desde el portal de desarrolladores: modo
desarrollador activado, clic derecho en el icono del servidor, *Copiar ID del
servidor*. Si copias otra vez el de la app, la comprobación de pertenencia
falla y no entra nadie.

**La línea `discord_roles` con los números de ejemplo.** Si está puesta, se
exige tener alguno de esos roles. Los `111111...` no existen, así que
bloquearía a todo el mundo, tú incluido. Para la prueba cerrada **bórrala**:
basta con pertenecer al servidor.

**Las direcciones de retorno.** Discord compara carácter por carácter. Un `/`
final de más, o `http` en vez de `https`, y rechaza la entrada.

---

## 5. El canal de Tickets

El botón de soporte del terminal publica los avisos en tu canal **Tickets** de
Discord. Se hace con un **webhook**, no con un bot: es una dirección que crea
el propio canal, no necesita permisos ni programa aparte, y si se filtrara lo
único que permitiría es escribir en ese canal.

1. En Discord, clic derecho sobre el canal **Tickets** → *Editar canal*.
2. **Integraciones → Webhooks → Nuevo webhook**.
3. Ponle nombre (por ejemplo, *Analyst*) y pulsa **Copiar URL del webhook**.
4. En `clave.txt`, una línea más:

   ```
   tickets https://discord.com/api/webhooks/...
   ```

5. Reinicia el servidor. Al arrancar debe decir:
   `Soporte: los tickets van al canal de Discord por webhook`

Cada aviso llega como una tarjeta con el alumno, el tipo, la sección en la que
estaba, el par en pantalla, su navegador y una referencia corta para poder
responderle. **Si el envío falla, el aviso no se pierde**: queda guardado en
`tickets.json` junto al servidor.

---

## 6. Qué necesito de ti para seguir

- Decidir el proveedor de hosting, o decirme que lo recomiende yo.
- El `client_id` **no** me lo pases por aquí, y el `client_secret` menos:
  ponlos tú directamente en `clave.txt`. Yo no necesito verlos en ningún
  momento.

---

## 7. Cómo probarlo hoy, antes del dominio

1. Añade solo la primera redirección, la de `127.0.0.1`.
2. Escribe las líneas de `discord` en `clave.txt` sin la de `dominio`.
3. Reinicia y abre `http://127.0.0.1:8787/forex`.

Deberías ver la pantalla de entrada, y al pulsar el botón, volver dentro con
tu nombre de Discord. En la ventana del servidor aparecerá:

```
acceso    ENTRA Ricardo (123456789012345678)
```

Y si pruebas con una cuenta que no esté en el servidor:

```
acceso    RECHAZADO · nombre no esta en el servidor
```
