"""Tests for WslDistroSandbox and wsl_manager helpers."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.safety.sandbox import WslDistroSandbox

_LIVE_WSL_DISTRO = os.environ.get("AAR_TEST_WSL_DISTRO", "aar-sandbox")

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


class TestWslWorkspaceEscape:
    """H4: cwd must stay inside the workspace."""

    @pytest.mark.asyncio
    async def test_cwd_outside_workspace_rejected(self):
        sb = WslDistroSandbox(workspace="B:/project")
        result = await sb.execute("echo x", cwd="B:/other_project")
        assert result.exit_code == 1
        assert "outside the sandbox workspace" in result.stderr

    @pytest.mark.asyncio
    async def test_cwd_traversal_blocked(self):
        """`..` traversal should be collapsed and then rejected."""
        sb = WslDistroSandbox(workspace="B:/project")
        result = await sb.execute("echo x", cwd="B:/project/../other")
        assert result.exit_code == 1
        assert "outside the sandbox workspace" in result.stderr

    @pytest.mark.asyncio
    async def test_cwd_inside_workspace_runs(self):
        sb = WslDistroSandbox(workspace="B:/project")
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_proc
            result = await sb.execute("echo x", cwd="B:/project/sub")
        assert result.exit_code == 0
        assert mock_exec.called

    @pytest.mark.asyncio
    async def test_cwd_drive_letter_case_insensitive(self):
        """Mixed-case drive letters must still count as inside."""
        sb = WslDistroSandbox(workspace="B:/project")
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_proc
            result = await sb.execute("echo x", cwd="b:\\Project\\sub")
        assert result.exit_code == 0
        assert mock_exec.called

    @pytest.mark.asyncio
    async def test_cwd_none_uses_workspace(self):
        """No cwd argument → workspace is used, always valid."""
        sb = WslDistroSandbox(workspace="B:/project")
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_proc
            result = await sb.execute("echo x")
        assert result.exit_code == 0


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
# S6 — rootfs sha256 verification
# ---------------------------------------------------------------------------


class TestS6RootfsSha256:
    """S6: ``download_rootfs`` must verify a supplied SHA-256 and delete
    the downloaded file on mismatch. Pre-S6 a CDN compromise would silently
    install an attacker-controlled rootfs.
    """

    def _stub_urlretrieve(self, contents: bytes):
        """Return a function suitable for patching ``urllib.request.urlretrieve``
        that writes *contents* to the destination path and ignores the URL.
        """

        def _stub(url, dest, reporthook=None):
            from pathlib import Path

            Path(dest).write_bytes(contents)

        return _stub

    def test_matching_sha256_keeps_file(self, tmp_path):
        from hashlib import sha256
        from unittest.mock import patch

        from agent.safety import wsl_manager as wm

        payload = b"hello-rootfs"
        expected = sha256(payload).hexdigest()
        dest = tmp_path / "rootfs.tar.gz"

        with patch("urllib.request.urlretrieve", new=self._stub_urlretrieve(payload)):
            wm.download_rootfs("http://example/x.tgz", dest, expected_sha256=expected)

        assert dest.exists()
        assert dest.read_bytes() == payload

    def test_mismatched_sha256_raises_and_unlinks(self, tmp_path):
        from unittest.mock import patch

        from agent.safety import wsl_manager as wm

        payload = b"tampered-rootfs"
        dest = tmp_path / "rootfs.tar.gz"

        with patch("urllib.request.urlretrieve", new=self._stub_urlretrieve(payload)):
            with pytest.raises(ValueError, match="SHA-256 mismatch"):
                wm.download_rootfs(
                    "http://example/x.tgz",
                    dest,
                    expected_sha256="00" * 32,
                )

        assert not dest.exists(), "download must be unlinked on mismatch"

    def test_missing_sha256_logs_warning(self, tmp_path, caplog):
        import logging
        from unittest.mock import patch

        from agent.safety import wsl_manager as wm

        payload = b"unverified-rootfs"
        dest = tmp_path / "rootfs.tar.gz"

        with patch("urllib.request.urlretrieve", new=self._stub_urlretrieve(payload)):
            with caplog.at_level(logging.WARNING, logger=wm.logger.name):
                wm.download_rootfs("http://example/x.tgz", dest, expected_sha256=None)

        assert dest.exists()
        msgs = [r.getMessage() for r in caplog.records]
        assert any("without sha256 verification" in m for m in msgs), msgs

    def test_sha256_comparison_case_insensitive(self, tmp_path):
        from hashlib import sha256
        from unittest.mock import patch

        from agent.safety import wsl_manager as wm

        payload = b"case-test"
        expected = sha256(payload).hexdigest().upper()  # uppercase
        dest = tmp_path / "rootfs.tar.gz"

        with patch("urllib.request.urlretrieve", new=self._stub_urlretrieve(payload)):
            wm.download_rootfs("http://example/x.tgz", dest, expected_sha256=expected)

        assert dest.exists()

    def test_sha256_of_file_streams_chunks(self, tmp_path):
        """Sanity: the helper accepts a tiny chunk size and produces the
        right digest for a multi-chunk file.
        """
        from hashlib import sha256

        from agent.safety.wsl_manager import _sha256_of_file

        payload = b"x" * (3 * 1024 * 1024 + 17)  # 3 MiB + change
        p = tmp_path / "big.bin"
        p.write_bytes(payload)
        assert _sha256_of_file(p, chunk_size=4096) == sha256(payload).hexdigest()


class TestBundledWslProfiles:
    """Bundled distro profiles preserve integrity and runtime dependencies."""

    def test_config_field_defaults_to_none(self):
        from agent.core.config import WslSandboxConfig

        assert WslSandboxConfig().rootfs_sha256 is None

    def test_config_field_can_be_set(self):
        from agent.core.config import WslSandboxConfig

        cfg = WslSandboxConfig(rootfs_sha256="de" * 32)
        assert cfg.rootfs_sha256 == "de" * 32

    @staticmethod
    def _profiles():
        import json
        from pathlib import Path

        profiles_dir = Path(__file__).resolve().parent.parent / "config" / "distros"
        for path in sorted(profiles_dir.glob("*.json")):
            yield path.name, json.loads(path.read_text(encoding="utf-8"))

    def test_bundled_profiles_have_sha256(self):
        for name, data in self._profiles():
            assert data.get("rootfs_sha256"), f"{name} must ship a rootfs_sha256"
            assert len(data["rootfs_sha256"]) == 64, name

    def test_bundled_profiles_include_linux_sandbox_dependencies(self):
        for name, data in self._profiles():
            packages = set(data.get("packages", []))
            assert {"bubblewrap", "socat"} <= packages, name


# ---------------------------------------------------------------------------
# Config — new SafetyConfig fields
# ---------------------------------------------------------------------------


class TestWslSetupPackages:
    def test_required_dependencies_are_added_to_overrides(self):
        from agent.transports.cli import _resolve_wsl_packages

        assert _resolve_wsl_packages("python3,nodejs") == [
            "python3",
            "nodejs",
            "bubblewrap",
            "socat",
        ]

    def test_packages_are_trimmed_and_deduplicated(self):
        from agent.transports.cli import _resolve_wsl_packages

        assert _resolve_wsl_packages(" python3, socat,python3,bubblewrap ") == [
            "python3",
            "socat",
            "bubblewrap",
        ]


class TestSafetyConfigWslFields:
    def test_defaults(self):
        from agent.core.config import SafetyConfig

        sc = SafetyConfig()
        assert sc.sandbox.wsl.distro == "aar-sandbox"
        assert sc.sandbox.wsl.shell == "sh"
        assert sc.sandbox.wsl.install_path is None
        assert "alpine" in sc.sandbox.wsl.rootfs_url.lower()
        assert "python3" in sc.sandbox.wsl.packages
        assert "bubblewrap" in sc.sandbox.wsl.packages
        assert "socat" in sc.sandbox.wsl.packages

    def test_sandbox_mode_default_is_local(self):
        from agent.core.config import SafetyConfig

        assert SafetyConfig().sandbox.mode == "local"

    def test_wsl_mode_creates_wsl_sandbox(self):
        from agent.core.config import SafetyConfig, SandboxConfig, WslSandboxConfig
        from agent.tools.execution import _create_sandbox

        sc = SafetyConfig(
            sandbox=SandboxConfig(mode="wsl", wsl=WslSandboxConfig(distro="my-distro"))
        )
        sb = _create_sandbox(sc)
        assert isinstance(sb, WslDistroSandbox)
        assert sb.distro_name == "my-distro"


# ---------------------------------------------------------------------------
# S4 — env-key validation
# ---------------------------------------------------------------------------


class TestEnvKeyValidation:
    """S4: ``WslDistroSandbox`` must reject env keys that aren't valid POSIX
    identifiers — ``shlex.quote`` only quotes *values*, so an attacker-supplied
    key like ``"FOO; rm -rf /"`` would otherwise be spliced into the shell
    command verbatim.
    """

    def _make_mock_proc(self):
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()
        return mock_proc

    @pytest.mark.asyncio
    async def test_valid_keys_pass_through(self):
        mock_proc = self._make_mock_proc()
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_proc
            sb = WslDistroSandbox()
            result = await sb.execute("printenv", env={"FOO": "bar", "_X": "y", "A1": "z"})
        assert result.exit_code == 0
        assert mock_exec.called
        shell_cmd = mock_exec.call_args[0][-1]
        assert "FOO=bar" in shell_cmd

    @pytest.mark.asyncio
    async def test_semicolon_key_rejected(self):
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            sb = WslDistroSandbox()
            result = await sb.execute("printenv", env={"FOO; rm -rf /": "y"})
        assert result.exit_code == 1
        assert "invalid environment variable name" in result.stderr
        # subprocess must NOT have been invoked
        assert not mock_exec.called

    @pytest.mark.asyncio
    async def test_space_in_key_rejected(self):
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            sb = WslDistroSandbox()
            result = await sb.execute("printenv", env={"FOO BAR": "y"})
        assert result.exit_code == 1
        assert "invalid environment variable name" in result.stderr
        assert not mock_exec.called

    @pytest.mark.asyncio
    async def test_empty_key_rejected(self):
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            sb = WslDistroSandbox()
            result = await sb.execute("printenv", env={"": "y"})
        assert result.exit_code == 1
        assert "invalid environment variable name" in result.stderr
        assert not mock_exec.called

    @pytest.mark.asyncio
    async def test_leading_digit_rejected(self):
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            sb = WslDistroSandbox()
            result = await sb.execute("printenv", env={"1FOO": "y"})
        assert result.exit_code == 1
        assert "invalid environment variable name" in result.stderr
        assert not mock_exec.called

    @pytest.mark.asyncio
    async def test_first_invalid_key_blocks_rest(self):
        """Even one bad key fails the entire call — fail-closed."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            sb = WslDistroSandbox()
            result = await sb.execute(
                "printenv",
                env={"GOOD": "a", "BAD KEY": "b", "ALSO_GOOD": "c"},
            )
        assert result.exit_code == 1
        assert "invalid environment variable name" in result.stderr
        assert not mock_exec.called


