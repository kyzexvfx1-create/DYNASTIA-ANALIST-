#!/bin/bash
# Doble clic para abrir el terminal de DIVISAS.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo ""; echo "  Python 3 no esta instalado."
  echo "  Instalalo desde https://www.python.org/downloads/"; echo ""
  read -r -p "  Pulsa Intro para cerrar..."; exit 1
fi

if [ ! -f "forex-ops-center.html" ]; then
  echo ""; echo "  No encuentro forex-ops-center.html en esta carpeta:"
  echo "  $(pwd)"; echo ""
  echo "  Copia ese archivo aqui y vuelve a intentarlo."; echo ""
  read -r -p "  Pulsa Intro para cerrar..."; exit 1
fi

# ¿Hay ya un servidor escuchando? Se comprueba ademas que sea una version
# que conozca la ruta de divisas: la anterior devuelve 404 en /forex.
PING=$(curl -s -m 2 http://127.0.0.1:8787/api/ping 2>/dev/null)
if [ -n "$PING" ]; then
  if echo "$PING" | grep -q '"/forex"'; then
    echo ""; echo "  Servidor ya en marcha. Abriendo el terminal de divisas..."
    open "http://127.0.0.1:8787/forex"; sleep 1; exit 0
  fi
  echo ""
  echo "  =============================================================="
  echo "   Hay un servidor ANTIGUO funcionando en el puerto 8787."
  echo "  =============================================================="
  echo ""
  echo "   Esa version no conoce la ruta de divisas: por eso salia el"
  echo "   error 404. Hay que reiniciarlo."
  echo ""
  echo "   1. Busca la ventana negra del servidor anterior."
  echo "   2. Pulsa Control+C ahi, o cierra esa ventana."
  echo "   3. Vuelve a abrir este lanzador."
  echo ""
  read -r -p "   ¿Quieres que intente cerrarlo yo? (s/n): " R
  if [ "$R" = "s" ] || [ "$R" = "S" ]; then
    pkill -f "servidor.py" 2>/dev/null
    sleep 2
    echo "   Servidor anterior detenido. Arrancando el nuevo..."
  else
    echo ""; read -r -p "   Pulsa Intro para cerrar..."; exit 0
  fi
fi

echo ""
echo "  Arrancando el servidor y abriendo el terminal de divisas..."
export CRYPTO_OPS_ABRIR=forex
exec python3 servidor.py
