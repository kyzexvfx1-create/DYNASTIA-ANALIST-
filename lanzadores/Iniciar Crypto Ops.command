#!/bin/bash
# Doble clic para arrancar el panel. Deja la ventana abierta mientras lo uses.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo ""
  echo "  Python 3 no esta instalado."
  echo "  Instalalo desde https://www.python.org/downloads/ y vuelve a intentarlo."
  echo ""
  read -r -p "  Pulsa Intro para cerrar..."
  exit 1
fi
exec python3 servidor.py
