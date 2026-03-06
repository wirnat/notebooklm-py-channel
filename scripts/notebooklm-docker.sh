#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${NOTEBOOKLM_PROJECT_DIR:-/Users/iturban/Development/notebooklm-py-channel}"
STACK_SCRIPT="$PROJECT_DIR/scripts/whatsapp-bridge-docker.sh"
CONTAINER_NAME="${NOTEBOOKLM_CONTAINER_NAME:-notebooklm-whatsapp-notebooklm_bridge-1}"
ENV_FILE="${WHATSAPP_BRIDGE_ENV_FILE:-$PROJECT_DIR/.env.whatsapp}"
HOST_NOTEBOOKLM_BIN="${NOTEBOOKLM_HOST_BIN:-$PROJECT_DIR/.venv/bin/notebooklm}"

if [[ ! -x "$STACK_SCRIPT" ]]; then
  echo "Script stack tidak ditemukan: $STACK_SCRIPT" >&2
  exit 1
fi

if [[ "${1:-}" == "login" ]]; then
  if [[ ! -x "$HOST_NOTEBOOKLM_BIN" ]]; then
    echo "Binary host untuk login tidak ditemukan: $HOST_NOTEBOOKLM_BIN" >&2
    echo "Set NOTEBOOKLM_HOST_BIN atau siapkan .venv agar notebooklm login bisa dijalankan." >&2
    exit 1
  fi

  # Sinkronkan lokasi session cookie host dengan yang dipakai container.
  if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
  fi
  if [[ -n "${NOTEBOOKLM_HOME_HOST:-}" ]]; then
    export NOTEBOOKLM_HOME="$NOTEBOOKLM_HOME_HOST"
  fi

  exec "$HOST_NOTEBOOKLM_BIN" "$@"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker tidak ditemukan. Install Docker terlebih dahulu." >&2
  exit 1
fi

# Auto-start stack jika container bridge belum aktif.
if [[ -z "$(docker ps --filter "name=^${CONTAINER_NAME}$" --format '{{.Names}}')" ]]; then
  echo "Stack belum aktif. Menjalankan docker stack..."
  (cd "$PROJECT_DIR" && "$STACK_SCRIPT" up)
fi

if [[ $# -eq 0 ]]; then
  set -- --help
fi

if [[ -t 0 && -t 1 ]]; then
  exec docker exec -it "$CONTAINER_NAME" notebooklm "$@"
fi

exec docker exec -i "$CONTAINER_NAME" notebooklm "$@"
