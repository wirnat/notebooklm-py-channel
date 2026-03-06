#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE_REGISTRY="$ROOT_DIR/docker-compose.whatsapp.registry.yml"
COMPOSE_FILE_BUILD="$ROOT_DIR/docker-compose.whatsapp.yml"
ENV_EXAMPLE="$ROOT_DIR/.env.whatsapp.example"
ENV_FILE="${WHATSAPP_BRIDGE_ENV_FILE:-$ROOT_DIR/.env.whatsapp}"

usage() {
  cat <<USAGE
Penggunaan:
  $(basename "$0") init
  $(basename "$0") up
  $(basename "$0") up-build
  $(basename "$0") pull
  $(basename "$0") down
  $(basename "$0") restart
  $(basename "$0") logs [service]
  $(basename "$0") ps

Keterangan:
  - Command "up" = mode docker-only (pull image prebuilt dari Docker Hub)
  - Default image namespace: docker.io/wirnat
  - "up-build" hanya untuk maintainer yang perlu build dari source lokal
  - File env default: .env.whatsapp
  - Override env file: WHATSAPP_BRIDGE_ENV_FILE=/path/file.env
  - Override compose file: WHATSAPP_BRIDGE_COMPOSE_FILE=/path/docker-compose.yml
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
  local compose_file="${WHATSAPP_BRIDGE_COMPOSE_FILE:-$COMPOSE_FILE_REGISTRY}"
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose --env-file "$ENV_FILE" -f "$compose_file" "$@"
    return
  fi

  if command -v docker-compose >/dev/null 2>&1; then
    docker-compose --env-file "$ENV_FILE" -f "$compose_file" "$@"
    return
  fi

  echo "Docker Compose tidak ditemukan. Install Docker Desktop atau docker-compose." >&2
  exit 1
}

run_registry_stack() {
  local bridge_image="${NOTEBOOKLM_BRIDGE_IMAGE:-docker.io/wirnat/notebooklm-wa-bridge:latest}"
  local gowa_image="${WHATSAPP_GO_IMAGE:-docker.io/wirnat/notebooklm-whatsapp-go:latest}"

  WHATSAPP_BRIDGE_COMPOSE_FILE="$COMPOSE_FILE_REGISTRY" compose pull
  WHATSAPP_BRIDGE_COMPOSE_FILE="$COMPOSE_FILE_REGISTRY" GOWA_WEBHOOK_URL="http://notebooklm_bridge:8787/webhook/whatsapp" compose up -d

  echo "Menggunakan image registry:"
  echo "- notebooklm_bridge: ${bridge_image}"
  echo "- whatsapp_go      : ${gowa_image}"
}

run_build_stack() {
  WHATSAPP_BRIDGE_COMPOSE_FILE="$COMPOSE_FILE_BUILD" GOWA_WEBHOOK_URL="http://notebooklm_bridge:8787/webhook/whatsapp" compose up -d --build
}

print_endpoints() {
  echo "GoWA UI   : http://127.0.0.1:${GOWA_UI_PORT:-8781}"
  echo "Webhook   : http://127.0.0.1:${WA_BRIDGE_PORT:-8787}/webhook/whatsapp"
  echo "Bridge HP : http://127.0.0.1:${WA_BRIDGE_PORT:-8787}/healthz"
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
    echo "Direktori auth siap."
    echo "Default image registry: docker.io/wirnat/*"
    ;;
  up)
    ensure_env_file
    load_env_and_prepare_dirs
    run_registry_stack
    print_endpoints
    ;;
  up-build)
    ensure_env_file
    load_env_and_prepare_dirs
    run_build_stack
    echo "Menggunakan mode build dari source lokal."
    print_endpoints
    ;;
  pull)
    ensure_env_file
    load_env_and_prepare_dirs
    WHATSAPP_BRIDGE_COMPOSE_FILE="$COMPOSE_FILE_REGISTRY" compose pull
    ;;
  down)
    ensure_env_file
    compose down
    ;;
  restart)
    ensure_env_file
    load_env_and_prepare_dirs
    compose down
    run_registry_stack
    print_endpoints
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
