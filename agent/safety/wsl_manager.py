"""WSL2 distro management helpers for the Aar WSL sandbox."""

from __future__ import annotations

import subprocess
import urllib.request
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Availability / introspection
# ---------------------------------------------------------------------------


def is_wsl_available() -> bool:
    """Return True if wsl.exe is accessible and operational."""
    try:
        result = subprocess.run(
            ["wsl", "--status"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def list_distros() -> list[str]:
    """Return names of registered WSL2 distros.

    ``wsl -l -q`` outputs UTF-16-LE on Windows (including NUL bytes).  We
    decode carefully and strip empty / whitespace-only entries.
    """
    try:
        result = subprocess.run(
            ["wsl", "-l", "-q"],
            capture_output=True,
            timeout=15,
        )
        raw = result.stdout
        # wsl outputs UTF-16-LE; strip NUL bytes after decoding
        text = raw.decode("utf-16-le", errors="replace").replace("\x00", "")
        return [line.strip() for line in text.splitlines() if line.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def distro_exists(name: str) -> bool:
    """Return True if a distro with *name* is registered (case-insensitive)."""
    lower = name.lower()
    return any(d.lower() == lower for d in list_distros())


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def import_distro(name: str, install_path: Path, rootfs_path: Path) -> None:
    """Import *rootfs_path* as a new WSL2 distro named *name*.

    Raises ``subprocess.CalledProcessError`` on failure.
    """
    install_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["wsl", "--import", name, str(install_path), str(rootfs_path)],
        check=True,
        capture_output=True,
    )


def unregister_distro(name: str) -> None:
    """Unregister (delete) the WSL2 distro named *name*.

    Raises ``subprocess.CalledProcessError`` on failure.
    """
    subprocess.run(
        ["wsl", "--unregister", name],
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


def run_in_distro(name: str, command: str) -> tuple[str, str, int]:
    """Run *command* via sh inside distro *name*.

    Returns ``(stdout, stderr, returncode)``.  Does not raise on non-zero exit.
    """
    result = subprocess.run(
        ["wsl", "-d", name, "--", "sh", "-c", command],
        capture_output=True,
        timeout=120,
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    return stdout, stderr, result.returncode


# ---------------------------------------------------------------------------
# Rootfs download
# ---------------------------------------------------------------------------

_ALPINE_ROOTFS_URL = (
    "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/x86_64/"
    "alpine-minirootfs-3.21.0-x86_64.tar.gz"
)


def default_rootfs_url() -> str:
    """Return the default Alpine rootfs URL."""
    return _ALPINE_ROOTFS_URL


def download_rootfs(
    url: str,
    dest: Path,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Download *url* to *dest*, calling *progress_cb(downloaded_bytes, total_bytes)* if given.

    Raises ``urllib.error.URLError`` on network failure.
    """

    def _reporthook(block_num: int, block_size: int, total_size: int) -> None:
        if progress_cb is not None:
            downloaded = min(block_num * block_size, total_size if total_size > 0 else 0)
            progress_cb(downloaded, total_size)

    urllib.request.urlretrieve(url, str(dest), reporthook=_reporthook)  # noqa: S310


# ---------------------------------------------------------------------------
# Default install path
# ---------------------------------------------------------------------------


def default_install_path(distro_name: str) -> Path:
    """Return the default Windows filesystem path for storing the distro data.

    Uses ``%LOCALAPPDATA%\\aar\\wsl-distros\\<distro_name>``.
    Falls back to the user home directory on non-Windows.
    """
    import os

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        return Path(local_app_data) / "aar" / "wsl-distros" / distro_name
    return Path.home() / ".aar" / "wsl-distros" / distro_name