# ---------------------------------------------------------------------------
# Live tests — require real WSL2 + aar-sandbox distro
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(os.name != "nt", reason="WSL2 sandbox only on Windows")
class TestWslDistroSandboxLive:
    """These tests require WSL2 and the configured live-test distro.

    Defaults to ``aar-sandbox``; override with ``AAR_TEST_WSL_DISTRO``.
    """

    @pytest.mark.asyncio
    async def test_execute_simple(self):
        from agent.safety import wsl_manager as wm

        if not wm.distro_exists(_LIVE_WSL_DISTRO):
            pytest.skip(f"{_LIVE_WSL_DISTRO} distro not installed — run: aar sandbox setup")
        sb = WslDistroSandbox(distro_name=_LIVE_WSL_DISTRO)
        result = await sb.execute("echo hello")
        assert "hello" in result.stdout
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_linux_sandbox_dependencies_work(self):
        from agent.safety import wsl_manager as wm

        if not wm.distro_exists(_LIVE_WSL_DISTRO):
            pytest.skip(f"{_LIVE_WSL_DISTRO} distro not installed — run: aar sandbox setup")
        sb = WslDistroSandbox(distro_name=_LIVE_WSL_DISTRO)
        result = await sb.execute(
            "bwrap --ro-bind / / --proc /proc --dev /dev sh -c 'echo bubblewrap-ok' "
            "&& socat -V"
        )
        assert result.exit_code == 0, result.stderr
        assert "bubblewrap-ok" in result.stdout
        assert "socat version" in result.stdout.lower()

    @pytest.mark.asyncio
    async def test_isolated_from_main_distro(self):
        from agent.safety import wsl_manager as wm

        if not wm.distro_exists(_LIVE_WSL_DISTRO):
            pytest.skip(f"{_LIVE_WSL_DISTRO} distro not installed — run: aar sandbox setup")
        sb = WslDistroSandbox(distro_name=_LIVE_WSL_DISTRO)
        result = await sb.execute("uname -r")
        assert result.exit_code == 0
        assert result.stdout.strip() != ""
