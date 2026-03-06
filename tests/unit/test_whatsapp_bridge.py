"""Unit tests for WhatsApp bridge runtime."""

import hashlib
import hmac
import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from notebooklm.whatsapp_bridge import (
    WhatsAppBridgeConfig,
    WhatsAppNotebookLMBridge,
    _build_whatsapp_question,
    _format_whatsapp_answer,
    _normalize_basic_auth,
    _normalize_jid,
    _read_http_request_body,
    _strip_footnotes_and_references,
    _split_text,
)


def _signature(secret: str, raw_body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture
def bridge(tmp_path: Path) -> WhatsAppNotebookLMBridge:
    cfg = WhatsAppBridgeConfig(
        webhook_secret="secret",
        gowa_base_url="http://localhost:3000",
        admin_numbers=("6281111111111",),
    )
    return WhatsAppNotebookLMBridge(
        cfg,
        state_path=tmp_path / "bridge-state.json",
    )


def test_handle_webhook_request_accepts_valid_signature(bridge: WhatsAppNotebookLMBridge):
    body = {
        "event": "message",
        "device_id": "6281000000000@s.whatsapp.net",
        "payload": {
            "id": "msg-1",
            "chat_id": "6281222222222@s.whatsapp.net",
            "from": "6281222222222@s.whatsapp.net",
            "body": "halo",
        },
    }
    raw = json.dumps(body).encode("utf-8")
    headers = {"X-Hub-Signature-256": _signature("secret", raw)}

    status, message = bridge.handle_webhook_request(headers, raw)
    assert status == 202
    assert message == "accepted"


def test_handle_webhook_request_rejects_invalid_signature(bridge: WhatsAppNotebookLMBridge):
    raw = b'{"event":"message"}'
    headers = {"X-Hub-Signature-256": "sha256=invalid"}

    status, message = bridge.handle_webhook_request(headers, raw)
    assert status == 401
    assert "invalid" in message


def test_nb_use_requires_admin(bridge: WhatsAppNotebookLMBridge):
    response = bridge._handle_nb_command(
        "/nb use abc123",
        {
            "chat_id": "6281222222222@s.whatsapp.net",
            "sender": "6281222222222@s.whatsapp.net",
            "incoming_id": "msg-1",
            "device_id": "6281000000000@s.whatsapp.net",
        },
    )
    assert "khusus admin" in response.lower()


def test_nb_use_admin_updates_global_notebook(monkeypatch, bridge: WhatsAppNotebookLMBridge):
    async def _fake_resolve(_: str) -> str:
        return "nb-full-123"

    monkeypatch.setattr(bridge, "_resolve_notebook_id", _fake_resolve)

    response = bridge._handle_nb_command(
        "/nb use nb",
        {
            "chat_id": "6281222222222@s.whatsapp.net",
            "sender": "6281111111111@s.whatsapp.net",
            "incoming_id": "msg-1",
            "device_id": "6281000000000@s.whatsapp.net",
        },
    )
    assert "berhasil diubah" in response.lower()
    assert bridge.state.global_notebook_id == "nb-full-123"


def test_ask_reply_when_notebook_unset(bridge: WhatsAppNotebookLMBridge):
    bridge.state.global_notebook_id = None
    response = bridge._ask_notebook_reply(
        "halo",
        {
            "chat_id": "6281222222222@s.whatsapp.net",
            "sender": "6281222222222@s.whatsapp.net",
            "incoming_id": "msg-1",
            "device_id": "6281000000000@s.whatsapp.net",
        },
    )
    assert "belum diset" in response.lower()


def test_duplicate_message_detection(bridge: WhatsAppNotebookLMBridge):
    assert bridge._is_duplicate_message("msg-dup-1") is False
    assert bridge._is_duplicate_message("msg-dup-1") is True


def test_send_reply_chunks_and_sets_reply_id(monkeypatch, bridge: WhatsAppNotebookLMBridge):
    bridge.config.max_reply_chars = 220

    calls = []

    class _FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": {"message_id": "out-msg-1"}}

    def _fake_post(url, json, headers):
        calls.append((url, json, headers))
        return _FakeResponse()

    fake_client = MagicMock()
    fake_client.post.side_effect = _fake_post
    monkeypatch.setattr(bridge, "_http_client", fake_client)

    bridge._send_reply(
        {
            "chat_id": "6281222222222@s.whatsapp.net",
            "sender": "6281222222222@s.whatsapp.net",
            "incoming_id": "msg-1",
            "device_id": "device-123",
        },
        " ".join(["jawaban"] * 120),
    )

    assert len(calls) >= 2
    assert calls[0][1]["reply_message_id"] == "msg-1"
    assert "reply_message_id" not in calls[1][1]
    assert calls[0][2]["X-Device-Id"] == "device-123"


def test_send_reply_resolves_jid_device_id(monkeypatch, bridge: WhatsAppNotebookLMBridge):
    calls = []

    class _FakeResponse:
        def __init__(self, payload=None):
            self._payload = payload or {}
            self.status_code = 200
            self.text = json.dumps(self._payload)

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _fake_get(url, headers):
        assert url.endswith("/devices")
        return _FakeResponse(
            {
                "results": [
                    {
                        "id": "device-uuid-1",
                        "jid": "6281000000000@s.whatsapp.net",
                    }
                ]
            }
        )

    def _fake_post(url, json, headers):
        calls.append((url, json, headers))
        return _FakeResponse()

    fake_client = MagicMock()
    fake_client.get.side_effect = _fake_get
    fake_client.post.side_effect = _fake_post
    monkeypatch.setattr(bridge, "_http_client", fake_client)

    bridge._send_reply(
        {
            "chat_id": "6281222222222@s.whatsapp.net",
            "sender": "6281222222222@s.whatsapp.net",
            "incoming_id": "msg-1",
            "device_id": "6281000000000@s.whatsapp.net",
        },
        "halo",
    )

    assert len(calls) == 1
    assert calls[0][2]["X-Device-Id"] == "device-uuid-1"


def test_text_helpers():
    chunks = _split_text("a" * 450, max_chars=200)
    assert len(chunks) == 3
    assert all(chunks)

    assert _normalize_jid("6281234567890") == "6281234567890@s.whatsapp.net"
    assert _normalize_jid("6281234567890:33@s.whatsapp.net") == "6281234567890@s.whatsapp.net"

    assert _normalize_basic_auth("user:pass").startswith("Basic ")
    assert _normalize_basic_auth("Basic abc") == "Basic abc"


def test_read_http_request_body_with_content_length():
    body = b'{"event":"message"}'
    stream = io.BytesIO(body)
    parsed = _read_http_request_body({"Content-Length": str(len(body))}, stream)
    assert parsed == body


def test_read_http_request_body_with_chunked_encoding():
    body = b'{"event":"message","payload":{"body":"halo"}}'
    chunked = (
        f"{len(body):X}\r\n".encode("ascii")
        + body
        + b"\r\n0\r\n\r\n"
    )
    stream = io.BytesIO(chunked)
    parsed = _read_http_request_body({"Transfer-Encoding": "chunked"}, stream)
    assert parsed == body


def test_read_http_request_body_rejects_invalid_chunk():
    stream = io.BytesIO(b"ZZ\r\ninvalid\r\n0\r\n\r\n")
    with pytest.raises(ValueError):
        _read_http_request_body({"Transfer-Encoding": "chunked"}, stream)


def test_format_whatsapp_answer_removes_footnotes_and_shortens():
    raw = (
        "Ini jawaban panjang sekali [1] dengan detail berlebih. "
        "Kalimat kedua tetap relevan. Kalimat ketiga harus terpotong.\n"
        "Sumber: https://contoh.com"
    )
    formatted = _format_whatsapp_answer(raw, max_sentences=2, target_chars=120)
    assert "[1]" not in formatted
    assert "Sumber:" not in formatted
    # Maksimal dua kalimat.
    assert formatted.count(".") <= 2


def test_build_whatsapp_question_adds_format_instruction():
    q = _build_whatsapp_question("Apa update terbaru?")
    assert "Apa update terbaru?" in q
    assert "Format jawaban WhatsApp" in q


def test_strip_footnotes_and_references():
    cleaned = _strip_footnotes_and_references(
        "Ringkasan [2] utama.\nReferensi: buku A\nCatatan kaki: abc"
    )
    assert "[2]" not in cleaned
    assert "Referensi:" not in cleaned


def test_group_message_replies_only_when_mentioned(monkeypatch, bridge: WhatsAppNotebookLMBridge):
    bridge.config.allow_groups = True

    sent = []

    def _fake_presence(_ctx, _action):
        return None

    def _fake_reply(_question, _ctx):
        return "ok"

    def _fake_send(_ctx, reply_text):
        sent.append(reply_text)

    monkeypatch.setattr(bridge, "_send_chat_presence", _fake_presence)
    monkeypatch.setattr(bridge, "_ask_notebook_reply", _fake_reply)
    monkeypatch.setattr(bridge, "_send_reply", _fake_send)
    monkeypatch.setattr(
        bridge,
        "_resolve_ai_jid",
        lambda _device_id: "6281000000000@s.whatsapp.net",
    )

    event_without_mention = {
        "event": "message",
        "device_id": "6281000000000@s.whatsapp.net",
        "payload": {
            "id": "msg-group-1",
            "chat_id": "120363123@g.us",
            "from": "6281222222222@s.whatsapp.net",
            "body": "halo semua",
            "is_from_me": False,
        },
    }
    bridge._process_event(event_without_mention)
    assert sent == []

    event_with_mention = {
        "event": "message",
        "device_id": "6281000000000@s.whatsapp.net",
        "payload": {
            "id": "msg-group-2",
            "chat_id": "120363123@g.us",
            "from": "6281222222222@s.whatsapp.net",
            "body": "halo @6281000000000",
            "is_from_me": False,
        },
    }
    bridge._process_event(event_with_mention)
    assert sent == ["ok"]


def test_group_message_with_uuid_device_id_can_match_mention(monkeypatch, bridge: WhatsAppNotebookLMBridge):
    bridge.config.allow_groups = True
    sent = []

    monkeypatch.setattr(bridge, "_send_chat_presence", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bridge, "_ask_notebook_reply", lambda *_args, **_kwargs: "ok")
    monkeypatch.setattr(bridge, "_send_reply", lambda *_args, **_kwargs: sent.append("ok"))

    def _fake_refresh():
        bridge._gowa_device_jid_by_id["device-uuid-1"] = "6281000000000@s.whatsapp.net"

    monkeypatch.setattr(bridge, "_refresh_gowa_device_cache", _fake_refresh)

    bridge._process_event(
        {
            "event": "message",
            "device_id": "device-uuid-1",
            "payload": {
                "id": "msg-group-uuid-1",
                "chat_id": "120363123@g.us",
                "from": "6281222222222@s.whatsapp.net",
                "body": "tolong cek @6281000000000",
                "is_group": True,
                "is_from_me": False,
            },
        }
    )

    assert sent == ["ok"]


def test_group_message_replies_when_replying_to_ai_message(monkeypatch, bridge: WhatsAppNotebookLMBridge):
    bridge.config.allow_groups = True
    sent = []

    monkeypatch.setattr(bridge, "_send_chat_presence", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bridge, "_ask_notebook_reply", lambda *_args, **_kwargs: "ok")
    monkeypatch.setattr(bridge, "_send_reply", lambda *_args, **_kwargs: sent.append("ok"))
    monkeypatch.setattr(
        bridge,
        "_resolve_ai_jid",
        lambda _device_id: "6281000000000@s.whatsapp.net",
    )

    chat_id = "120363123@g.us"
    device_id = "6281000000000@s.whatsapp.net"
    chat_key = bridge._chat_key(device_id, chat_id)
    bridge._remember_ai_sent_message(chat_key, "ai-msg-1")

    bridge._process_event(
        {
            "event": "message",
            "device_id": device_id,
            "payload": {
                "id": "msg-group-reply-1",
                "chat_id": chat_id,
                "from": "6281222222222@s.whatsapp.net",
                "body": "lanjut ya",
                "replied_to_id": "ai-msg-1",
                "is_group": True,
                "is_from_me": False,
            },
        }
    )

    assert sent == ["ok"]


def test_group_reply_can_use_gowa_history_when_cache_empty(
    monkeypatch, bridge: WhatsAppNotebookLMBridge
):
    bridge.config.allow_groups = True
    sent = []

    monkeypatch.setattr(bridge, "_send_chat_presence", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bridge, "_ask_notebook_reply", lambda *_args, **_kwargs: "ok")
    monkeypatch.setattr(bridge, "_send_reply", lambda *_args, **_kwargs: sent.append("ok"))
    monkeypatch.setattr(
        bridge,
        "_resolve_ai_jid",
        lambda _device_id: "6281000000000@s.whatsapp.net",
    )

    class _FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": {
                    "data": [
                        {
                            "id": "ai-msg-legacy-1",
                            "is_from_me": True,
                        }
                    ]
                }
            }

    calls = []

    def _fake_get(url, params=None, headers=None):
        calls.append((url, params, headers))
        return _FakeResponse()

    fake_client = MagicMock()
    fake_client.get.side_effect = _fake_get
    monkeypatch.setattr(bridge, "_http_client", fake_client)

    bridge._process_event(
        {
            "event": "message",
            "device_id": "device-123",
            "payload": {
                "id": "msg-group-reply-legacy-1",
                "chat_id": "120363123@g.us",
                "from": "6281222222222@s.whatsapp.net",
                "body": "balas ini",
                "replied_to_id": "ai-msg-legacy-1",
                "is_group": True,
                "is_from_me": False,
            },
        }
    )

    assert sent == ["ok"]
    assert len(calls) == 1
    assert "/chat/120363123@g.us/messages" in calls[0][0]
    assert calls[0][1]["is_from_me"] == "true"


def test_process_event_sends_typing_start_and_stop(monkeypatch, bridge: WhatsAppNotebookLMBridge):
    actions = []

    def _fake_presence(_ctx, action):
        actions.append(action)

    monkeypatch.setattr(bridge, "_send_chat_presence", _fake_presence)
    monkeypatch.setattr(bridge, "_ask_notebook_reply", lambda *_args, **_kwargs: "ok")
    monkeypatch.setattr(bridge, "_send_reply", lambda *_args, **_kwargs: None)

    bridge._process_event(
        {
            "event": "message",
            "device_id": "6281000000000@s.whatsapp.net",
            "payload": {
                "id": "msg-typing-1",
                "chat_id": "6281222222222@s.whatsapp.net",
                "from": "6281222222222@s.whatsapp.net",
                "body": "halo",
                "is_from_me": False,
            },
        }
    )

    assert actions == ["start", "stop"]
