"""C1 — HTTP transport hardening: auth, CORS, and tighten-only safety overrides.

Before these fixes ``aar serve`` and ``aar acp --http`` were unauthenticated,
sent ``Access-Control-Allow-Origin: *``, auto-approved every tool call and let
the request body delete the safety policy — a drive-by RCE from any web page
the user happened to visit.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from agent.core.config import AgentConfig, ProviderConfig, SafetyConfig
from agent.transports._http_auth import (
    BearerAuth,
    apply_safety_override,
    cors_headers,
    is_loopback,
    normalize_origins,
)
from agent.transports.acp.http import create_acp_asgi_app
from agent.transports.cli import app as cli_app
from agent.transports.web import create_asgi_app

runner = CliRunner()


def _config() -> AgentConfig:
    return AgentConfig(provider=ProviderConfig(name="mock", model="mock-1"))


async def _request(
    app: Any,
    method: str,
    path: str,
    headers: list | None = None,
    body: bytes = b"",
) -> tuple[int, dict, bytes]:
    scope = {
        "type": "http",
        "method": method.upper(),
        "path": path,
        "query_string": b"",
        "headers": headers if headers is not None else [],
    }
    started: list[dict] = []
    chunks: list[bytes] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg: dict) -> None:
        if msg["type"] == "http.response.start":
            started.append(msg)
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body", b""))

    await app(scope, receive, send)
    status = started[0]["status"] if started else 500
    headers_out = {
        bytes(k).lower(): bytes(v) for k, v in (started[0]["headers"] if started else [])
    }
    return status, headers_out, b"".join(chunks)


def _bearer(app: Any) -> list:
    return [[b"authorization", f"Bearer {app.auth.token}".encode()]]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthentication:
    @pytest.mark.asyncio
    async def test_serve_rejects_missing_token(self):
        app = create_asgi_app(config=_config())
        status, _, body = await _request(
            app, "POST", "/chat", body=json.dumps({"prompt": "hi"}).encode()
        )
        assert status == 401
        assert b"unauthorized" in body.lower()

    @pytest.mark.asyncio
    async def test_serve_rejects_wrong_token(self):
        app = create_asgi_app(config=_config())
        status, _, _ = await _request(
            app,
            "POST",
            "/chat",
            headers=[[b"authorization", b"Bearer not-the-token"]],
            body=json.dumps({"prompt": "hi"}).encode(),
        )
        assert status == 401

    @pytest.mark.asyncio
    async def test_sessions_are_not_readable_without_a_token(self):
        """``GET /sessions/<id>`` returns full conversation history."""
        app = create_asgi_app(config=_config())
        for path in ("/sessions", "/sessions/abc123"):
            status, _, _ = await _request(app, "GET", path)
            assert status == 401, path

    @pytest.mark.asyncio
    async def test_health_stays_open(self):
        app = create_asgi_app(config=_config())
        status, _, _ = await _request(app, "GET", "/health")
        assert status == 200

    @pytest.mark.asyncio
    async def test_valid_token_is_accepted(self):
        app = create_asgi_app(config=_config(), auth=BearerAuth("t0ken"))
        status, _, _ = await _request(
            app,
            "GET",
            "/sessions",
            headers=[[b"authorization", b"Bearer t0ken"]],
        )
        assert status == 200

    @pytest.mark.asyncio
    async def test_disabled_auth_is_an_explicit_opt_in(self):
        app = create_asgi_app(config=_config(), auth=BearerAuth.disabled())
        status, _, _ = await _request(app, "GET", "/sessions")
        assert status == 200

    @pytest.mark.asyncio
    async def test_acp_http_rejects_missing_token(self):
        app = create_acp_asgi_app(config=_config())
        status, _, _ = await _request(app, "GET", "/agents")
        assert status == 401

    @pytest.mark.asyncio
    async def test_acp_http_ping_stays_open(self):
        app = create_acp_asgi_app(config=_config())
        status, _, _ = await _request(app, "GET", "/ping")
        assert status == 200

    @pytest.mark.asyncio
    async def test_acp_http_accepts_valid_token(self):
        app = create_acp_asgi_app(config=_config())
        status, _, _ = await _request(app, "GET", "/agents", headers=_bearer(app))
        assert status == 200

    def test_token_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("AAR_HTTP_TOKEN", "from-env")
        auth = BearerAuth()
        assert auth.token == "from-env"
        assert not auth.generated

    def test_generated_token_is_flagged(self, monkeypatch):
        monkeypatch.delenv("AAR_HTTP_TOKEN", raising=False)
        auth = BearerAuth()
        assert auth.generated
        assert len(auth.token) >= 32

    def test_check_requires_the_bearer_scheme(self, monkeypatch):
        monkeypatch.delenv("AAR_HTTP_TOKEN", raising=False)
        auth = BearerAuth("abc")
        assert not auth.check({"headers": [[b"authorization", b"abc"]]})
        assert not auth.check({"headers": []})
        assert auth.check({"headers": [[b"Authorization", b"Bearer abc"]]})


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


class TestCors:
    @pytest.mark.asyncio
    async def test_serve_no_cors_by_default(self):
        """A drive-by page must not be able to read the response."""
        app = create_asgi_app(config=_config())
        _, headers, _ = await _request(
            app, "OPTIONS", "/chat", headers=[[b"origin", b"https://evil.example"]]
        )
        assert b"access-control-allow-origin" not in headers

    @pytest.mark.asyncio
    async def test_no_wildcard_on_authenticated_responses(self):
        app = create_asgi_app(config=_config(), auth=BearerAuth("t"))
        _, headers, _ = await _request(
            app,
            "GET",
            "/sessions",
            headers=[[b"authorization", b"Bearer t"], [b"origin", b"https://evil.example"]],
        )
        assert headers.get(b"access-control-allow-origin") != b"*"
        assert b"access-control-allow-origin" not in headers

    @pytest.mark.asyncio
    async def test_configured_origin_is_reflected(self):
        app = create_asgi_app(config=_config(), cors_origins=["https://app.example"])
        _, headers, _ = await _request(
            app, "OPTIONS", "/chat", headers=[[b"origin", b"https://app.example"]]
        )
        assert headers[b"access-control-allow-origin"] == b"https://app.example"
        assert headers[b"vary"] == b"origin"

    @pytest.mark.asyncio
    async def test_unconfigured_origin_is_not_reflected(self):
        app = create_asgi_app(config=_config(), cors_origins=["https://app.example"])
        _, headers, _ = await _request(
            app, "OPTIONS", "/chat", headers=[[b"origin", b"https://evil.example"]]
        )
        assert b"access-control-allow-origin" not in headers

    @pytest.mark.asyncio
    async def test_acp_http_no_cors_by_default(self):
        app = create_acp_asgi_app(config=_config())
        _, headers, _ = await _request(
            app, "OPTIONS", "/runs", headers=[[b"origin", b"https://evil.example"]]
        )
        assert b"access-control-allow-origin" not in headers

    def test_origin_matching_is_exact(self):
        allowed = normalize_origins(["https://app.example"])
        assert cors_headers({"headers": [[b"origin", b"https://app.example.evil"]]}, allowed) == []
        assert cors_headers({"headers": [[b"origin", b"https://app.example/"]]}, allowed) != []


# ---------------------------------------------------------------------------
# Client-supplied safety override
# ---------------------------------------------------------------------------


class TestSafetyOverride:
    def test_override_cannot_loosen(self):
        base = SafetyConfig()
        result = apply_safety_override(
            base,
            {
                "read_only": False,
                "allowed_paths": [],
                "denied_paths": [],
                "sandbox": {"mode": "local"},
                "require_approval_for_execute": False,
            },
        )
        assert result.allowed_paths == base.allowed_paths
        assert result.denied_paths == base.denied_paths
        assert result.require_approval_for_execute is True
        assert result.sandbox.mode == base.sandbox.mode

    def test_override_can_tighten(self):
        base = SafetyConfig(read_only=False)
        result = apply_safety_override(base, {"read_only": True})
        assert result.read_only is True

    def test_deny_lists_are_extended_not_replaced(self):
        base = SafetyConfig()
        result = apply_safety_override(base, {"denied_paths": ["/srv/secret"]})
        assert "/srv/secret" in result.denied_paths
        assert set(base.denied_paths) <= set(result.denied_paths)

    def test_denied_commands_extension_keeps_policy_defaults(self):
        """``SafetyConfig.denied_commands`` defaults to None (= policy defaults)."""
        from agent.safety.policy import PolicyConfig

        base = SafetyConfig()
        assert base.denied_commands is None
        result = apply_safety_override(base, {"denied_commands": ["my-dangerous-tool"]})
        assert "my-dangerous-tool" in result.denied_commands
        assert set(PolicyConfig().denied_commands) <= set(result.denied_commands)

    def test_permissive_mode_restores_full_override(self):
        base = SafetyConfig()
        result = apply_safety_override(base, {"allowed_paths": []}, permissive=True)
        assert result.allowed_paths == []

    def test_ignored_keys_are_logged(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="agent.transports._http_auth"):
            apply_safety_override(SafetyConfig(), {"allowed_paths": []})
        assert "allowed_paths" in caplog.text

    @pytest.mark.asyncio
    async def test_transport_uses_tighten_only_by_default(self):
        from tests.conftest import MockProvider

        from agent.transports.web import WebTransport

        transport = WebTransport(config=_config())
        with patch("agent.core.agent._create_provider", lambda *a, **kw: MockProvider()):
            agent = transport._make_agent({"allowed_paths": [], "read_only": True})
        assert agent.config.safety.allowed_paths == SafetyConfig().allowed_paths
        assert agent.config.safety.read_only is True


# ---------------------------------------------------------------------------
# Approval default + bind-address policy
# ---------------------------------------------------------------------------


class TestApprovalAndBindPolicy:
    @pytest.mark.asyncio
    async def test_web_default_approval_denies(self):
        from agent.core.events import ToolCall
        from agent.safety.permissions import ApprovalResult
        from agent.tools.schema import SideEffect, ToolSpec
        from agent.transports.web import WebTransport

        transport = WebTransport(config=_config())
        spec = ToolSpec(name="bash", description="", side_effects=[SideEffect.EXECUTE])
        tc = ToolCall(tool_call_id="1", tool_name="bash", arguments={"command": "rm -rf /"})
        assert await transport.approval_callback(spec, tc) is ApprovalResult.DENIED

    @pytest.mark.asyncio
    async def test_acp_http_default_approval_denies(self):
        from agent.core.events import ToolCall
        from agent.safety.permissions import ApprovalResult
        from agent.tools.schema import SideEffect, ToolSpec

        app = create_acp_asgi_app(config=_config())
        spec = ToolSpec(name="bash", description="", side_effects=[SideEffect.EXECUTE])
        tc = ToolCall(tool_call_id="1", tool_name="bash", arguments={"command": "rm -rf /"})
        assert await app.transport.approval_callback(spec, tc) is ApprovalResult.DENIED

    def test_is_loopback(self):
        assert is_loopback("127.0.0.1")
        assert is_loopback("localhost")
        assert is_loopback("::1")
        assert not is_loopback("0.0.0.0")
        assert not is_loopback("192.168.1.5")

    def _invoke_serve(self, args: list[str], tmp_path):
        config = AgentConfig(
            provider=ProviderConfig(name="mock", model="mock-1"),
            session_dir=tmp_path / "sessions",
        )
        mock_uvicorn = MagicMock()
        with (
            patch("agent.transports.cli._build_config", return_value=config),
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn}),
        ):
            return runner.invoke(cli_app, ["serve", *args])

    def test_serve_refuses_public_bind_without_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AAR_HTTP_TOKEN", raising=False)
        result = self._invoke_serve(["--host", "0.0.0.0"], tmp_path)
        assert result.exit_code != 0
        assert "token" in result.output.lower()

    def test_serve_refuses_public_bind_with_no_auth(self, tmp_path):
        result = self._invoke_serve(["--host", "0.0.0.0", "--no-auth"], tmp_path)
        assert result.exit_code != 0

    def test_serve_allows_public_bind_with_token(self, tmp_path):
        result = self._invoke_serve(["--host", "0.0.0.0", "--token", "abc"], tmp_path)
        assert result.exit_code == 0

    def test_serve_prints_generated_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AAR_HTTP_TOKEN", raising=False)
        result = self._invoke_serve([], tmp_path)
        assert result.exit_code == 0
        assert "Auth token" in result.output

    def test_serve_rejects_unknown_approval_mode(self, tmp_path):
        result = self._invoke_serve(["--approval", "yolo"], tmp_path)
        assert result.exit_code != 0
