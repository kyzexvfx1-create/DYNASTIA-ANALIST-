# Despliegue

Del portátil a `analyst.dynastia.es` funcionando 24/7. Pensado para Ubuntu
24.04 LTS en un Hetzner Cloud CX22.

Aviso de proveedor: Hetzner subió precios dos veces en 2026 y la línea AMD
(CPX, CCX) se encareció entre el 113 % y el 175 %. **Contratar CX o CAX.**

---

## 1. El servidor

Hetzner Cloud → nuevo proyecto → CX22, Ubuntu 24.04 LTS, Falkenstein o
Helsinki, con clave SSH. Anota la IP.

## 2. El dominio

En GoDaddy, *Mis productos → DNS → Administrar zonas*:

- Tipo **A**, nombre `analyst`, valor la IP del servidor, TTL 1 hora.

**No usar el reenvío de dominio de GoDaddy**: rompe HTTPS y las cookies de
sesión, y el login deja de funcionar.

## 3. Asegurar la máquina

```bash
adduser dynastia && usermod -aG sudo dynastia
# En /etc/ssh/sshd_config:  PasswordAuthentication no
systemctl restart ssh
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
apt update && apt install -y python3 caddy
```

## 4. La aplicación

```bash
mkdir -p /opt/dynastia && chown dynastia:dynastia /opt/dynastia
# subir el proyecto a /opt/dynastia
cp /opt/dynastia/clave.ejemplo.txt /opt/dynastia/clave.txt
chmod 600 /opt/dynastia/clave.txt        # solo el dueño puede leerla
```

En `clave.txt`, añade ahora sí la línea del dominio:

```
dominio https://analyst.dynastia.es
```

## 5. Arranque permanente

`/etc/systemd/system/dynastia.service`:

```ini
[Unit]
Description=Dynastia Analyst
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=dynastia
WorkingDirectory=/opt/dynastia
ExecStart=/usr/bin/python3 /opt/dynastia/servidor.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# El proceso solo necesita su carpeta
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now dynastia
systemctl status dynastia
journalctl -u dynastia -f        # ver el registro en vivo
```

`Restart=always` es lo que convierte "está encendido" en "está encendido
siempre": si el proceso muere, systemd lo levanta en 5 segundos, y si se
reinicia la máquina arranca solo.

## 6. HTTPS

`/etc/caddy/Caddyfile`:

```
analyst.dynastia.es {
	encode gzip

	# Los eventos en vivo son SSE: sin esto la conexion se corta cada minuto
	@stream path /api/stream
	handle @stream {
		reverse_proxy 127.0.0.1:8787 {
			flush_interval -1
			transport http {
				response_header_timeout 24h
			}
		}
	}

	# Las llamadas a la IA pueden pasar de 60 s
	handle {
		reverse_proxy 127.0.0.1:8787 {
			transport http {
				response_header_timeout 300s
			}
		}
	}
}
```

```bash
systemctl reload caddy
```

Caddy pide y renueva el certificado de Let's Encrypt solo. La cookie de sesión
pasa a marcarse `Secure` en cuanto detecta HTTPS.

**Los dos ajustes del proxy no son opcionales.** Sin `flush_interval -1` el
terminal se queda sin datos en vivo, y sin el tiempo de espera largo las
respuestas de la IA se cortan a medias.

## 7. Discord

En el portal de desarrolladores, añadir la redirección de producción:

```
https://analyst.dynastia.es/auth/discord/callback
```

Discord compara carácter por carácter. Una barra de más y rechaza la entrada.

## 8. Copias de seguridad

- Snapshot automático del proveedor: cubre el desastre completo.
- Y una copia diaria de los `.json`, que es donde vive todo lo de los alumnos:

```bash
0 4 * * * cd /opt/dynastia && tar czf /opt/copias/datos-$(date +\%F).tgz *.json
```

Una copia que nunca se ha restaurado no es una copia. Probar la restauración
una vez.

## 9. Vigilancia

Un comprobador externo cada 5 minutos contra `/api/ping`, avisando al canal de
Discord. Sin esto, te enteras de que está caído porque lo dice un alumno.

---

## Comprobación final

1. `https://analyst.dynastia.es` carga con candado.
2. El login de Discord entra y vuelve con el nombre.
3. Los precios se mueven solos (SSE vivo).
4. El sesgo se genera sin cortarse.
5. `systemctl restart dynastia` y la web vuelve sola.
6. Reiniciar la máquina entera y comprobar que arranca sin tocar nada.

El paso 6 es el que de verdad demuestra el 24/7.
