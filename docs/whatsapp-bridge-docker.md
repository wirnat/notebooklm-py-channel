# WhatsApp Bridge via Docker (Docker-Only)

Panduan ini sekarang **docker-only** untuk instalasi dan operasional harian.

- `./scripts/whatsapp-bridge-docker.sh up` akan menarik image prebuilt dari Docker Hub.
- Default image namespace yang dipakai tim: `docker.io/wirnat`.

## Arsitektur

Stack menjalankan 2 service dalam satu network Docker:

1. `whatsapp_go` (GoWA)
2. `notebooklm_bridge` (webhook bridge ke NotebookLM)

Endpoint default:

- GoWA UI: `http://127.0.0.1:8781`
- Webhook bridge: `http://127.0.0.1:8787/webhook/whatsapp`
- Health bridge: `http://127.0.0.1:8787/healthz`

## Prasyarat

- Docker Desktop / Docker Engine
- Docker Compose (`docker compose` atau `docker-compose`)
- Auth NotebookLM valid, pilih salah satu:
  - mount `storage_state.json` ke `NOTEBOOKLM_HOME_HOST`, atau
  - isi `NOTEBOOKLM_AUTH_JSON` di `.env.whatsapp`

## Instalasi Cepat (Docker-Only)

```bash
# 1) Buat file env dari template
./scripts/whatsapp-bridge-docker.sh init

# 2) Edit konfigurasi
#    file: .env.whatsapp
#    minimal: WA_WEBHOOK_SECRET dan NOTEBOOKLM_HOME_HOST

# 3) Jalankan semua service dari image prebuilt
./scripts/whatsapp-bridge-docker.sh up
```

Selesai. Tidak perlu build source lokal untuk developer.

## Konfigurasi Image

Secara default, `.env.whatsapp.example` sudah mengarah ke image `wirnat`:

```env
NOTEBOOKLM_BRIDGE_IMAGE=docker.io/wirnat/notebooklm-wa-bridge:latest
WHATSAPP_GO_IMAGE=docker.io/wirnat/notebooklm-whatsapp-go:latest
```

Kalau ingin pin versi tertentu, ubah tag image di `.env.whatsapp`.

## Command Operasional

```bash
# Pull image terbaru tanpa start container
./scripts/whatsapp-bridge-docker.sh pull

# Start stack (docker-only)
./scripts/whatsapp-bridge-docker.sh up

# Restart stack
./scripts/whatsapp-bridge-docker.sh restart

# Lihat status
./scripts/whatsapp-bridge-docker.sh ps

# Lihat logs
./scripts/whatsapp-bridge-docker.sh logs
./scripts/whatsapp-bridge-docker.sh logs whatsapp_go
./scripts/whatsapp-bridge-docker.sh logs notebooklm_bridge

# Stop stack
./scripts/whatsapp-bridge-docker.sh down
```

## Untuk Maintainer (Opsional)

Mode berikut **bukan** alur instalasi developer, hanya untuk maintenance image:

```bash
# Build dari source lokal
./scripts/whatsapp-bridge-docker.sh up-build

# Publish image (default registry: docker.io/wirnat)
./scripts/docker-publish-whatsapp.sh --tag v1.0.0
```

## Catatan Best Practice

- Gunakan `WA_WEBHOOK_SECRET` panjang dan random.
- Jangan commit `.env.whatsapp`.
- Isi `NOTEBOOKLM_WA_ADMINS` untuk membatasi admin command.
- Untuk production, tambahkan reverse proxy + TLS di depan port `8781` dan `8787`.
