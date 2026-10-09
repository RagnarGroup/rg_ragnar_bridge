#!/usr/bin/env bash
# Instala ragnar-bridge como servicio de systemd de usuario (sin root).
#
#   curl -fsSL https://raw.githubusercontent.com/gaguilarf/ragnar_bridge/main/install.sh \
#     | bash -s -- --url wss://panel.ragnargroup.app/api/v1/bridge/ws
#
# El token (ragbrg_...) se pide sin eco desde la terminal, o se toma de la
# variable RAGNAR_BRIDGE_TOKEN. `--token` sigue andando pero esta deprecado: en
# argv queda visible en `ps` y en el historial del shell.
#
# Detecta solo los CLIs instalados en el servidor (Claude Code, Antigravity y/o Codex):
# con al menos uno alcanza, y la app te deja elegir entre los que funcionan.
# --agent claude|agy|codex   opcional: maneja SOLO ese CLI.
#
# El bridge usa TU sesion del CLI: no te pide credenciales y Ragnar nunca las
# ve. Volver a correr este script con otro token REEMPLAZA la config y reinicia
# el servicio; para tener dos bridges distintos en el mismo servidor, --name.
set -euo pipefail

AGENT=""
URL=""
TOKEN="${RAGNAR_BRIDGE_TOKEN:-}"
WORKDIR="$HOME"
MODEL=""
NAME=""
SOURCE="git+https://github.com/gaguilarf/ragnar_bridge"

while [ $# -gt 0 ]; do
  case "$1" in
    --agent) AGENT="$2"; shift 2 ;;
    --url) URL="$2"; shift 2 ;;
    --token)
      TOKEN="$2"; shift 2
      echo "AVISO: --token esta deprecado (queda visible en ps y en el historial). Usa la variable RAGNAR_BRIDGE_TOKEN o deja que el script lo pida." >&2
      ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    *) echo "Argumento desconocido: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$TOKEN" ] && [ -n "$URL" ] && { : </dev/tty; } 2>/dev/null; then
  # `curl | bash` deja el stdin ocupado por el script: se lee de la terminal.
  printf "Token del servidor (ragbrg_..., no se muestra): " >/dev/tty
  IFS= read -rs TOKEN </dev/tty || true
  printf "\n" >/dev/tty
fi
if [ -z "$URL" ] || [ -z "$TOKEN" ]; then
  echo "Uso: [RAGNAR_BRIDGE_TOKEN=ragbrg_...] install.sh --url wss://.../api/v1/bridge/ws [--agent claude|agy|codex] [--workdir DIR] [--model M] [--name N]" >&2
  echo "     (sin RAGNAR_BRIDGE_TOKEN el token se pide por la terminal)" >&2
  exit 2
fi
case "$AGENT" in ""|claude|agy|codex) ;; *) echo "--agent tiene que ser claude, agy o codex." >&2; exit 2 ;; esac

command -v python3 >/dev/null || { echo "Falta python3 (>= 3.9, con el modulo venv)." >&2; exit 1; }

CLI_BIN=""
if [ -n "$AGENT" ]; then
  CLI_BIN="$(command -v "$AGENT" || true)"
  if [ -z "$CLI_BIN" ]; then
    echo "No encuentro \`$AGENT\` en el PATH (abri una terminal nueva si acabas de instalarlo)." >&2
    exit 1
  fi
  echo "==> Agente: $AGENT ($CLI_BIN)"
else
  DETECTADOS=""
  command -v claude >/dev/null && DETECTADOS="$DETECTADOS claude"
  command -v agy >/dev/null && DETECTADOS="$DETECTADOS agy"
  command -v codex >/dev/null && DETECTADOS="$DETECTADOS codex"
  if [ -z "$DETECTADOS" ]; then
    echo "No encuentro ni \`claude\`, ni \`agy\`, ni \`codex\` en el PATH. Instala al menos uno y logueate corriendolo una vez:" >&2
    echo "  Claude Code:  curl -fsSL https://claude.ai/install.sh | bash" >&2
    echo "  Antigravity:  curl -fsSL https://antigravity.google/cli/install.sh | bash" >&2
    echo "  Codex:        npm install -g @openai/codex   (y despues: codex login)" >&2
    echo "(si acabas de instalarlo, abri una terminal nueva para que el PATH se recargue)" >&2
    exit 1
  fi
  echo "==> Agentes detectados:$DETECTADOS"
fi

SUFIJO=""
[ -n "$NAME" ] && SUFIJO="-$NAME"
CONFIG_DIR="$HOME/.config/ragnar-bridge"
CONFIG="$CONFIG_DIR/config$SUFIJO.json"
SERVICIO="ragnar-bridge$SUFIJO"
VENV="$HOME/.local/share/ragnar-bridge/venv"

echo "==> Instalando ragnar-bridge en $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --upgrade --force-reinstall "$SOURCE"

echo "==> Escribiendo la config en $CONFIG (permisos 600)"
INIT_ARGS=(--config "$CONFIG" init --url "$URL" --workdir "$WORKDIR")
[ -n "$AGENT" ] && INIT_ARGS+=(--agent "$AGENT" --cli "$CLI_BIN")
[ -n "$MODEL" ] && INIT_ARGS+=(--model "$MODEL")
# El token va por el entorno de `init`, nunca en sus argumentos.
RAGNAR_BRIDGE_TOKEN="$TOKEN" "$VENV/bin/ragnar-bridge" "${INIT_ARGS[@]}" </dev/null

echo "==> Verificando (config, CLI y conexion con Ragnar; no gasta cuota)"
if ! "$VENV/bin/ragnar-bridge" --config "$CONFIG" doctor; then
  echo >&2
  echo "Algun paso fallo (arriba). Corregilo y corre:  $VENV/bin/ragnar-bridge --config $CONFIG doctor" >&2
  exit 1
fi

if ! command -v systemctl >/dev/null || ! systemctl --user show-environment >/dev/null 2>&1; then
  echo
  echo "No hay systemd de usuario disponible. Para correrlo a mano:"
  echo "  $VENV/bin/ragnar-bridge --config $CONFIG run"
  exit 0
fi

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$SERVICIO.service" <<UNIT
[Unit]
Description=Ragnar bridge${NAME:+ ($NAME)}
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$VENV/bin/ragnar-bridge --config $CONFIG run
Restart=always
RestartSec=5
# Salida 78 = Ragnar rechazo el token (revocado/invalido): reintentar no sirve.
RestartPreventExitStatus=78
# El CLI (y Node, en el caso de claude) tienen que estar en el PATH del servicio.
Environment=PATH=$PATH

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable "$SERVICIO.service"
# restart y no `enable --now`: si el servicio ya corria (una instalacion
# anterior), `--now` NO lo reinicia y el proceso viejo seguiria con la config y
# el token de antes -- el servidor nuevo de la app nunca se conectaria.
systemctl --user restart "$SERVICIO.service"

# Sin linger el servicio de usuario muere al cerrar tu sesion SSH.
if ! loginctl enable-linger "$USER" 2>/dev/null; then
  echo "AVISO: no pude activar linger; el bridge se detendra al cerrar tu sesion." >&2
  echo "       Corre una vez como root:  loginctl enable-linger $USER" >&2
fi

echo
echo "Listo. Ya deberias verlo conectado (punto verde) en la app de Ragnar."
echo "  Estado:  systemctl --user status $SERVICIO"
echo "  Logs:    journalctl --user -u $SERVICIO -f"
echo "  Chequeo: $VENV/bin/ragnar-bridge --config $CONFIG doctor"
