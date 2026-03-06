"""Bridge integration commands."""

from pathlib import Path

import click

from ..auth import load_auth_from_storage
from ..whatsapp_bridge import WhatsAppBridgeConfig, WhatsAppNotebookLMBridge
from .helpers import console, get_current_notebook


@click.group()
def bridge():
    """Bridge commands for external integrations."""
    pass


@bridge.command("whatsapp")
@click.option("--host", default=None, help="Webhook bind host (default: env or 0.0.0.0)")
@click.option("--port", type=int, default=None, help="Webhook bind port (default: env or 8787)")
@click.option(
    "--path",
    "webhook_path",
    default=None,
    help="Webhook path (default: env or /webhook/whatsapp)",
)
@click.option(
    "--health-path",
    default=None,
    help="Health check path (default: /healthz)",
)
@click.option(
    "--webhook-secret",
    default=None,
    help="HMAC secret for X-Hub-Signature-256 (default: NOTEBOOKLM_WA_WEBHOOK_SECRET)",
)
@click.option(
    "--url",
    "gowa_url",
    default=None,
    help="URL UI GoWA, contoh: http://127.0.0.1:8781",
)
@click.option(
    "--gowa-base-url",
    default=None,
    hidden=True,
    help="(Deprecated) gunakan --url",
)
@click.option(
    "--gowa-basic-auth",
    default=None,
    help="Basic auth GoWA (user:pass atau header penuh Basic ...)",
)
@click.option(
    "--global-notebook-id",
    default=None,
    help="Notebook global default. Bisa diubah admin via /nb use <id>.",
)
@click.option(
    "--admin",
    "--admin-phone",
    "admins",
    multiple=True,
    help=(
        "Nomor/JID admin whitelist. Bisa diulang atau comma-separated. "
        "Contoh: --admin 62812xxxx atau --admin 62812xxxx,62813yyyy"
    ),
)
@click.option(
    "--allow-groups/--no-allow-groups",
    default=None,
    help="Proses group chat atau tidak.",
)
@click.option(
    "--max-reply-chars",
    type=int,
    default=None,
    help="Maks karakter per pesan balasan WhatsApp.",
)
@click.option(
    "--state-file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Lokasi file state bridge (default: ~/.notebooklm/whatsapp_bridge_state.json).",
)
@click.pass_context
def whatsapp_bridge_cmd(
    ctx,
    host,
    port,
    webhook_path,
    health_path,
    webhook_secret,
    gowa_url,
    gowa_base_url,
    gowa_basic_auth,
    global_notebook_id,
    admins,
    allow_groups,
    max_reply_chars,
    state_file,
):
    """Jalankan webhook bridge WhatsApp -> NotebookLM -> WhatsApp."""
    env_cfg = WhatsAppBridgeConfig.from_env()
    current_notebook = get_current_notebook()

    parsed_admins: tuple[str, ...] = ()
    if admins:
        values: list[str] = []
        for value in admins:
            values.extend(part.strip() for part in value.split(",") if part.strip())
        parsed_admins = tuple(values)

    resolved = WhatsAppBridgeConfig(
        host=host or env_cfg.host,
        port=port or env_cfg.port,
        webhook_path=webhook_path or env_cfg.webhook_path,
        webhook_secret=(webhook_secret if webhook_secret is not None else env_cfg.webhook_secret),
        gowa_base_url=(gowa_url or gowa_base_url or env_cfg.gowa_base_url),
        gowa_basic_auth=(
            gowa_basic_auth if gowa_basic_auth is not None else env_cfg.gowa_basic_auth
        ),
        global_notebook_id=(
            global_notebook_id
            if global_notebook_id is not None
            else (env_cfg.global_notebook_id or current_notebook)
        ),
        admin_numbers=parsed_admins if parsed_admins else env_cfg.admin_numbers,
        allow_groups=allow_groups if allow_groups is not None else env_cfg.allow_groups,
        max_reply_chars=max_reply_chars or env_cfg.max_reply_chars,
        health_path=health_path or env_cfg.health_path,
    )

    if not resolved.webhook_secret:
        raise click.ClickException(
            "Webhook secret wajib diisi. Gunakan --webhook-secret "
            "atau NOTEBOOKLM_WA_WEBHOOK_SECRET."
        )
    if not resolved.gowa_base_url:
        raise click.ClickException(
            "GoWA URL wajib diisi. Gunakan --url "
            "atau NOTEBOOKLM_WA_URL."
        )

    storage_path = ctx.obj.get("storage_path") if ctx.obj else None
    state_path = Path(state_file).expanduser().resolve() if state_file else None

    try:
        # Fail fast kalau auth NotebookLM belum siap.
        load_auth_from_storage(storage_path)
    except Exception as exc:
        raise click.ClickException(
            "Auth NotebookLM tidak valid. Jalankan `notebooklm login` "
            "atau set NOTEBOOKLM_AUTH_JSON."
        ) from exc

    console.print("[bold green]Menjalankan WhatsApp bridge...[/bold green]")
    console.print(f"GoWA UI: [cyan]{resolved.gowa_base_url.rstrip('/')}[/cyan]")
    console.print(f"Webhook: [cyan]{resolved.normalized_webhook_path()}[/cyan]")
    console.print(f"Health : [cyan]{resolved.normalized_health_path()}[/cyan]")
    if resolved.global_notebook_id:
        console.print(f"Notebook global: [cyan]{resolved.global_notebook_id}[/cyan]")
    else:
        console.print(
            "[yellow]Notebook global belum diset. "
            "Admin bisa set via /nb use <id>.[/yellow]"
        )

    bridge = WhatsAppNotebookLMBridge(
        resolved,
        storage_path=storage_path,
        state_path=state_path,
    )
    bridge.run_forever()
