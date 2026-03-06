"""WhatsApp webhook bridge for NotebookLM.

This module provides an HTTP webhook server that receives WhatsApp events
from go-whatsapp-web-multidevice, processes user messages with NotebookLM,
and sends replies back through the GoWA REST API.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import re
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from time import time
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from .client import NotebookLMClient
from .exceptions import NotebookLMError
from .paths import get_home_dir

logger = logging.getLogger(__name__)

_DEFAULT_STATE_FILENAME = "whatsapp_bridge_state.json"
_DEFAULT_WEBHOOK_PATH = "/webhook/whatsapp"
_DEFAULT_HEALTH_PATH = "/healthz"
_DEFAULT_MAX_REPLY_CHARS = 3000
_DEFAULT_DEDUP_TTL_SECONDS = 900
_DEFAULT_MAX_WEBHOOK_BODY_BYTES = 2 * 1024 * 1024
_DEFAULT_REPLY_MAX_SENTENCES = 2
_DEFAULT_REPLY_TARGET_CHARS = 480


@dataclass
class WhatsAppBridgeConfig:
    """Runtime configuration for the WhatsApp bridge."""

    host: str = "0.0.0.0"
    port: int = 8787
    webhook_path: str = _DEFAULT_WEBHOOK_PATH
    webhook_secret: str = ""
    gowa_base_url: str = "http://127.0.0.1:8781"
    gowa_basic_auth: str | None = None
    global_notebook_id: str | None = None
    admin_numbers: tuple[str, ...] = ()
    allow_groups: bool = False
    max_reply_chars: int = _DEFAULT_MAX_REPLY_CHARS
    reply_max_sentences: int = _DEFAULT_REPLY_MAX_SENTENCES
    reply_target_chars: int = _DEFAULT_REPLY_TARGET_CHARS
    queue_size: int = 1024
    dedup_ttl_seconds: int = _DEFAULT_DEDUP_TTL_SECONDS
    gowa_timeout_seconds: float = 10.0
    health_path: str = _DEFAULT_HEALTH_PATH

    def normalized_webhook_path(self) -> str:
        """Return normalized webhook path with leading slash."""
        path = self.webhook_path.strip() or _DEFAULT_WEBHOOK_PATH
        if not path.startswith("/"):
            path = f"/{path}"
        return path

    def normalized_health_path(self) -> str:
        """Return normalized health check path with leading slash."""
        path = self.health_path.strip() or _DEFAULT_HEALTH_PATH
        if not path.startswith("/"):
            path = f"/{path}"
        return path

    def normalized_admins(self) -> set[str]:
        """Return normalized admin JIDs."""
        return {_normalize_jid(v) for v in self.admin_numbers if _normalize_jid(v)}

    def gowa_send_message_url(self) -> str:
        """Return full GoWA send message endpoint URL."""
        return f"{self.gowa_base_url.rstrip('/')}/send/message"

    @classmethod
    def from_env(cls) -> "WhatsAppBridgeConfig":
        """Create config from NOTEBOOKLM_WA_* environment variables."""
        host = os.environ.get("NOTEBOOKLM_WA_HOST", "0.0.0.0").strip() or "0.0.0.0"
        port = _parse_int_env("NOTEBOOKLM_WA_PORT", 8787)
        webhook_path = os.environ.get("NOTEBOOKLM_WA_PATH", _DEFAULT_WEBHOOK_PATH)
        webhook_secret = os.environ.get("NOTEBOOKLM_WA_WEBHOOK_SECRET", "").strip()
        gowa_base_url = (
            os.environ.get("NOTEBOOKLM_WA_URL")
            or os.environ.get("NOTEBOOKLM_WA_GOWA_BASE_URL")
            or "http://127.0.0.1:8781"
        ).strip()
        gowa_basic_auth = os.environ.get("NOTEBOOKLM_WA_GOWA_BASIC_AUTH")
        global_notebook_id = os.environ.get("NOTEBOOKLM_WA_GLOBAL_NOTEBOOK_ID")
        admins_raw = os.environ.get("NOTEBOOKLM_WA_ADMINS", "").strip()
        admin_numbers = tuple(v.strip() for v in admins_raw.split(",") if v.strip())
        allow_groups = _parse_bool_env("NOTEBOOKLM_WA_ALLOW_GROUPS", False)
        max_reply_chars = _parse_int_env("NOTEBOOKLM_WA_MAX_REPLY_CHARS", _DEFAULT_MAX_REPLY_CHARS)
        reply_max_sentences = _parse_int_env(
            "NOTEBOOKLM_WA_REPLY_MAX_SENTENCES",
            _DEFAULT_REPLY_MAX_SENTENCES,
        )
        reply_target_chars = _parse_int_env(
            "NOTEBOOKLM_WA_REPLY_TARGET_CHARS",
            _DEFAULT_REPLY_TARGET_CHARS,
        )

        return cls(
            host=host,
            port=port,
            webhook_path=webhook_path,
            webhook_secret=webhook_secret,
            gowa_base_url=gowa_base_url,
            gowa_basic_auth=gowa_basic_auth,
            global_notebook_id=global_notebook_id.strip() if global_notebook_id else None,
            admin_numbers=admin_numbers,
            allow_groups=allow_groups,
            max_reply_chars=max_reply_chars,
            reply_max_sentences=reply_max_sentences,
            reply_target_chars=reply_target_chars,
        )


@dataclass
class WhatsAppBridgeState:
    """Persistent state for bridge runtime."""

    global_notebook_id: str | None = None
    conversations: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, *, default_notebook_id: str | None = None) -> "WhatsAppBridgeState":
        """Load bridge state from disk."""
        if not path.exists():
            return cls(global_notebook_id=default_notebook_id)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load bridge state from %s: %s", path, exc)
            return cls(global_notebook_id=default_notebook_id)

        global_notebook_id = raw.get("global_notebook_id")
        if not isinstance(global_notebook_id, str) or not global_notebook_id.strip():
            global_notebook_id = default_notebook_id
        else:
            global_notebook_id = global_notebook_id.strip()

        conversations = raw.get("conversations")
        if not isinstance(conversations, dict):
            conversations = {}
        else:
            conversations = {
                str(k): str(v)
                for k, v in conversations.items()
                if isinstance(k, str) and isinstance(v, str)
            }

        return cls(global_notebook_id=global_notebook_id, conversations=conversations)

    def save(self, path: Path) -> None:
        """Persist state to disk."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {
            "global_notebook_id": self.global_notebook_id,
            "conversations": self.conversations,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            logger.debug("Unable to chmod state file %s", path, exc_info=True)


