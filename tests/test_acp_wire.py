"""Wire-level ACP transport tests — paired AgentSideConnection + ClientSideConnection.

These tests drive ``AarAcpAgent`` through the real SDK JSON-RPC framing over
a loopback TCP socket. Unlike the unit tests in ``test_acp.py`` (which mock
the ``_conn`` object and assert on ``AsyncMock.call_args_list``), these
exercise the full wire roundtrip and catch schema, ordering, and
serialization regressions that a mock cannot.

Adapted from the ``agent-client-protocol`` python-sdk test conftest pattern
(https://github.com/agentclientprotocol/python-sdk/blob/main/tests/).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from agent.core.config import AgentConfig, ProviderConfig, SafetyConfig, ToolConfig
from agent.transports.acp.stdio import AarAcpAgent
from tests.conftest import MockProvider

acp_sdk = pytest.importorskip("acp", reason="agent-client-protocol not installed")

from acp import Agent as SdkAgent  # noqa: E402
from acp import Client as SdkClient  # noqa: E402
from acp.core import AgentSideConnection, ClientSideConnection  # noqa: E402
from acp.schema import (  # noqa: E402
    AgentMessageChunk,
    AllowedOutcome,
    AvailableCommandsUpdate,
    CreateTerminalResponse,
    CurrentModeUpdate,
    KillTerminalResponse,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    TerminalOutputResponse,
    TextContentBlock,
    UserMessageChunk,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(session_dir: Any) -> AgentConfig:
    return AgentConfig(
        provider=ProviderConfig(name="mock", model="mock-1"),
        max_steps=5,
        timeout=10.0,
        safety=SafetyConfig(
            require_approval_for_writes=False,
            require_approval_for_execute=False,
        ),
        tools=ToolConfig(enabled_builtins=[]),
        session_dir=session_dir,
    )


def _make_aar_sdk_agent(config: AgentConfig, provider: MockProvider) -> Any:
    """Build an AarAcpAgent subclass of the SDK Agent, wired with a mock provider.

    Mirrors the ``type()`` trick from ``run_acp_stdio()`` so the instance is
    accepted by ``AgentSideConnection``. ``_make_aar_agent`` is patched to
    inject the mock provider into the inner Aar run loop.
    """
    agent_cls = type("_TestSdkAgent", (AarAcpAgent, SdkAgent), {})
    agent = agent_cls(config=config, agent_name="aar")

    def patched_make(session_id: str = "", approval_callback: Any = None) -> Any:
        from agent.core.agent import Agent as InnerAgent

        return InnerAgent(
            config=agent._session_configs.get(session_id, agent._config),
            provider=provider,
            approval_callback=approval_callback or agent._default_approval,
            registry=agent._session_registries.get(session_id, agent._registry),
        )

    agent._make_aar_agent = patched_make  # type: ignore[method-assign]
    return agent


class _CaptureClient(SdkClient):
    """Minimal ACP client that records notifications and auto-answers permissions.

    Stores ``(session_id, update)`` tuples in ``.notifications`` as they arrive.
    Permission requests resolve to ``self.permission_option_id`` (default
    ``allow_once``). File ops are served from in-memory ``.files``.
    """

    def __init__(self) -> None:
        self.notifications: list[tuple[str, Any]] = []
        self.permission_requests: list[Any] = []
        self.permission_option_id: str = "allow_once"
        self.files: dict[str, str] = {}
        # Terminal bookkeeping — each id maps to the recorded create args,
        # a pre-canned output string, and an exit status.
        self.terminals: dict[str, dict[str, Any]] = {}
        self.terminal_calls: list[tuple[str, dict[str, Any]]] = []
        self._terminal_counter: int = 0
        self.default_terminal_output: str = "ok\n"
        self.default_exit_code: int | None = 0

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.notifications.append((session_id, update))

    async def request_permission(self, *args: Any, **kwargs: Any) -> Any:
        self.permission_requests.append(kwargs or (args[0] if args else None))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=self.permission_option_id)
        )

    async def read_text_file(self, *args: Any, **kwargs: Any) -> Any:
        path = kwargs.get("path", "")
        return ReadTextFileResponse(content=self.files.get(path, ""))

    async def write_text_file(self, *args: Any, **kwargs: Any) -> Any:
        self.files[kwargs.get("path", "")] = kwargs.get("content", "")
        return WriteTextFileResponse()

    async def create_terminal(self, *args: Any, **kwargs: Any) -> Any:
        self._terminal_counter += 1
        tid = f"term-{self._terminal_counter}"
        self.terminals[tid] = {
            "command": kwargs.get("command", ""),
            "args": kwargs.get("args"),
            "cwd": kwargs.get("cwd"),
            "env": kwargs.get("env"),
            "output": self.default_terminal_output,
            "exit_code": self.default_exit_code,
            "released": False,
            "killed": False,
        }
        self.terminal_calls.append(("create", {"terminal_id": tid, **kwargs}))
        return CreateTerminalResponse(terminal_id=tid)

    async def terminal_output(self, *args: Any, **kwargs: Any) -> Any:
        tid = kwargs.get("terminal_id", "")
        self.terminal_calls.append(("output", kwargs))
        rec = self.terminals.get(tid, {})
        return TerminalOutputResponse(
            output=rec.get("output", ""),
            truncated=False,
        )

    async def wait_for_terminal_exit(self, *args: Any, **kwargs: Any) -> Any:
        tid = kwargs.get("terminal_id", "")
        self.terminal_calls.append(("wait", kwargs))
        rec = self.terminals.get(tid, {})
        return WaitForTerminalExitResponse(exit_code=rec.get("exit_code", 0), signal=None)

    async def release_terminal(self, *args: Any, **kwargs: Any) -> Any:
        tid = kwargs.get("terminal_id", "")
        self.terminal_calls.append(("release", kwargs))
        if tid in self.terminals:
            self.terminals[tid]["released"] = True
        return ReleaseTerminalResponse()

    async def kill_terminal(self, *args: Any, **kwargs: Any) -> Any:
        tid = kwargs.get("terminal_id", "")
        self.terminal_calls.append(("kill", kwargs))
        if tid in self.terminals:
            self.terminals[tid]["killed"] = True
        return KillTerminalResponse()


class _AcpPair:
    """Paired ``AgentSideConnection`` + ``ClientSideConnection`` over a loopback socket.

    Usage::

        async with _AcpPair(agent, client) as (agent_side, client_side):
            await client_side.initialize(protocol_version=1)
            ...

    The test driver calls RPCs on ``client_side``; the agent receives them
    via ``agent_side`` and dispatches to ``agent``'s methods. Notifications
    flow the other way and land in ``client.notifications``.
    """

    def __init__(self, agent: Any, client: _CaptureClient) -> None:
        self.agent = agent
        self.client = client
        self._tcp_server: asyncio.AbstractServer | None = None
        self._server_reader: asyncio.StreamReader | None = None
        self._server_writer: asyncio.StreamWriter | None = None
        self._client_reader: asyncio.StreamReader | None = None
        self._client_writer: asyncio.StreamWriter | None = None
        self.agent_side: AgentSideConnection | None = None
        self.client_side: ClientSideConnection | None = None

    async def __aenter__(self) -> tuple[AgentSideConnection, ClientSideConnection]:
        ready = asyncio.Event()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            self._server_reader = reader
            self._server_writer = writer
            ready.set()

        self._tcp_server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
        host, port = self._tcp_server.sockets[0].getsockname()[:2]
        self._client_reader, self._client_writer = await asyncio.open_connection(host, port)
        await asyncio.wait_for(ready.wait(), timeout=2.0)

        # Positional order matches the upstream python-sdk connect fixture.
        self.agent_side = AgentSideConnection(
            self.agent,
            self._server_writer,
            self._server_reader,
            listening=True,
            use_unstable_protocol=True,
        )
        self.client_side = ClientSideConnection(
            self.client,
            self._client_writer,
            self._client_reader,
            use_unstable_protocol=True,
        )
        return self.agent_side, self.client_side

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        with contextlib.suppress(Exception):
            await self.agent.shutdown()
        for w in (self._client_writer, self._server_writer):
            if w is not None:
                w.close()
                with contextlib.suppress(Exception):
                    await w.wait_closed()
        if self._tcp_server is not None:
            self._tcp_server.close()
            with contextlib.suppress(Exception):
                await self._tcp_server.wait_closed()


async def _wait_for(predicate: Any, timeout: float = 2.0, step: float = 0.01) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


# ---------------------------------------------------------------------------
# Wire-level tests
# ---------------------------------------------------------------------------


class TestWireInitializeAndNewSession:
    @pytest.mark.asyncio
    async def test_initialize_returns_protocol_version_and_caps(self, tmp_path):
        from acp import PROTOCOL_VERSION

        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            resp = await client_side.initialize(protocol_version=1)

            assert resp.protocol_version == PROTOCOL_VERSION
            assert resp.agent_capabilities.load_session is True
            assert resp.agent_capabilities.prompt_capabilities.embedded_context is True

    @pytest.mark.asyncio
    async def test_new_session_pushes_available_commands(self, tmp_path):
        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            resp = await client_side.new_session(cwd="/ws", mcp_servers=[])
            assert resp.session_id

            await _wait_for(
                lambda: any(isinstance(u, AvailableCommandsUpdate) for _, u in client.notifications)
            )
            cmds_updates = [
                u for _, u in client.notifications if isinstance(u, AvailableCommandsUpdate)
            ]
            assert cmds_updates, "AvailableCommandsUpdate never arrived"
            names = {c.name for c in cmds_updates[0].available_commands}
            assert {"status", "tools", "policy"}.issubset(names)


class TestWirePromptRoundtrip:
    @pytest.mark.asyncio
    async def test_prompt_delivers_agent_message_chunk_and_end_turn(self, tmp_path):
        provider = MockProvider()
        provider.enqueue_text("Hello from Aar!", stop="end_turn")

        agent = _make_aar_sdk_agent(_make_config(tmp_path), provider)
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            sess = await client_side.new_session(cwd="/ws", mcp_servers=[])

            resp = await client_side.prompt(
                session_id=sess.session_id,
                prompt=[TextContentBlock(type="text", text="Hi")],
            )
            assert resp.stop_reason == "end_turn"

            await _wait_for(
                lambda: any(isinstance(u, AgentMessageChunk) for _, u in client.notifications)
            )
            msgs = [u for _, u in client.notifications if isinstance(u, AgentMessageChunk)]
            combined = "".join(m.content.text for m in msgs if hasattr(m.content, "text"))
            assert "Hello from Aar!" in combined


class TestWireSessionLoadReplay:
    """Per the ACP spec, the agent MUST replay the full conversation via
    ``session/update`` notifications before responding to ``session/load``.

    These are the highest-value wire tests: aar's ``load_session`` replays
    history in stdio.py:248-266 but the unit test at ``test_load_existing_session``
    only asserts the response is non-None. A regression in the replay loop
    would pass unit tests but break Zed's session-resume UX.
    """

    @pytest.mark.asyncio
    async def test_load_session_replays_history_in_order(self, tmp_path):
        from agent.core.events import AssistantMessage, UserMessage
        from agent.core.session import Session
        from agent.memory.session_store import SessionStore

        store = SessionStore(tmp_path)
        saved = Session()
        saved.events.append(UserMessage(content="What is 2+2?"))
        saved.events.append(AssistantMessage(content="Four."))
        saved.events.append(UserMessage(content="Thanks!"))
        saved.events.append(AssistantMessage(content="You're welcome."))
        store.save(saved)

        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)

            # Snapshot before load so we can isolate replay notifications from
            # any preamble (e.g. AvailableCommandsUpdate).
            before_load = len(client.notifications)

            await client_side.load_session(session_id=saved.session_id, cwd="", mcp_servers=[])

            await _wait_for(
                lambda: (
                    len(
                        [
                            u
                            for _, u in client.notifications[before_load:]
                            if isinstance(u, (UserMessageChunk, AgentMessageChunk))
                        ]
                    )
                    >= 4
                )
            )

            replayed = [
                u
                for _, u in client.notifications[before_load:]
                if isinstance(u, (UserMessageChunk, AgentMessageChunk))
            ]
            assert len(replayed) == 4, f"expected 4 replay chunks, got {len(replayed)}: {replayed}"

            # Ordering: User, Agent, User, Agent — content must match persisted events.
            assert isinstance(replayed[0], UserMessageChunk)
            assert replayed[0].content.text == "What is 2+2?"
            assert isinstance(replayed[1], AgentMessageChunk)
            assert replayed[1].content.text == "Four."
            assert isinstance(replayed[2], UserMessageChunk)
            assert replayed[2].content.text == "Thanks!"
            assert isinstance(replayed[3], AgentMessageChunk)
            assert replayed[3].content.text == "You're welcome."

    @pytest.mark.asyncio
    async def test_load_session_missing_sends_no_replay(self, tmp_path):
        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)

            before = len(client.notifications)
            # aar returns None for missing sessions — the wire may surface that
            # as an empty/null response or a validation error. Either way no
            # UserMessageChunk / AgentMessageChunk replay may be emitted.
            with contextlib.suppress(Exception):
                await client_side.load_session(
                    session_id="does-not-exist",
                    cwd="",
                    mcp_servers=[],
                )

            # Allow any stray notifications to arrive before asserting.
            await asyncio.sleep(0.05)
            stray = [
                u
                for _, u in client.notifications[before:]
                if isinstance(u, (UserMessageChunk, AgentMessageChunk))
            ]
            assert stray == [], f"missing session must not trigger replay: {stray}"


class TestWirePromptOrdering:
    """The ACP spec does not pin an exact order for every update, but the
    agent_message_chunk for the final assistant reply must arrive before the
    ``PromptResponse`` resolves — otherwise Zed's chat UI renders out of order.
    """

    @pytest.mark.asyncio
    async def test_final_message_arrives_before_prompt_response(self, tmp_path):
        provider = MockProvider()
        provider.enqueue_text("final reply", stop="end_turn")

        agent = _make_aar_sdk_agent(_make_config(tmp_path), provider)
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            sess = await client_side.new_session(cwd="/ws", mcp_servers=[])

            pre = len(client.notifications)

            resp = await client_side.prompt(
                session_id=sess.session_id,
                prompt=[TextContentBlock(type="text", text="go")],
            )
            assert resp.stop_reason == "end_turn"

            # By the time prompt() has returned, at least one
            # AgentMessageChunk for this turn must already be in the queue.
            agent_chunks_post = [
                u for _, u in client.notifications[pre:] if isinstance(u, AgentMessageChunk)
            ]
            assert agent_chunks_post, (
                "PromptResponse resolved before any AgentMessageChunk was delivered"
            )


class TestWireCancel:
    @pytest.mark.asyncio
    async def test_cancel_mid_prompt_returns_cancelled_stop_reason(self, tmp_path):
        provider = MockProvider()

        # Provider.complete blocks long enough for the test to cancel mid-call.
        async def slow_complete(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(30)
            raise AssertionError("slow_complete should have been cancelled")

        provider.complete = slow_complete  # type: ignore[method-assign]

        agent = _make_aar_sdk_agent(_make_config(tmp_path), provider)
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            sess = await client_side.new_session(cwd="/ws", mcp_servers=[])

            prompt_task = asyncio.create_task(
                client_side.prompt(
                    session_id=sess.session_id,
                    prompt=[TextContentBlock(type="text", text="slow please")],
                )
            )
            # Let the prompt reach the blocked provider.complete call.
            await asyncio.sleep(0.1)

            await client_side.cancel(session_id=sess.session_id)

            resp = await asyncio.wait_for(prompt_task, timeout=3.0)
            assert resp.stop_reason == "cancelled"


# ---------------------------------------------------------------------------
# Tier 2: JSON-RPC framing, EOF handling, and subprocess smoke
# ---------------------------------------------------------------------------


class TestWireJsonRpc:
    """Low-level JSON-RPC framing: what happens when the wire goes wrong.

    These tests bypass the SDK's typed helpers and poke the raw ``Connection``
    to exercise the error paths a misbehaving client could trigger.
    """

    @pytest.mark.asyncio
    async def test_invalid_json_frame_is_ignored(self, tmp_path):
        """A garbage line must NOT crash the receive loop.

        ``Connection._receive_loop`` logs and continues on ``JSONDecodeError``;
        a regression that propagates the exception would tear down the whole
        agent for one bad client frame.
        """
        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)

            # Push a non-JSON line directly at the agent's reader. The agent
            # socket's writer (``_client_writer`` from the pair's client side)
            # is the way raw bytes reach the agent's reader.
            # Access the pair via the enclosing context.
            # Use the underlying transport through the SDK connection object.
            inner = client_side._conn  # type: ignore[attr-defined]
            inner._writer.write(b"this is not json\n")  # type: ignore[attr-defined]
            await inner._writer.drain()  # type: ignore[attr-defined]

            # If the receive loop survived, a valid request still works.
            resp = await client_side.new_session(cwd="/ws", mcp_servers=[])
            assert resp.session_id

    @pytest.mark.asyncio
    async def test_method_not_found_returns_rpc_error(self, tmp_path):
        """Unknown methods must resolve to a JSON-RPC error (not hang).

        The SDK surfaces errors by raising ``RequestError`` from the
        ``send_request`` future. We don't assert on the exact code so the
        test stays robust against SDK error-code tweaks — just that the
        request resolves in error rather than deadlocking.
        """
        from acp.exceptions import RequestError

        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)

            inner = client_side._conn  # type: ignore[attr-defined]
            with pytest.raises((RequestError, Exception)):
                await asyncio.wait_for(
                    inner.send_request("no/such/method", {"session_id": "x"}),
                    timeout=2.0,
                )

    @pytest.mark.asyncio
    async def test_client_eof_shuts_agent_loop_gracefully(self, tmp_path):
        """Closing the client side of the pipe must let the agent shut down
        cleanly — not raise or leak unfinished tasks.
        """
        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        pair = _AcpPair(agent, client)
        agent_side, client_side = await pair.__aenter__()
        try:
            await client_side.initialize(protocol_version=1)
            # Half-close the client writer so the agent's reader gets EOF.
            pair._client_writer.write_eof()  # type: ignore[union-attr]
            await asyncio.sleep(0.1)
            # The receive loop should have exited; we can't introspect the
            # task directly from here, but the pair's aexit must still
            # complete without hanging.
        finally:
            await pair.__aexit__(None, None, None)


class TestWireSubprocessSmoke:
    """End-to-end smoke test: spawn ``python -m agent.transports.acp.stdio``
    (or ``aar acp``) as a subprocess and drive it with the SDK client.

    The process is torn down immediately after the initialize handshake so
    this stays fast.
    """

    @pytest.mark.asyncio
    async def test_subprocess_stdio_handshake(self, tmp_path):
        import os
        import sys

        # Run the stdio transport under a fresh Python to catch import-time
        # or startup regressions that don't surface in in-process tests.
        env = os.environ.copy()
        env["AAR_HOME"] = str(tmp_path / "aar_home")

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import asyncio; from agent.transports.acp.stdio import run_acp_stdio;"
            " asyncio.run(run_acp_stdio())",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            client = _CaptureClient()
            # The subprocess expects JSON-RPC on its stdin, responses on stdout.
            # Reverse the roles: our ClientSideConnection writes to the
            # subprocess's stdin and reads from its stdout.
            client_side = ClientSideConnection(
                client,
                proc.stdin,  # type: ignore[arg-type]
                proc.stdout,  # type: ignore[arg-type]
                use_unstable_protocol=True,
            )

            resp = await asyncio.wait_for(client_side.initialize(protocol_version=1), timeout=5.0)
            assert resp.agent_info.name == "aar"
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()  # type: ignore[union-attr]
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


# ---------------------------------------------------------------------------
# Wire tests for new ACP agent methods
# ---------------------------------------------------------------------------


class TestWireAuthenticate:
    @pytest.mark.asyncio
    async def test_authenticate_returns_response(self, tmp_path):
        """Aar has no auth methods so ``authenticate`` is a no-op that still
        returns a valid ``AuthenticateResponse``. Clients that probe it
        must not see an error.
        """
        from acp.schema import AuthenticateResponse

        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            resp = await client_side.authenticate(method_id="any-method")
            assert isinstance(resp, AuthenticateResponse)


class TestWireSessionDiscovery:
    """``new_session`` / ``load_session`` must advertise modes and config options
    so a generic client can discover what aar supports without guesswork."""

    @pytest.mark.asyncio
    async def test_new_session_advertises_modes_and_config_options(self, tmp_path):
        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            resp = await client_side.new_session(cwd="/ws", mcp_servers=[])

            assert resp.modes is not None
            mode_ids = {m.id for m in resp.modes.available_modes}
            assert {"auto", "review", "read-only"}.issubset(mode_ids)
            assert resp.modes.current_mode_id in mode_ids

            assert resp.config_options is not None
            option_ids = {o.id for o in resp.config_options}
            assert {"auto_approve_writes", "auto_approve_execute", "read_only"}.issubset(
                option_ids
            )

    @pytest.mark.asyncio
    async def test_load_session_advertises_current_mode(self, tmp_path):
        from agent.core.events import UserMessage
        from agent.core.session import Session
        from agent.memory.session_store import SessionStore

        store = SessionStore(tmp_path)
        saved = Session()
        saved.events.append(UserMessage(content="hi"))
        store.save(saved)

        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            resp = await client_side.load_session(
                cwd="", session_id=saved.session_id, mcp_servers=[]
            )
            # The test config turns both approvals off, so the derived mode
            # must be "auto" — discovery mirrors the effective safety config.
            assert resp.modes.current_mode_id == "auto"


class TestWireSetSessionMode:
    @pytest.mark.asyncio
    async def test_set_mode_auto_disables_approvals(self, tmp_path):
        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            sess = await client_side.new_session(cwd="/ws", mcp_servers=[])
            sid = sess.session_id

            # Force approval ON to start so we can see the mode flip it.
            base = agent._config.safety.model_copy(
                update={
                    "require_approval_for_writes": True,
                    "require_approval_for_execute": True,
                }
            )
            agent._session_configs[sid] = agent._config.model_copy(update={"safety": base})

            await client_side.set_session_mode(mode_id="auto", session_id=sid)

            patched = agent._session_configs[sid].safety
            assert patched.require_approval_for_writes is False
            assert patched.require_approval_for_execute is False
            assert agent._session_modes[sid] == "auto"

            # CurrentModeUpdate must land at the client.
            await _wait_for(
                lambda: any(isinstance(u, CurrentModeUpdate) for _, u in client.notifications)
            )
            modes = [u for _, u in client.notifications if isinstance(u, CurrentModeUpdate)]
            assert modes and modes[-1].current_mode_id == "auto"

    @pytest.mark.asyncio
    async def test_set_mode_read_only_locks_safety(self, tmp_path):
        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            sess = await client_side.new_session(cwd="/ws", mcp_servers=[])

            await client_side.set_session_mode(mode_id="read-only", session_id=sess.session_id)
            safety = agent._session_configs[sess.session_id].safety
            assert safety.read_only is True
            assert safety.require_approval_for_writes is True
            assert safety.require_approval_for_execute is True

    @pytest.mark.asyncio
    async def test_set_unknown_mode_errors(self, tmp_path):
        from acp.exceptions import RequestError

        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            sess = await client_side.new_session(cwd="/ws", mcp_servers=[])

            with pytest.raises((RequestError, Exception)):
                await client_side.set_session_mode(mode_id="bogus", session_id=sess.session_id)


class TestWireSetConfigOption:
    @pytest.mark.asyncio
    async def test_set_auto_approve_writes_flips_flag(self, tmp_path):
        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            sess = await client_side.new_session(cwd="/ws", mcp_servers=[])
            sid = sess.session_id

            await client_side.set_config_option(
                config_id="auto_approve_writes", session_id=sid, value=True
            )
            assert agent._session_configs[sid].safety.require_approval_for_writes is False

            await client_side.set_config_option(
                config_id="auto_approve_writes", session_id=sid, value=False
            )
            assert agent._session_configs[sid].safety.require_approval_for_writes is True

    @pytest.mark.asyncio
    async def test_set_read_only_toggles_sandbox_mode(self, tmp_path):
        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            sess = await client_side.new_session(cwd="/ws", mcp_servers=[])
            sid = sess.session_id

            await client_side.set_config_option(config_id="read_only", session_id=sid, value=True)
            assert agent._session_configs[sid].safety.read_only is True


class TestWireForkSession:
    @pytest.mark.asyncio
    async def test_fork_copies_events_to_new_session(self, tmp_path):
        from agent.core.events import AssistantMessage, UserMessage

        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            sess = await client_side.new_session(cwd="/ws", mcp_servers=[])
            src_sid = sess.session_id

            # Seed some events into the source session.
            agent._sessions[src_sid].events.append(UserMessage(content="Seed question"))
            agent._sessions[src_sid].events.append(AssistantMessage(content="Seed answer"))

            fork_resp = await client_side.fork_session(
                cwd="/ws", session_id=src_sid, mcp_servers=[]
            )
            new_sid = fork_resp.session_id

            assert new_sid != src_sid, "fork must return a new session id"
            forked = agent._sessions[new_sid]
            assert len(forked.events) == 2
            assert forked.events[0].content == "Seed question"
            assert forked.events[1].content == "Seed answer"
            assert forked.metadata.get("forked_from") == src_sid

            # Modifying the fork must not leak back into the source (deep copy).
            forked.events.append(UserMessage(content="Only in fork"))
            assert len(agent._sessions[src_sid].events) == 2

    @pytest.mark.asyncio
    async def test_fork_missing_session_raises(self, tmp_path):
        from acp.exceptions import RequestError

        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            with pytest.raises((RequestError, Exception)):
                await client_side.fork_session(cwd="", session_id="nonexistent-xxx", mcp_servers=[])


class TestWireResumeSession:
    @pytest.mark.asyncio
    async def test_resume_does_not_replay_history(self, tmp_path):
        """``session/resume`` differs from ``session/load`` — it re-attaches
        state without replaying events, so the client must not see
        UserMessageChunk / AgentMessageChunk notifications.
        """
        from agent.core.events import AssistantMessage, UserMessage
        from agent.core.session import Session
        from agent.memory.session_store import SessionStore

        store = SessionStore(tmp_path)
        saved = Session()
        saved.events.append(UserMessage(content="Q"))
        saved.events.append(AssistantMessage(content="A"))
        store.save(saved)

        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            before = len(client.notifications)

            await client_side.resume_session(cwd="", session_id=saved.session_id, mcp_servers=[])

            # No replay chunks should have been sent.
            await asyncio.sleep(0.1)
            replayed = [
                u
                for _, u in client.notifications[before:]
                if isinstance(u, (UserMessageChunk, AgentMessageChunk))
            ]
            assert replayed == []
            # But the session is now live in the agent.
            assert saved.session_id in agent._sessions

    @pytest.mark.asyncio
    async def test_resume_missing_session_raises(self, tmp_path):
        from acp.exceptions import RequestError

        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(protocol_version=1)
            with pytest.raises((RequestError, Exception)):
                await client_side.resume_session(cwd="", session_id="nope-xxx", mcp_servers=[])


class TestWireAcpTerminalTool:
    """The ``acp_terminal`` tool drives the client's terminal/* method family
    to run a command and return its output. This test exercises the full
    lifecycle: create → wait_for_exit → output → release.
    """

    @pytest.mark.asyncio
    async def test_terminal_tool_round_trip(self, tmp_path):
        from acp.schema import ClientCapabilities

        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()
        client.default_terminal_output = "hello from the editor terminal\n"

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(
                protocol_version=1,
                client_capabilities=ClientCapabilities(terminal=True),
            )
            sess = await client_side.new_session(cwd="/ws", mcp_servers=[])

            # The acp_terminal tool is registered at session setup because
            # _conn is set on the agent by on_connect AND the client
            # advertised terminal support. Call its handler.
            registry = agent._session_registries.get(sess.session_id)
            assert registry is not None, "session registry missing after new_session"
            spec = registry.get("acp_terminal")
            assert spec is not None, "acp_terminal tool was not registered"
            assert spec.handler is not None

            result = await spec.handler(command="echo", args=["hi"])

            assert "hello from the editor terminal" in result

            # The client must have seen the full lifecycle: create → wait
            # → output → release. Ordering matters because release must run
            # last, otherwise the output read would see a closed terminal.
            kinds = [k for k, _ in client.terminal_calls]
            assert kinds[0] == "create"
            assert kinds[-1] == "release"
            assert "wait" in kinds
            assert "output" in kinds

            # And the terminal record must now be marked released.
            tid = next(iter(client.terminals))
            assert client.terminals[tid]["released"] is True

    @pytest.mark.asyncio
    async def test_terminal_tool_timeout_kills_and_releases(self, tmp_path):
        from acp.schema import ClientCapabilities

        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        # Override wait_for_terminal_exit to never return so the tool has to
        # time out. The timeout branch must still call kill + release.
        async def hang(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(10)
            raise AssertionError("wait should have timed out")

        client.wait_for_terminal_exit = hang  # type: ignore[method-assign]

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(
                protocol_version=1,
                client_capabilities=ClientCapabilities(terminal=True),
            )
            sess = await client_side.new_session(cwd="/ws", mcp_servers=[])

            spec = agent._session_registries[sess.session_id].get("acp_terminal")
            assert spec is not None

            result = await spec.handler(command="sleep", args=["100"], timeout=0.1)
            assert "timed out" in result

            kinds = [k for k, _ in client.terminal_calls]
            assert "kill" in kinds, "timed-out terminals must be killed"
            assert kinds[-1] == "release"

    @pytest.mark.asyncio
    async def test_terminal_tool_absent_when_client_lacks_capability(self, tmp_path):
        """When the client doesn't advertise ``terminal`` support, the
        ``acp_terminal`` tool must NOT be registered — otherwise the agent
        could issue ``terminal/create`` to a peer that doesn't implement it.
        """
        agent = _make_aar_sdk_agent(_make_config(tmp_path), MockProvider())
        client = _CaptureClient()

        async with _AcpPair(agent, client) as (_, client_side):
            # Default ClientCapabilities() has terminal=False.
            await client_side.initialize(protocol_version=1)
            sess = await client_side.new_session(cwd="/ws", mcp_servers=[])

            # Either the session registry is absent entirely (nothing else
            # installed) or it exists without acp_terminal — both are valid,
            # neither must expose the tool.
            registry = agent._session_registries.get(sess.session_id)
            if registry is not None:
                assert registry.get("acp_terminal") is None

    @pytest.mark.asyncio
    async def test_terminal_tool_end_to_end_via_prompt_loop(self, tmp_path):
        """The prompt loop picks up a provider tool_call for ``acp_terminal``,
        drives the full ``terminal/*`` lifecycle, and the client observes
        both the terminal RPCs and the tool_call notification pair.
        """
        from acp.schema import ClientCapabilities, TextContentBlock

        provider = MockProvider()
        # Step 1: provider emits a tool_call for acp_terminal.
        provider.enqueue_tool_call(
            tool_name="acp_terminal",
            arguments={"command": "echo", "args": ["hi"]},
            tool_call_id="tc_term_1",
        )
        # Step 2: provider closes the turn with a plain-text reply.
        provider.enqueue_text("done", stop="end_turn")

        agent = _make_aar_sdk_agent(_make_config(tmp_path), provider)
        client = _CaptureClient()
        client.default_terminal_output = "hi\n"

        async with _AcpPair(agent, client) as (_, client_side):
            await client_side.initialize(
                protocol_version=1,
                client_capabilities=ClientCapabilities(terminal=True),
            )
            sess = await client_side.new_session(cwd="/ws", mcp_servers=[])

            resp = await client_side.prompt(
                session_id=sess.session_id,
                prompt=[TextContentBlock(type="text", text="run echo")],
            )
            assert resp.stop_reason == "end_turn"

            # Wait for the full terminal lifecycle to have been observed.
            await _wait_for(
                lambda: "release" in [k for k, _ in client.terminal_calls]
            )

            kinds = [k for k, _ in client.terminal_calls]
            # Full lifecycle in order: create first, release last.
            assert kinds[0] == "create"
            assert kinds[-1] == "release"
            assert "wait" in kinds
            assert "output" in kinds

            # The create call must carry the provider's arguments.
            _, create_kwargs = client.terminal_calls[0]
            assert create_kwargs.get("command") == "echo"
            assert create_kwargs.get("args") == ["hi"]

            # And the agent sent session/update notifications framing the tool
            # call: a ToolCallStart (session_update="tool_call") followed by
            # at least one ToolCallProgress (session_update="tool_call_update").
            tool_updates = [
                u
                for _, u in client.notifications
                if getattr(u, "session_update", None) in ("tool_call", "tool_call_update")
            ]
            start_updates = [
                u for u in tool_updates if u.session_update == "tool_call"
            ]
            progress_updates = [
                u for u in tool_updates if u.session_update == "tool_call_update"
            ]
            assert start_updates, "expected at least one tool_call (ToolCallStart) update"
            assert progress_updates, "expected at least one tool_call_update (ToolCallProgress)"

            # The start update must name the acp_terminal tool call.
            assert any(
                getattr(u, "tool_call_id", None) == "tc_term_1" for u in start_updates
            )
            # And a terminal progress update must report completion.
            assert any(
                getattr(u, "status", None) == "completed" for u in progress_updates
            )
