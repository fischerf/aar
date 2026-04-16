"""Tests for WslDistroSandbox and wsl_manager helpers."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.safety.sandbox import WslDistroSandbox


# ---------------------------------------------------------------------------
# WslDistroSandbox — path translation (pure, no subprocess)
# ---------------------------------------------------------------------------


class TestWslPathTranslation:
    def test_windows_path_b_drive(self):
        sb = WslDistroSandbox()
        assert sb._to_wsl_path("B:\\foo\\bar") == "/mnt/b/foo/bar"

    def test_windows_path_c_drive(self):
        sb = WslDistroSandbox()
        assert sb._to_wsl_path("C:\\Users\\x") == "/mnt/c/Users/x"

    def test_windows_path_lowercase_drive(self):
        sb = WslDistroSandbox()
        assert sb._to_wsl_path("c:\\Windows") == "/mnt/c/Windows"

    def test_already_unix_path_unchanged(self):
        sb = WslDistroSandbox()
        assert sb._to_wsl_path("/mnt/b/foo") == "/mnt/b/foo"

    def test_unix_root_unchanged(self):
        sb = WslDistroSandbox()
        assert sb._to_wsl_path("/tmp/workspace") == "/tmp/workspace"

    def test_drive_only(self):
        sb = WslDistroSandbox()
        assert sb._to_wsl_path("B:\\") in ("/mnt/b/", "/mnt/b")

    def test_forward_slashes_windows_path(self):
        # PureWindowsPath handles forward slashes too
        sb = WslDistroSandbox()
        result = sb._to_wsl_path("B:/foo/bar")
        assert result == "/mnt/b/foo/bar"


# ---------------------------------------------------------------------------
# WslDistroSandbox — execute (mocked subprocess)
# ---------------------------------------------------------------------------


class TestWslDistroSandboxExecute:
    def _make_mock_proc(self, stdout: bytes = b"hello\n", stderr: bytes = b"", rc: int = 0):
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(stdout, stderr))
        mock_proc.returncode = rc
        mock_proc.kill = MagicMock()
        return mock_proc

    @pytest.mark.asyncio
    async def test_calls_wsl_with_distro(self):
        mock_proc = self._make_mock_proc()
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_proc
            sb = WslDistroSandbox(distro_name="test-distro")
            await sb.execute("echo hello")

        args = mock_exec.call_args[0]
        assert args[0] == "wsl"
        assert "-d" in args
        assert "test-distro" in args
        assert "--" in args

    @pytest.mark.asyncio
    async def test_uses_configured_shell(self):
        mock_proc = self._make_mock_proc()
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_proc
            sb = WslDistroSandbox(shell="bash")
            await sb.execute("echo hi")

        args = mock_exec.call_args[0]
        assert "bash" in args

    @pytest.mark.asyncio
    async def test_stdout_captured(self):
        mock_proc = self._make_mock_proc(stdout=b"hello world\n")
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_proc
            sb = WslDistroSandbox()
            result = await sb.execute("echo hello world")

        assert "hello world" in result.stdout
        assert result.exit_code == 0
        assert not result.timed_out

    @pytest.mark.asyncio
    async def test_stderr_captured(self):
        mock_proc = self._make_mock_proc(stderr=b"an error\n", rc=1)
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_proc
            sb = WslDistroSandbox()
            result = await sb.execute("bad command")

        assert "an error" in result.stderr
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_timeout_returns_timed_out(self):
        import asyncio

        mock_proc = self._make_mock_proc()
        # First call raises TimeoutError; second call (drain after kill) returns empty
        mock_proc.communicate = AsyncMock(side_effect=[asyncio.TimeoutError, (b"", b"")])

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_proc
            sb = WslDistroSandbox()
            result = await sb.execute("sleep 999", timeout=1)

        assert result.timed_out
        assert result.exit_code == -1
        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_env_vars_included_in_command(self):
        mock_proc = self._make_mock_proc()
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_proc
            sb = WslDistroSandbox()
            await sb.execute("printenv MY_VAR", env={"MY_VAR": "hello"})

        # The shell arg (last positional after "--", shell, "-c") should contain the env prefix
        args = mock_exec.call_args[0]
        shell_cmd = args[-1]  # the "-c" argument is the last one
        assert "MY_VAR=" in shell_cmd

    @pytest.mark.asyncio
    async def test_wsl_not_found_returns_error_result(self):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            sb = WslDistroSandbox()
            result = await sb.execute("echo hi")

        assert result.exit_code == 1
        assert "wsl.exe not found" in result.stderr


# ---------------------------------------------------------------------------
# wsl_manager helpers (mocked subprocess.run)
# ---------------------------------------------------------------------------


class TestWslManager:
    def _utf16_bytes(self, text: str) -> bytes:
        """Encode text as UTF-16-LE (what wsl -l -q outputs)."""
        return text.encode("utf-16-le")

    def test_list_distros_parses_utf16(self):
        from agent.safety import wsl_manager as wm

        raw = self._utf16_bytes("Ubuntu\r\naar-sandbox\r\n")
        mock_result = MagicMock()
        mock_result.stdout = raw
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            distros = wm.list_distros()

        assert "Ubuntu" in distros
        assert "aar-sandbox" in distros

    def test_list_distros_empty_on_error(self):
        from agent.safety import wsl_manager as wm

        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert wm.list_distros() == []

    def test_distro_exists_true(self):
        from agent.safety import wsl_manager as wm

        with patch.object(wm, "list_distros", return_value=["aar-sandbox", "Ubuntu"]):
            assert wm.distro_exists("aar-sandbox") is True

    def test_distro_exists_false(self):
        from agent.safety import wsl_manager as wm

        with patch.object(wm, "list_distros", return_value=["Ubuntu"]):
            assert wm.distro_exists("aar-sandbox") is False

    def test_distro_exists_case_insensitive(self):
        from agent.safety import wsl_manager as wm

        with patch.object(wm, "list_distros", return_value=["Aar-Sandbox"]):
            assert wm.distro_exists("aar-sandbox") is True

    def test_is_wsl_available_true(self):
        from agent.safety import wsl_manager as wm

        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            assert wm.is_wsl_available() is True

    def test_is_wsl_available_false_on_file_not_found(self):
        from agent.safety import wsl_manager as wm

        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert wm.is_wsl_available() is False

    def test_is_wsl_available_false_on_nonzero(self):
        from agent.safety import wsl_manager as wm

        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("subprocess.run", return_value=mock_result):
            assert wm.is_wsl_available() is False

    def test_default_install_path_uses_localappdata(self, monkeypatch):
        from agent.safety import wsl_manager as wm

        monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\test\\AppData\\Local")
        path = wm.default_install_path("my-distro")
        assert "aar" in str(path)
        assert "my-distro" in str(path)

    def test_default_rootfs_url_is_alpine(self):
        from agent.safety import wsl_manager as wm

        url = wm.default_rootfs_url()
        assert "alpine" in url.lower()
        assert url.endswith(".tar.gz")


# ---------------------------------------------------------------------------
# Config — new SafetyConfig fields
# ---------------------------------------------------------------------------


class TestSafetyConfigWslFields:
    def test_defaults(self):
        from agent.core.config import SafetyConfig

        sc = SafetyConfig()
        assert sc.sandbox_wsl_distro == "aar-sandbox"
        assert sc.sandbox_wsl_shell == "sh"
        assert sc.sandbox_wsl_install_path is None
        assert "alpine" in sc.sandbox_wsl_rootfs_url.lower()
        assert "python3" in sc.sandbox_wsl_packages

    def test_sandbox_shell_path_default_empty(self):
        from agent.core.config import SafetyConfig

        assert SafetyConfig().sandbox_shell_path == ""

    def test_wsl_mode_creates_wsl_sandbox(self):
        from agent.core.config import SafetyConfig
        from agent.tools.execution import _create_sandbox

        sc = SafetyConfig(sandbox="wsl", sandbox_wsl_distro="my-distro")
        sb = _create_sandbox(sc)
        assert isinstance(sb, WslDistroSandbox)
        assert sb.distro_name == "my-distro"


# ---------------------------------------------------------------------------
# Live tests — require real WSL2 + aar-sandbox distro
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(os.name != "nt", reason="WSL2 sandbox only on Windows")
class TestWslDistroSandboxLive:
    """These tests require WSL2 and a distro named 'aar-sandbox'.
    Run after: aar sandbox setup
    """

    @pytest.mark.asyncio
    async def test_execute_simple(self):
        from agent.safety import wsl_manager as wm

        if not wm.distro_exists("aar-sandbox"):
            pytest.skip("aar-sandbox distro not installed — run: aar sandbox setup")
        sb = WslDistroSandbox(distro_name="aar-sandbox")
        result = await sb.execute("echo hello")
        assert "hello" in result.stdout
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_isolated_from_main_distro(self):
        from agent.safety import wsl_manager as wm

        if not wm.distro_exists("aar-sandbox"):
            pytest.skip("aar-sandbox distro not installed — run: aar sandbox setup")
        # aar-sandbox is Alpine; uname should show its kernel
        sb = WslDistroSandbox(distro_name="aar-sandbox")
        result = await sb.execute("uname -r")
        assert result.exit_code == 0
        assert result.stdout.strip() != ""
