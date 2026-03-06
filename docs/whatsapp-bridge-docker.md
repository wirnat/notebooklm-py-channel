# WhatsApp Bridge via Docker

Dokumen ini menyediakan 2 mode:

1. `up` (default): hanya `whatsapp_go` container, bridge dijalankan manual via CLI host.
2. `up-full`: `whatsapp_go` + `notebooklm_bridge` sekaligus di Docker.

## Kenapa ini menyelesaikan error `127.0.0.1 refused to connect`

Sebelumnya bridge jalan, tapi service GoWA tidak jalan di port UI. Dengan stack ini, GoWA dan bridge dijalankan dalam satu network Docker dan start bersama.

## Prasyarat

- Docker Desktop / Docker Engine + Compose plugin
- Session auth NotebookLM valid (pilih salah satu):
  - `storage_state.json` dari `notebooklm login` (opsi paling mudah), atau
  - `NOTEBOOKLM_AUTH_JSON`

## Quick Start

```bash
# 1) Siapkan env + direktori auth
./scripts/whatsapp-bridge-docker.sh init

# 2) Edit konfigurasi
#    file: .env.whatsapp

# 3) Jalankan GoWA container saja
./scripts/whatsapp-bridge-docker.sh up

# 4) Jalankan bridge di host (bukan di container)
notebooklm bridge whatsapp \
  --webhook-secret <isi WA_WEBHOOK_SECRET> \
  --url http://127.0.0.1:8781
```

Setelah `up`:

- GoWA UI: `http://127.0.0.1:8781`
- GoWA akan forward webhook ke: `http://host.docker.internal:8787/webhook/whatsapp`

## Konfigurasi Penting (`.env.whatsapp`)

- `WA_WEBHOOK_SECRET`
  - Wajib. Harus sama antara GoWA dan bridge.
- `GOWA_WEBHOOK_URL`
  - Default ke `http://host.docker.internal:8787/webhook/whatsapp` untuk bridge yang jalan di host.
- `NOTEBOOKLM_HOME_HOST`
  - Path host ke direktori auth NotebookLM (contoh: `/Users/iturban/.notebooklm`).
- `GOWA_UI_PORT`
  - Port host untuk UI GoWA (default `8781`).
- `WA_BRIDGE_PORT`
  - Port host untuk webhook bridge (default `8787`).
- `NOTEBOOKLM_WA_GLOBAL_NOTEBOOK_ID`
  - Notebook global default untuk command WhatsApp.
- `NOTEBOOKLM_WA_ADMINS`
  - Admin whitelist untuk `/nb use <id>`.

## Apakah `notebooklm login` masih perlu?

**Ya, tetap perlu sekali** untuk mendapatkan session cookie, kecuali Anda mengisi `NOTEBOOKLM_AUTH_JSON`.

Praktik yang direkomendasikan:

1. Login sekali di host:
   ```bash
   notebooklm login
   ```
2. Mount direktori auth ke container via `NOTEBOOKLM_HOME_HOST`.
3. Jalankan bridge via Docker tanpa perlu login ulang tiap start.

## Mode Full Docker (opsional)

Kalau mau bridge juga di container:

```bash
./scripts/whatsapp-bridge-docker.sh up-full
```

## Command Operasional

```bash
# Stop semua service
./scripts/whatsapp-bridge-docker.sh down

# Restart clean
./scripts/whatsapp-bridge-docker.sh restart

# Log service spesifik
./scripts/whatsapp-bridge-docker.sh logs whatsapp_go
./scripts/whatsapp-bridge-docker.sh logs notebooklm_bridge
```

## Catatan Best Practice

- Simpan `WA_WEBHOOK_SECRET` panjang dan random.
- Jangan commit `.env.whatsapp` ke git.
- Gunakan `NOTEBOOKLM_WA_ADMINS` agar `/nb use` tidak bisa dipakai semua nomor.
- Untuk produksi, taruh reverse proxy + TLS di depan port `8781` dan `8787`.