class WhatsAppNotebookLMBridge:
    """Bridge service that wires WhatsApp webhook events to NotebookLM."""

    def __init__(
        self,
        config: WhatsAppBridgeConfig,
        *,
        storage_path: Path | None = None,
        state_path: Path | None = None,
    ):
        self.config = config
        self._storage_path = storage_path
        self._state_path = state_path or (get_home_dir(create=True) / _DEFAULT_STATE_FILENAME)
        self._state = WhatsAppBridgeState.load(
            self._state_path,
            default_notebook_id=self.config.global_notebook_id,
        )
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=self.config.queue_size
        )
        self._stop_event = Event()
        self._worker_thread: Thread | None = None
        self._seen_message_ids: dict[str, float] = {}
        self._seen_lock = Lock()
        self._sent_message_ids: dict[str, dict[str, float]] = {}
        self._sent_lock = Lock()
        self._state_lock = Lock()
        self._admins = self.config.normalized_admins()
        self._gowa_auth_header = _normalize_basic_auth(self.config.gowa_basic_auth)
        self._http_client = httpx.Client(timeout=self.config.gowa_timeout_seconds)
        self._gowa_device_cache: dict[str, str] = {}
        self._gowa_device_jid_by_id: dict[str, str] = {}

    @property
    def state(self) -> WhatsAppBridgeState:
        """Expose current state for tests and observability."""
        return self._state

    def run_forever(self) -> None:
        """Start worker and HTTP server."""
        self.start()
        webhook_path = self.config.normalized_webhook_path()
        address = (self.config.host, self.config.port)

        server = _BridgeHTTPServer(
            address,
            _BridgeHTTPRequestHandler,
            bridge=self,
            webhook_path=webhook_path,
            health_path=self.config.normalized_health_path(),
        )

        logger.info(
            "WhatsApp bridge listening on http://%s:%d%s",
            self.config.host,
            self.config.port,
            webhook_path,
        )
        logger.info(
            "Health check available on http://%s:%d%s",
            self.config.host,
            self.config.port,
            self.config.normalized_health_path(),
        )

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Received interrupt signal, shutting down bridge...")
        finally:
            server.shutdown()
            server.server_close()
            self.stop()

    def start(self) -> None:
        """Start background worker."""
        if self._worker_thread and self._worker_thread.is_alive():
            return

        self._stop_event.clear()
        self._worker_thread = Thread(target=self._worker_loop, name="wa-bridge-worker", daemon=True)
        self._worker_thread.start()

    def stop(self) -> None:
        """Stop worker and close HTTP resources."""
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # Best effort shutdown signal.
            pass

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5)
        self._worker_thread = None
        self._http_client.close()

    def handle_webhook_request(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> tuple[int, str]:
        """Validate and enqueue incoming webhook request."""
        if not self.config.webhook_secret:
            logger.error("Webhook secret is empty; refusing all requests.")
            return 503, "bridge is not configured with webhook secret"

        signature = headers.get("X-Hub-Signature-256", "")
        if not self._verify_signature(signature, raw_body):
            return 401, "invalid webhook signature"

        try:
            event = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, "invalid JSON payload"

        if not isinstance(event, dict):
            return 400, "payload must be a JSON object"

        try:
            self._queue.put_nowait(event)
        except queue.Full:
            return 503, "event queue is full"

        return 202, "accepted"

    def _verify_signature(self, signature_header: str, body: bytes) -> bool:
        """Verify X-Hub-Signature-256 using HMAC SHA256."""
        if not signature_header:
            return False

        received = signature_header.strip()
        if received.startswith("sha256="):
            received = received[7:]

        expected = hmac.new(
            self.config.webhook_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, received)

    def _worker_loop(self) -> None:
        """Process queued events asynchronously."""
        while not self._stop_event.is_set():
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if event is None:
                self._queue.task_done()
                break

            try:
                self._process_event(event)
            except Exception:
                logger.exception("Failed processing webhook event")
            finally:
                self._queue.task_done()

    def _process_event(self, event: dict[str, Any]) -> None:
        """Process one event payload."""
        if event.get("event") != "message":
            return

        payload = event.get("payload")
        if not isinstance(payload, dict):
            return

        if bool(payload.get("is_from_me")):
            return

        body = payload.get("body")
        if not isinstance(body, str) or not body.strip():
            return

        chat_id = _coerce_str(payload.get("chat_id")) or _coerce_str(payload.get("from"))
        sender = _normalize_jid(_coerce_str(payload.get("from")))
        incoming_id = _coerce_str(payload.get("id"))
        device_id = _coerce_str(event.get("device_id"))
        is_group = bool(payload.get("is_group")) or chat_id.endswith("@g.us")

        if not chat_id or not sender:
            return
        if is_group and not self.config.allow_groups:
            return
        if incoming_id and self._is_duplicate_message(incoming_id):
            return

        context = {
            "chat_id": chat_id,
            "sender": sender,
            "incoming_id": incoming_id,
            "device_id": device_id,
        }
        chat_key = self._chat_key(device_id, chat_id)

        text = body.strip()
        if is_group and not self._should_reply_group_message(
            payload,
            device_id,
            chat_id,
            text,
            chat_key,
        ):
            logger.debug(
                "Skip group message id=%s chat=%s: no mention/reply trigger",
                incoming_id,
                chat_id,
            )
            return

        self._send_chat_presence(context, "start")
        if text.lower().startswith("/nb"):
            try:
                reply = self._handle_nb_command(text, context)
            finally:
                self._send_chat_presence(context, "stop")
        else:
            try:
                reply = self._ask_notebook_reply(text, context)
            finally:
                self._send_chat_presence(context, "stop")

        if reply:
            self._send_reply(context, reply)

    def _should_reply_group_message(
        self,
        payload: dict[str, Any],
        device_id: str,
        chat_id: str,
        text: str,
        chat_key: str,
    ) -> bool:
        """Return True when group message explicitly mentions the AI number."""
        target_jid = self._resolve_ai_jid(device_id)
        target_phone = _jid_user(target_jid)

        replied_to_id = _extract_replied_to_message_id(payload)
        if replied_to_id:
            if self._is_reply_to_ai_message(chat_key, replied_to_id):
                return True
            if self._is_reply_to_ai_message_in_gowa_history(
                device_id=device_id,
                chat_id=chat_id,
                chat_key=chat_key,
                message_id=replied_to_id,
            ):
                return True

        if not target_jid or not target_phone:
            return False

        target_variants = _phone_variants(target_phone)

        for key in ("mentioned_jids", "mentions", "mentioned"):
            values = payload.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                mentioned_jid = _normalize_jid(_extract_mention_jid(value))
                if mentioned_jid == target_jid:
                    return True
                mentioned_phone = _jid_user(mentioned_jid)
                if mentioned_phone and mentioned_phone in target_variants:
                    return True

        for mention in _extract_mentioned_phones_from_text(text):
            if mention in target_variants:
                return True

        return False

    def _resolve_ai_jid(self, incoming_device_id: str) -> str:
        """Resolve AI JID for current device from webhook device identifier."""
        candidate = _coerce_str(incoming_device_id)
        if not candidate:
            return ""

        if "@" in candidate:
            return _normalize_jid(candidate)

        cached = self._gowa_device_jid_by_id.get(candidate)
        if cached:
            return cached

        self._refresh_gowa_device_cache()
        return self._gowa_device_jid_by_id.get(candidate, "")

    def _handle_nb_command(self, text: str, context: dict[str, str]) -> str:
        """Handle /nb command family."""
        parts = text.strip().split(maxsplit=2)
        if len(parts) == 1:
            return self._help_text()

        subcommand = parts[1].lower()
        if subcommand == "help":
            return self._help_text()

        if subcommand == "current":
            notebook_id = self._state.global_notebook_id
            if notebook_id:
                return f"Notebook global aktif: {notebook_id}"
            return (
                "Notebook belum diset. Admin bisa set dengan `/nb use <notebook_id>` "
                "atau set `NOTEBOOKLM_WA_GLOBAL_NOTEBOOK_ID`."
            )

        if subcommand == "reset":
            chat_key = self._chat_key(context["device_id"], context["chat_id"])
            with self._state_lock:
                self._state.conversations.pop(chat_key, None)
                self._save_state_unlocked()
            return "Context percakapan direset untuk chat ini."

        if subcommand == "use":
            if not self._is_admin(context["sender"]):
                return "Perintah ini khusus admin whitelist."
            if len(parts) < 3 or not parts[2].strip():
                return "Gunakan: `/nb use <notebook_id>`."

            target = parts[2].strip()
            try:
                resolved = asyncio.run(self._resolve_notebook_id(target))
            except Exception as exc:
                return f"Gagal set notebook: {exc}"

            with self._state_lock:
                self._state.global_notebook_id = resolved
                self._save_state_unlocked()
            return f"Notebook global berhasil diubah ke: {resolved}"

        if subcommand == "ask":
            if len(parts) < 3 or not parts[2].strip():
                return "Gunakan: `/nb ask <pertanyaan>`."
            return self._ask_notebook_reply(parts[2].strip(), context)

        return self._help_text()

    def _help_text(self) -> str:
        """Return compact command help text."""
        return (
            "Perintah NotebookLM:\n"
            "- /nb help\n"
            "- /nb current\n"
            "- /nb ask <pertanyaan>\n"
            "- /nb reset\n"
            "- /nb use <notebook_id> (admin)"
        )

    def _ask_notebook_reply(self, question: str, context: dict[str, str]) -> str:
        """Ask NotebookLM and return answer text."""
        notebook_id = self._state.global_notebook_id
        if not notebook_id:
            return (
                "Notebook belum diset. Minta admin jalankan `/nb use <notebook_id>` "
                "atau set `NOTEBOOKLM_WA_GLOBAL_NOTEBOOK_ID`."
            )

        chat_key = self._chat_key(context["device_id"], context["chat_id"])
        conversation_id = self._state.conversations.get(chat_key)
        effective_question = _build_whatsapp_question(question)

        try:
            answer, new_conv_id, resolved_notebook_id = asyncio.run(
                self._ask_notebook(effective_question, notebook_id, conversation_id)
            )
        except NotebookLMError as exc:
            logger.warning("NotebookLM error while answering chat: %s", exc)
            return f"NotebookLM error: {exc}"
        except Exception as exc:
            logger.exception("Unexpected error while asking NotebookLM")
            return f"Terjadi error internal saat menghubungi NotebookLM: {exc}"

        with self._state_lock:
            if resolved_notebook_id and resolved_notebook_id != self._state.global_notebook_id:
                self._state.global_notebook_id = resolved_notebook_id
            if new_conv_id:
                self._state.conversations[chat_key] = new_conv_id
            self._save_state_unlocked()

        answer = _format_whatsapp_answer(
            answer,
            max_sentences=self.config.reply_max_sentences,
            target_chars=self.config.reply_target_chars,
        )
        if answer:
            return answer
        return "NotebookLM tidak mengembalikan jawaban."

    async def _ask_notebook(
        self,
        question: str,
        notebook_id: str,
        conversation_id: str | None,
    ) -> tuple[str, str | None, str]:
        """Execute ask call against NotebookLM."""
        storage_arg = str(self._storage_path) if self._storage_path else None
        async with await NotebookLMClient.from_storage(storage_arg) as client:
            resolved = await self._resolve_partial_notebook_id(client, notebook_id)
            result = await client.chat.ask(
                resolved,
                question,
                conversation_id=conversation_id,
            )
            return result.answer, result.conversation_id, resolved

    async def _resolve_notebook_id(self, notebook_id: str) -> str:
        """Resolve notebook ID (supports partial IDs)."""
        storage_arg = str(self._storage_path) if self._storage_path else None
        async with await NotebookLMClient.from_storage(storage_arg) as client:
            return await self._resolve_partial_notebook_id(client, notebook_id)

    async def _resolve_partial_notebook_id(self, client: NotebookLMClient, notebook_id: str) -> str:
        """Resolve full notebook ID from partial prefix."""
        candidate = notebook_id.strip()
        if len(candidate) >= 20:
            return candidate

        notebooks = await client.notebooks.list()
        matches = [nb.id for nb in notebooks if nb.id.lower().startswith(candidate.lower())]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(
                f"Tidak ada notebook dengan prefix '{candidate}'. "
                "Periksa ID dengan `notebooklm list`."
            )
        raise ValueError(f"Prefix notebook '{candidate}' ambigu ({len(matches)} match).")

    def _send_reply(self, context: dict[str, str], reply_text: str) -> None:
        """Send reply to GoWA send/message endpoint."""
        chunks = _split_text(reply_text, max_chars=self.config.max_reply_chars)
        send_url = self.config.gowa_send_message_url()
        chat_key = self._chat_key(context.get("device_id", ""), context["chat_id"])
        for index, chunk in enumerate(chunks):
            request_body: dict[str, Any] = {
                "phone": context["chat_id"],
                "message": chunk,
            }
            if index == 0 and context.get("incoming_id"):
                request_body["reply_message_id"] = context["incoming_id"]

            headers = {"Content-Type": "application/json"}
            if self._gowa_auth_header:
                headers["Authorization"] = self._gowa_auth_header

            resolved_device_id = self._resolve_gowa_device_id(context.get("device_id", ""))
            if resolved_device_id:
                headers["X-Device-Id"] = resolved_device_id

            try:
                response = self._http_client.post(send_url, json=request_body, headers=headers)
                if (
                    response.status_code == 404
                    and "X-Device-Id" in headers
                    and _extract_error_code(response) == "DEVICE_NOT_FOUND"
                ):
                    # Fallback for single-device mode when webhook carries JID instead of UUID.
                    fallback_headers = dict(headers)
                    fallback_headers.pop("X-Device-Id", None)
                    response = self._http_client.post(
                        send_url,
                        json=request_body,
                        headers=fallback_headers,
                    )
                response.raise_for_status()
                sent_message_id = _extract_sent_message_id(response)
                if sent_message_id:
                    self._remember_ai_sent_message(chat_key, sent_message_id)
            except httpx.HTTPStatusError as exc:
                error_body = ""
                try:
                    error_body = exc.response.text
                except Exception:
                    error_body = "<unavailable>"
                logger.error(
                    "Failed sending reply to GoWA (%s): status=%s body=%s",
                    send_url,
                    exc.response.status_code,
                    error_body,
                )
                return
            except Exception as exc:
                logger.error("Failed sending reply to GoWA (%s): %s", send_url, exc)
                return

    def _remember_ai_sent_message(self, chat_key: str, message_id: str) -> None:
        """Track AI outgoing message IDs for reply-to detection in groups."""
        message_id_key = message_id.strip().lower()
        if not message_id_key:
            return

        now = time()
        ttl = max(self.config.dedup_ttl_seconds, 60)
        with self._sent_lock:
            self._prune_sent_message_ids_unlocked(now, ttl)
            bucket = self._sent_message_ids.setdefault(chat_key, {})
            bucket[message_id_key] = now

    def _is_reply_to_ai_message(self, chat_key: str, message_id: str) -> bool:
        """Return True if replied message ID belongs to a recent AI response."""
        message_id_key = message_id.strip().lower()
        if not message_id_key:
            return False

        now = time()
        ttl = max(self.config.dedup_ttl_seconds, 60)
        with self._sent_lock:
            self._prune_sent_message_ids_unlocked(now, ttl)
            bucket = self._sent_message_ids.get(chat_key, {})
            return message_id_key in bucket

    def _prune_sent_message_ids_unlocked(self, now: float, ttl: int) -> None:
        """Prune expired AI sent message IDs. Caller must hold _sent_lock."""
        stale_chat_keys: list[str] = []
        for chat_key, bucket in self._sent_message_ids.items():
            stale_ids = [msg_id for msg_id, ts in bucket.items() if now - ts > ttl]
            for msg_id in stale_ids:
                bucket.pop(msg_id, None)
            if not bucket:
                stale_chat_keys.append(chat_key)

        for chat_key in stale_chat_keys:
            self._sent_message_ids.pop(chat_key, None)

    def _send_chat_presence(self, context: dict[str, str], action: str) -> None:
        """Send typing indicator (start/stop) to GoWA."""
        if action not in {"start", "stop"}:
            return

        presence_url = f"{self.config.gowa_base_url.rstrip('/')}/send/chat-presence"
        headers = {"Content-Type": "application/json"}
        if self._gowa_auth_header:
            headers["Authorization"] = self._gowa_auth_header

        resolved_device_id = self._resolve_gowa_device_id(context.get("device_id", ""))
        if resolved_device_id:
            headers["X-Device-Id"] = resolved_device_id

        request_body = {
            "phone": context["chat_id"],
            "action": action,
        }

        try:
            response = self._http_client.post(presence_url, json=request_body, headers=headers)
            if (
                response.status_code == 404
                and "X-Device-Id" in headers
                and _extract_error_code(response) == "DEVICE_NOT_FOUND"
            ):
                fallback_headers = dict(headers)
                fallback_headers.pop("X-Device-Id", None)
                response = self._http_client.post(
                    presence_url,
                    json=request_body,
                    headers=fallback_headers,
                )
            response.raise_for_status()
        except Exception:
            logger.debug("Failed sending GoWA chat presence action=%s", action, exc_info=True)

    def _resolve_gowa_device_id(self, incoming_device_id: str) -> str | None:
        """Resolve webhook device identifier to GoWA API device ID."""
        candidate = _coerce_str(incoming_device_id)
        if not candidate:
            return None

        # If it does not look like JID, assume it's already a GoWA device ID.
        if "@" not in candidate:
            return candidate

        normalized = _normalize_jid(candidate)
        cached = self._gowa_device_cache.get(candidate) or self._gowa_device_cache.get(normalized)
        if cached:
            return cached

        self._refresh_gowa_device_cache()
        return self._gowa_device_cache.get(candidate) or self._gowa_device_cache.get(normalized)

    def _is_reply_to_ai_message_in_gowa_history(
        self,
        *,
        device_id: str,
        chat_id: str,
        chat_key: str,
        message_id: str,
    ) -> bool:
        """Check GoWA chat history to detect reply target sent by this AI."""
        target_id = message_id.strip().lower()
        if not target_id:
            return False

        chat_jid = chat_id.strip()
        if not chat_jid:
            return False

        history_url = f"{self.config.gowa_base_url.rstrip('/')}/chat/{chat_jid}/messages"
        headers: dict[str, str] = {}
        if self._gowa_auth_header:
            headers["Authorization"] = self._gowa_auth_header

        resolved_device_id = self._resolve_gowa_device_id(device_id)
        if resolved_device_id:
            headers["X-Device-Id"] = resolved_device_id

        params = {
            "limit": 80,
            "offset": 0,
            "is_from_me": "true",
        }

        try:
            response = self._http_client.get(history_url, params=params, headers=headers)
            if (
                response.status_code == 404
                and "X-Device-Id" in headers
                and _extract_error_code(response) == "DEVICE_NOT_FOUND"
            ):
                fallback_headers = dict(headers)
                fallback_headers.pop("X-Device-Id", None)
                response = self._http_client.get(history_url, params=params, headers=fallback_headers)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            logger.debug(
                "Failed checking GoWA history for reply detection chat=%s id=%s",
                chat_jid,
                message_id,
                exc_info=True,
            )
            return False

        messages = _extract_chat_messages(payload)
        for item in messages:
            msg_id = (
                _coerce_str(item.get("id"))
                or _coerce_str(item.get("message_id"))
                or _coerce_str(item.get("msg_id"))
            ).lower()
            if msg_id == target_id:
                self._remember_ai_sent_message(chat_key, msg_id)
                return True
        return False

    def _refresh_gowa_device_cache(self) -> None:
        """Refresh GoWA device cache from /devices endpoint."""
        devices_url = f"{self.config.gowa_base_url.rstrip('/')}/devices"
        headers: dict[str, str] = {}
        if self._gowa_auth_header:
            headers["Authorization"] = self._gowa_auth_header

        try:
            response = self._http_client.get(devices_url, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            logger.debug("Unable to refresh GoWA device cache from %s", devices_url, exc_info=True)
            return

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return

        for item in results:
            if not isinstance(item, dict):
                continue

            device_id = _coerce_str(item.get("id"))
            if not device_id:
                continue
            self._gowa_device_cache[device_id] = device_id

            jid = _coerce_str(item.get("jid"))
            if jid:
                self._gowa_device_cache[jid] = device_id
                normalized_jid = _normalize_jid(jid)
                if normalized_jid:
                    self._gowa_device_cache[normalized_jid] = device_id
                    self._gowa_device_jid_by_id[device_id] = normalized_jid

    def _is_admin(self, sender: str) -> bool:
        """Check whether sender belongs to admin whitelist."""
        if not self._admins:
            return False
        return _normalize_jid(sender) in self._admins

    def _chat_key(self, device_id: str, chat_id: str) -> str:
        """Build stable key for per-chat conversation context."""
        if device_id:
            return f"{device_id}|{chat_id}"
        return chat_id

    def _is_duplicate_message(self, message_id: str) -> bool:
        """Return True for duplicate message IDs within TTL window."""
        now = time()
        ttl = max(self.config.dedup_ttl_seconds, 1)
        with self._seen_lock:
            expired = [k for k, ts in self._seen_message_ids.items() if now - ts > ttl]
            for key in expired:
                self._seen_message_ids.pop(key, None)

            if message_id in self._seen_message_ids:
                return True
            self._seen_message_ids[message_id] = now
            return False

    def _save_state_unlocked(self) -> None:
        """Persist state. Caller must hold _state_lock."""
        try:
            self._state.save(self._state_path)
        except OSError as exc:
            logger.warning("Failed saving bridge state to %s: %s", self._state_path, exc)


class _BridgeHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying bridge runtime references."""

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        bridge: WhatsAppNotebookLMBridge,
        webhook_path: str,
        health_path: str,
    ):
        super().__init__(server_address, request_handler_class)
        self.bridge = bridge
        self.webhook_path = webhook_path
        self.health_path = health_path


class _BridgeHTTPRequestHandler(BaseHTTPRequestHandler):
    """HTTP handlers for webhook and health endpoints."""

    server: _BridgeHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Handle health checks."""
        path = urlparse(self.path).path
        if path != self.server.health_path:
            self._send_json(404, {"error": "not found"})
            return
        self._send_json(200, {"status": "ok"})

    def do_POST(self) -> None:
        """Handle webhook events."""
        path = urlparse(self.path).path
        if path != self.server.webhook_path:
            self._send_json(404, {"error": "not found"})
            return

        try:
            raw_body = _read_http_request_body(
                self.headers,
                self.rfile,
                max_bytes=_DEFAULT_MAX_WEBHOOK_BODY_BYTES,
            )
        except ValueError:
            self._send_json(400, {"error": "invalid request body"})
            return

        status, message = self.server.bridge.handle_webhook_request(self.headers, raw_body)
        self._send_json(status, {"message": message})

    def log_message(self, fmt: str, *args: Any) -> None:
        """Route handler logs through module logger."""
        logger.debug("Webhook HTTP: " + fmt, *args)

    def _send_json(self, status_code: int, data: dict[str, Any]) -> None:
        """Write JSON response."""
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _parse_bool_env(name: str, default: bool) -> bool:
    """Parse environment variable into bool."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int_env(name: str, default: int) -> int:
    """Parse environment variable into int."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("Invalid integer for %s=%r, using default=%d", name, raw, default)
        return default


def _normalize_basic_auth(raw: str | None) -> str | None:
    """Normalize basic auth value to Authorization header format."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if value.lower().startswith("basic "):
        return value
    if ":" in value:
        token = base64.b64encode(value.encode("utf-8")).decode("utf-8")
        return f"Basic {token}"
    return value


def _coerce_str(value: Any) -> str:
    """Convert value to stripped string safely."""
    if isinstance(value, str):
        return value.strip()
    return ""


def _normalize_jid(value: str) -> str:
    """Normalize sender/admin value into canonical WhatsApp JID."""
    normalized = value.strip().lower()
    if not normalized:
        return ""
    normalized = re.sub(r":\d+(?=@s\.whatsapp\.net$)", "", normalized)
    if "@" not in normalized:
        normalized = f"{normalized}@s.whatsapp.net"
    return normalized


def _jid_user(jid: str) -> str:
    """Extract user/phone part from JID."""
    if "@" not in jid:
        return jid.strip()
    return jid.split("@", 1)[0].strip()


def _extract_mention_jid(value: Any) -> str:
    """Extract mention JID string from webhook mention value."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("jid", "id", "phone", "value"):
            extracted = _coerce_str(value.get(key))
            if extracted:
                return extracted
    return ""


def _extract_mentioned_phones_from_text(text: str) -> set[str]:
    """Extract numeric mention targets from text like @62812xxxx."""
    phones: set[str] = set()
    for token in re.findall(r"@([^\s@]+)", text):
        digits = re.sub(r"\D", "", token)
        if len(digits) >= 6:
            phones.add(digits)
    return phones


def _phone_variants(phone: str) -> set[str]:
    """Build normalized phone variants for matching mention formats."""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return set()
    variants = {digits}
    if digits.startswith("62") and len(digits) > 2:
        variants.add(f"0{digits[2:]}")
    if digits.startswith("0") and len(digits) > 1:
        variants.add(f"62{digits[1:]}")
    return variants


def _split_text(text: str, *, max_chars: int) -> list[str]:
    """Split long text into WhatsApp-friendly chunks."""
    message = text.strip()
    if not message:
        return []

    max_chars = max(200, max_chars)
    if len(message) <= max_chars:
        return [message]

    chunks: list[str] = []
    remaining = message
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break

        split_idx = remaining.rfind("\n", 0, max_chars)
        if split_idx < max_chars // 2:
            split_idx = remaining.rfind(" ", 0, max_chars)
        if split_idx <= 0:
            split_idx = max_chars

        chunks.append(remaining[:split_idx].strip())
        remaining = remaining[split_idx:].strip()

    return [chunk for chunk in chunks if chunk]


def _build_whatsapp_question(question: str) -> str:
    """Attach lightweight formatting instructions for WhatsApp output."""
    base = question.strip()
    if not base:
        return question
    return (
        f"{base}\n\n"
        "Format jawaban WhatsApp: ringkas maksimal 2 kalimat, langsung ke inti, "
        "tanpa catatan kaki atau daftar referensi."
    )


def _format_whatsapp_answer(text: str, *, max_sentences: int, target_chars: int) -> str:
    """Normalize answer text for concise WhatsApp delivery."""
    cleaned = _strip_footnotes_and_references(text or "")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\s+([.,!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if not cleaned:
        return ""

    max_sentences = max(1, max_sentences)
    target_chars = max(120, target_chars)

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    if len(sentences) > max_sentences:
        cleaned = " ".join(sentences[:max_sentences]).strip()

    if len(cleaned) > target_chars:
        clipped = cleaned[:target_chars].rstrip()
        if " " in clipped:
            clipped = clipped.rsplit(" ", 1)[0]
        cleaned = clipped.rstrip(".,;: ") + "..."

    return cleaned.strip()


def _strip_footnotes_and_references(text: str) -> str:
    """Remove citation markers and reference blocks from model answer."""
    output = text
    output = re.sub(r"【\d+[^】]*】", "", output)
    output = re.sub(r"\[\^?\d+(?:\s*,\s*\d+)*\]", "", output)
    output = re.sub(r"\(\s*sumber\s*:[^)]+\)", "", output, flags=re.IGNORECASE)
    output = re.sub(r"\(\s*source\s*:[^)]+\)", "", output, flags=re.IGNORECASE)
    output = re.sub(r"^\[\^.+\]:.*$", "", output, flags=re.MULTILINE)

    filtered_lines: list[str] = []
    for line in output.splitlines():
        lowered = line.strip().lower()
        if lowered.startswith(
            (
                "sumber:",
                "referensi:",
                "references:",
                "catatan kaki:",
                "footnotes:",
            )
        ):
            continue
        filtered_lines.append(line)

    return "\n".join(filtered_lines).strip()


def _extract_error_code(response: httpx.Response) -> str:
    """Extract GoWA JSON error code from response payload."""
    try:
        payload = response.json()
    except ValueError:
        return ""

    if not isinstance(payload, dict):
        return ""
    code = payload.get("code")
    return code.strip() if isinstance(code, str) else ""


def _extract_sent_message_id(response: httpx.Response) -> str | None:
    """Extract sent message ID from GoWA send response payload."""
    try:
        payload = response.json()
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    top_message_id = _coerce_str(payload.get("message_id")) or _coerce_str(payload.get("id"))
    if top_message_id:
        return top_message_id

    results = payload.get("results")
    if isinstance(results, dict):
        message_payload = results.get("message")
        nested_message_id = (
            _coerce_str(message_payload.get("id")) if isinstance(message_payload, dict) else ""
        )
        message_id = (
            _coerce_str(results.get("message_id"))
            or _coerce_str(results.get("id"))
            or nested_message_id
        )
        if message_id:
            return message_id

    return None


def _extract_replied_to_message_id(payload: Mapping[str, Any]) -> str:
    """Extract replied message ID from known GoWA webhook payload shapes."""
    direct = (
        _coerce_str(payload.get("replied_to_id"))
        or _coerce_str(payload.get("reply_message_id"))
        or _coerce_str(payload.get("quoted_message_id"))
    )
    if direct:
        return direct

    quoted = payload.get("quoted_message")
    if isinstance(quoted, Mapping):
        quoted_id = _coerce_str(quoted.get("id")) or _coerce_str(quoted.get("message_id"))
        if quoted_id:
            return quoted_id

    for key in ("context", "context_info", "contextInfo", "reply"):
        nested = payload.get(key)
        if not isinstance(nested, Mapping):
            continue
        nested_id = (
            _coerce_str(nested.get("replied_to_id"))
            or _coerce_str(nested.get("reply_message_id"))
            or _coerce_str(nested.get("quoted_message_id"))
            or _coerce_str(nested.get("stanza_id"))
            or _coerce_str(nested.get("stanzaId"))
            or _coerce_str(nested.get("id"))
            or _coerce_str(nested.get("message_id"))
        )
        if nested_id:
            return nested_id

    return ""


def _extract_chat_messages(payload: Any) -> list[dict[str, Any]]:
    """Extract message array from GoWA /chat/:jid/messages response shapes."""
    if not isinstance(payload, dict):
        return []

    results = payload.get("results")
    if isinstance(results, Mapping):
        data = results.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]

    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    return []


def _read_http_request_body(
    headers: Mapping[str, str],
    rfile,
    *,
    max_bytes: int = _DEFAULT_MAX_WEBHOOK_BODY_BYTES,
) -> bytes:
    """Read HTTP request body for content-length or chunked transfer encoding."""
    transfer_encoding = headers.get("Transfer-Encoding", "").strip().lower()
    if "chunked" in transfer_encoding:
        return _read_chunked_body(rfile, max_bytes=max_bytes)

    content_length_raw = headers.get("Content-Length")
    if content_length_raw is None:
        return b""

    try:
        content_length = int(content_length_raw.strip())
    except ValueError as exc:
        raise ValueError("invalid content length") from exc

    if content_length < 0:
        raise ValueError("invalid content length")
    if content_length > max_bytes:
        raise ValueError("payload too large")

    raw_body = rfile.read(content_length)
    if len(raw_body) != content_length:
        raise ValueError("incomplete request body")
    return raw_body


def _read_chunked_body(rfile, *, max_bytes: int) -> bytes:
    """Read chunked-encoding body stream."""
    chunks: list[bytes] = []
    total = 0

    while True:
        line = rfile.readline()
        if not line:
            raise ValueError("invalid chunked body")

        chunk_size_raw = line.strip()
        if not chunk_size_raw:
            continue
        if b";" in chunk_size_raw:
            chunk_size_raw = chunk_size_raw.split(b";", 1)[0]

        try:
            chunk_size = int(chunk_size_raw, 16)
        except ValueError as exc:
            raise ValueError("invalid chunk size") from exc

        if chunk_size < 0:
            raise ValueError("invalid chunk size")

        if chunk_size == 0:
            # Consume optional trailer headers.
            while True:
                trailer = rfile.readline()
                if trailer in (b"", b"\r\n", b"\n"):
                    break
            break

        total += chunk_size
        if total > max_bytes:
            raise ValueError("payload too large")

        chunk = rfile.read(chunk_size)
        if len(chunk) != chunk_size:
            raise ValueError("incomplete chunk body")
        chunks.append(chunk)

        delimiter = rfile.read(2)
        if delimiter == b"\r\n":
            continue
        if delimiter == b"\n":
            continue
        raise ValueError("invalid chunk delimiter")

    return b"".join(chunks)
