#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.whatsapp.yml"
ENV_EXAMPLE="$ROOT_DIR/.env.whatsapp.example"
ENV_FILE="${WHATSAPP_BRIDGE_ENV_FILE:-$ROOT_DIR/.env.whatsapp}"

usage() {
  cat <<USAGE
Penggunaan:
  $(basename "$0") init
  $(basename "$0") up
  $(basename "$0") up-full
  $(basename "$0") down
  $(basename "$0") restart
  $(basename "$0") logs [service]
  $(basename "$0") ps

Keterangan:
  - File env default: .env.whatsapp
  - Override env file: WHATSAPP_BRIDGE_ENV_FILE=/path/file.env
USAGE
}

ensure_env_file() {
  if [[ -f "$ENV_FILE" ]]; then
    return
  fi

  cp "$ENV_EXAMPLE" "$ENV_FILE"
  echo "File env belum ada. Template dibuat di: $ENV_FILE"
  echo "Silakan edit nilainya dulu, lalu jalankan lagi command ini."
  exit 1
}

load_env_and_prepare_dirs() {
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  local notebooklm_home_host="${NOTEBOOKLM_HOME_HOST:-$ROOT_DIR/.docker/notebooklm-home}"
  mkdir -p "$notebooklm_home_host"
}

compose() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
    return
  fi

  if command -v docker-compose >/dev/null 2>&1; then
    docker-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
    return
  fi

  echo "Docker Compose tidak ditemukan. Install Docker Desktop atau docker-compose." >&2
  exit 1
}

cmd="${1:-}"
shift || true

case "$cmd" in
  init)
    if [[ -f "$ENV_FILE" ]]; then
      echo "Env file sudah ada: $ENV_FILE"
    else
      cp "$ENV_EXAMPLE" "$ENV_FILE"
      echo "Template env dibuat: $ENV_FILE"
    fi
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    mkdir -p "${NOTEBOOKLM_HOME_HOST:-$ROOT_DIR/.docker/notebooklm-home}"
    echo "Direktori auth siap. Pastikan storage_state.json sudah tersedia."
    ;;
  up)
    ensure_env_file
    load_env_and_prepare_dirs
    compose stop notebooklm_bridge >/dev/null 2>&1 || true
    compose rm -f notebooklm_bridge >/dev/null 2>&1 || true
    compose up -d --build whatsapp_go
    echo "GoWA UI   : http://127.0.0.1:${GOWA_UI_PORT:-8781}"
    echo
    echo "Jalankan bridge di host dengan command ini:"
    echo "notebooklm bridge whatsapp \\"
    echo "  --webhook-secret ${WA_WEBHOOK_SECRET:-secret} \\"
    echo "  --url http://127.0.0.1:${GOWA_UI_PORT:-8781}"
    ;;
  up-full)
    ensure_env_file
    load_env_and_prepare_dirs
    GOWA_WEBHOOK_URL="http://notebooklm_bridge:8787/webhook/whatsapp" compose up -d --build
    echo "GoWA UI   : http://127.0.0.1:${GOWA_UI_PORT:-8781}"
    echo "Webhook   : http://127.0.0.1:${WA_BRIDGE_PORT:-8787}/webhook/whatsapp"
    echo "Bridge HP : http://127.0.0.1:${WA_BRIDGE_PORT:-8787}/healthz"
    ;;
  down)
    ensure_env_file
    compose down
    ;;
  restart)
    ensure_env_file
    load_env_and_prepare_dirs
    compose down
    compose up -d --build whatsapp_go
    ;;
  logs)
    ensure_env_file
    compose logs -f "$@"
    ;;
  ps)
    ensure_env_file
    compose ps
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    echo "Perintah tidak dikenali: $cmd" >&2
    usage
    exit 1
    ;;
esac
