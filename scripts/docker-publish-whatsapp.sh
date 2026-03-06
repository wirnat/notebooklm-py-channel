#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_DOCKERFILE="$ROOT_DIR/docker/notebooklm.bridge.Dockerfile"
BRIDGE_CONTEXT="$ROOT_DIR"
GOWA_DOCKERFILE="$ROOT_DIR/go-whatsapp-web-multidevice/docker/golang.Dockerfile"
GOWA_CONTEXT="$ROOT_DIR/go-whatsapp-web-multidevice"

REGISTRY_NAMESPACE="docker.io/wirnat"
TAG="latest"
PLATFORMS="linux/amd64,linux/arm64"
BRIDGE_IMAGE_NAME="notebooklm-wa-bridge"
GOWA_IMAGE_NAME="notebooklm-whatsapp-go"
NO_CACHE="false"
DRY_RUN="false"

usage() {
  cat <<'USAGE'
Publish image WhatsApp stack (bridge + GoWA) ke Docker registry.

Penggunaan:
  ./scripts/docker-publish-whatsapp.sh [opsi]

Contoh:
  ./scripts/docker-publish-whatsapp.sh \
    --registry docker.io/wirnat \
    --tag v0.1.0

Opsi:
  --registry <namespace>        Default: docker.io/wirnat
  --tag <tag>                   Default: latest
  --platform <list>             Default: linux/amd64,linux/arm64 (buildx)
  --bridge-image-name <name>    Default: notebooklm-wa-bridge
  --gowa-image-name <name>      Default: notebooklm-whatsapp-go
  --no-cache                    Build tanpa cache
  --dry-run                     Print command tanpa eksekusi
  -h, --help                    Tampilkan bantuan
USAGE
}

run_cmd() {
  echo "+ $*"
  if [[ "$DRY_RUN" == "false" ]]; then
    "$@"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --registry)
      REGISTRY_NAMESPACE="${2:-}"
      shift 2
      ;;
    --tag)
      TAG="${2:-}"
      shift 2
      ;;
    --platform)
      PLATFORMS="${2:-}"
      shift 2
      ;;
    --bridge-image-name)
      BRIDGE_IMAGE_NAME="${2:-}"
      shift 2
      ;;
    --gowa-image-name)
      GOWA_IMAGE_NAME="${2:-}"
      shift 2
      ;;
    --no-cache)
      NO_CACHE="true"
      shift
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Argumen tidak dikenali: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker tidak ditemukan. Install Docker terlebih dahulu." >&2
  exit 1
fi

BRIDGE_IMAGE="${REGISTRY_NAMESPACE}/${BRIDGE_IMAGE_NAME}:${TAG}"
GOWA_IMAGE="${REGISTRY_NAMESPACE}/${GOWA_IMAGE_NAME}:${TAG}"

build_and_push() {
  local image="$1"
  local dockerfile="$2"
  local context="$3"

  if docker buildx version >/dev/null 2>&1; then
    local cmd=(docker buildx build --platform "$PLATFORMS" -t "$image" -f "$dockerfile" "$context" --push)
    if [[ "$NO_CACHE" == "true" ]]; then
      cmd+=(--no-cache)
    fi
    run_cmd "${cmd[@]}"
  else
    echo "buildx tidak ditemukan. Fallback ke docker build + push single-arch."
    local cmd=(docker build -t "$image" -f "$dockerfile" "$context")
    if [[ "$NO_CACHE" == "true" ]]; then
      cmd+=(--no-cache)
    fi
    run_cmd "${cmd[@]}"
    run_cmd docker push "$image"
  fi
}

echo "Pastikan sudah login registry (contoh: docker login atau docker login ghcr.io)."
echo "Publish bridge image: $BRIDGE_IMAGE"
build_and_push "$BRIDGE_IMAGE" "$BRIDGE_DOCKERFILE" "$BRIDGE_CONTEXT"

echo "Publish GoWA image: $GOWA_IMAGE"
build_and_push "$GOWA_IMAGE" "$GOWA_DOCKERFILE" "$GOWA_CONTEXT"

cat <<EOF

Selesai publish image:
- $BRIDGE_IMAGE
- $GOWA_IMAGE

Contoh env untuk dibagikan ke developer:
NOTEBOOKLM_BRIDGE_IMAGE=$BRIDGE_IMAGE
WHATSAPP_GO_IMAGE=$GOWA_IMAGE
EOF
