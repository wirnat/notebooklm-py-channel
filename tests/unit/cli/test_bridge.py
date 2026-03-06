"""Tests for bridge CLI commands."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli


def test_bridge_command_shows_in_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "bridge" in result.output


def test_bridge_whatsapp_runs_with_required_options():
    runner = CliRunner()
    with (
        patch("notebooklm.cli.bridge.load_auth_from_storage") as mock_load_auth,
        patch("notebooklm.cli.bridge.WhatsAppNotebookLMBridge") as mock_bridge_cls,
    ):
        mock_load_auth.return_value = {"SID": "test"}
        mock_bridge = MagicMock()
        mock_bridge_cls.return_value = mock_bridge

        result = runner.invoke(
            cli,
            [
                "bridge",
                "whatsapp",
                "--webhook-secret",
                "secret",
                "--url",
                "http://127.0.0.1:8781",
            ],
        )

        assert result.exit_code == 0, result.output
        mock_bridge.run_forever.assert_called_once()


def test_bridge_whatsapp_requires_secret():
    runner = CliRunner()
    with patch("notebooklm.cli.bridge.load_auth_from_storage") as mock_load_auth:
        mock_load_auth.return_value = {"SID": "test"}
        result = runner.invoke(
            cli,
            [
                "bridge",
                "whatsapp",
                "--url",
                "http://127.0.0.1:8781",
            ],
        )
    assert result.exit_code != 0
    assert "Webhook secret wajib" in result.output


def test_bridge_whatsapp_fails_fast_when_auth_invalid():
    runner = CliRunner()
    with patch("notebooklm.cli.bridge.load_auth_from_storage") as mock_load_auth:
        mock_load_auth.side_effect = FileNotFoundError("missing auth")
        result = runner.invoke(
            cli,
            [
                "bridge",
                "whatsapp",
                "--webhook-secret",
                "secret",
                "--url",
                "http://127.0.0.1:8781",
            ],
        )
    assert result.exit_code != 0
    assert "Auth NotebookLM tidak valid" in result.output


def test_bridge_whatsapp_accepts_admin_option():
    runner = CliRunner()
    with (
        patch("notebooklm.cli.bridge.load_auth_from_storage") as mock_load_auth,
        patch("notebooklm.cli.bridge.WhatsAppNotebookLMBridge") as mock_bridge_cls,
    ):
        mock_load_auth.return_value = {"SID": "test"}
        mock_bridge = MagicMock()
        mock_bridge_cls.return_value = mock_bridge

        result = runner.invoke(
            cli,
            [
                "bridge",
                "whatsapp",
                "--webhook-secret",
                "secret",
                "--url",
                "http://127.0.0.1:8781",
                "--admin",
                "62812xxxx,62813yyyy",
            ],
        )

        assert result.exit_code == 0, result.output
        passed_config = mock_bridge_cls.call_args.args[0]
        assert passed_config.admin_numbers == ("62812xxxx", "62813yyyy")


def test_bridge_whatsapp_uses_current_context_notebook_as_fallback():
    runner = CliRunner()
    with (
        patch("notebooklm.cli.bridge.load_auth_from_storage") as mock_load_auth,
        patch("notebooklm.cli.bridge.get_current_notebook") as mock_current_notebook,
        patch("notebooklm.cli.bridge.WhatsAppNotebookLMBridge") as mock_bridge_cls,
    ):
        mock_load_auth.return_value = {"SID": "test"}
        mock_current_notebook.return_value = "e70facd7-4864-4460-81bf-bc0823a26805"
        mock_bridge = MagicMock()
        mock_bridge_cls.return_value = mock_bridge

        result = runner.invoke(
            cli,
            [
                "bridge",
                "whatsapp",
                "--webhook-secret",
                "secret",
                "--url",
                "http://127.0.0.1:8781",
            ],
        )

        assert result.exit_code == 0, result.output
        passed_config = mock_bridge_cls.call_args.args[0]
        assert passed_config.global_notebook_id == "e70facd7-4864-4460-81bf-bc0823a26805"
