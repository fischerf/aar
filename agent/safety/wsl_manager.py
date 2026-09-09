"""WSL2 distro management helpers for the Aar WSL sandbox."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from urllib.parse import quote

logger = logging.getLogger(__name__)


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


def prepare_import_path(install_path: Path, *, force: bool = False) -> bool:
    """Ensure a WSL import path is empty, optionally removing stale contents.

    Returns ``True`` when a stale directory was removed. A non-empty path is
    never removed without an explicit ``force=True`` request.
    """
    if not install_path.exists():
        return False
    if not install_path.is_dir():
        raise ValueError(f"WSL install path exists but is not a directory: {install_path}")
    if not any(install_path.iterdir()):
        return False
    if not force:
        raise FileExistsError(
            f"WSL install path is not empty but its distro is not registered: {install_path}"
        )
    shutil.rmtree(install_path)
    return True


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


def run_in_distro(name: str, command: str, timeout: int = 120) -> tuple[str, str, int]:
    """Run *command* via sh inside distro *name*.

    Returns ``(stdout, stderr, returncode)``.  Does not raise on non-zero exit.

    Args:
        name:    WSL2 distro name.
        command: Shell command to execute inside the distro.
        timeout: Seconds to wait before raising ``subprocess.TimeoutExpired``.
                 Default is 120 s — use a larger value for long-running steps
                 such as package installation.
    """
    result = subprocess.run(
        ["wsl", "-d", name, "--", "sh", "-c", command],
        capture_output=True,
        timeout=timeout,
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    return stdout, stderr, result.returncode


def install_alpine_packages_from_host(
    name: str,
    packages: list[str],
    timeout: int = 600,
) -> tuple[str, str, int]:
    """Download Alpine packages on Windows and install them from a mounted cache.

    This supports managed environments where Windows has network access but WSL2
    egress is blocked. Alpine's signed repository indexes are downloaded on the
    host, dependency resolution runs inside the distro without network access,
    and the selected APKs are then downloaded on the host and installed via
    ``/mnt/<drive>``. APK signature verification remains enabled.
    """
    repositories_out, repositories_err, repositories_rc = run_in_distro(
        name,
        "cat /etc/apk/repositories",
    )
    if repositories_rc != 0:
        return "", repositories_err or "Could not read /etc/apk/repositories", repositories_rc

    repositories = _parse_alpine_repositories(repositories_out)
    if not repositories:
        return "", "No HTTP(S) Alpine repositories found in /etc/apk/repositories", 1

    arch_out, arch_err, arch_rc = run_in_distro(name, "apk --print-arch")
    architecture = arch_out.strip()
    if arch_rc != 0 or not architecture:
        return "", arch_err or "Could not determine the Alpine package architecture", arch_rc or 1

    with tempfile.TemporaryDirectory(prefix="aar-apk-") as temp_dir:
        cache_dir = Path(temp_dir)
        cache_wsl = _to_wsl_mount_path(cache_dir)
        cache_uri_path = quote(cache_wsl, safe="/")
        local_repositories: list[str] = []

        for index, repository in enumerate(repositories):
            repo_dir = cache_dir / f"repo-{index}" / architecture
            repo_dir.mkdir(parents=True)
            _download_host_file(
                f"{repository}/{architecture}/APKINDEX.tar.gz",
                repo_dir / "APKINDEX.tar.gz",
            )
            local_repositories.append(f"file://{cache_uri_path}/repo-{index}")

        repositories_file = cache_dir / "repositories"
        repositories_file.write_bytes(("\n".join(local_repositories) + "\n").encode())
        repositories_wsl = f"{cache_wsl}/repositories"
        quoted_packages = " ".join(shlex.quote(package) for package in packages)
        fetch_command = (
            "apk fetch --simulate --recursive --url --no-network "
            f"--repositories-file {shlex.quote(repositories_wsl)} {quoted_packages}"
        )
        fetch_out, fetch_err, fetch_rc = run_in_distro(name, fetch_command, timeout=timeout)
        if fetch_rc != 0:
            return fetch_out, fetch_err, fetch_rc

        package_urls = [line.strip() for line in fetch_out.splitlines() if line.endswith(".apk")]
        if not package_urls:
            return fetch_out, "Alpine dependency resolution returned no package files", 1

        for package_url in package_urls:
            match = re.search(r"/repo-(\d+)/[^/]+/([^/]+\.apk)$", package_url)
            if match is None:
                return fetch_out, f"Unexpected Alpine package URL: {package_url}", 1
            repository_index = int(match.group(1))
            if repository_index >= len(repositories):
                return fetch_out, f"Unknown Alpine repository in package URL: {package_url}", 1
            filename = match.group(2)
            destination = cache_dir / f"repo-{repository_index}" / architecture / filename
            _download_host_file(
                f"{repositories[repository_index]}/{architecture}/{filename}",
                destination,
            )

        install_command = (
            "apk add --no-network "
            f"--repositories-file {shlex.quote(repositories_wsl)} {quoted_packages}"
        )
        return run_in_distro(name, install_command, timeout=timeout)


def _parse_alpine_repositories(contents: str) -> list[str]:
    """Extract HTTP(S) repository URLs from an Alpine repositories file."""
    repositories: list[str] = []
    for raw_line in contents.splitlines():
        line = raw_line.partition("#")[0].strip()
        if not line:
            continue
        repository = line.split()[-1].rstrip("/")
        if repository.startswith(("https://", "http://")):
            repositories.append(repository)
    return repositories


def _to_wsl_mount_path(path: Path) -> str:
    """Translate an absolute Windows host path to its default WSL mount path."""
    windows_path = PureWindowsPath(path.resolve())
    if not windows_path.drive:
        raise ValueError(f"Cannot translate host package cache path to WSL: {path}")
    drive = windows_path.drive.rstrip(":").lower()
    rest = "/".join(windows_path.parts[1:])
    return f"/mnt/{drive}/{rest}"


# ---------------------------------------------------------------------------
# Host downloads / rootfs download
# ---------------------------------------------------------------------------

_ALPINE_ROOTFS_URL = (
    "https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/x86_64/"
    "alpine-minirootfs-3.23.0-x86_64.tar.gz"
)


def default_rootfs_url() -> str:
    """Return the default Alpine rootfs URL."""
    return _ALPINE_ROOTFS_URL


def download_rootfs(
    url: str,
    dest: Path,
    progress_cb: Callable[[int, int], None] | None = None,
    expected_sha256: str | None = None,
) -> None:
    """Download *url* to *dest*, calling *progress_cb(downloaded_bytes, total_bytes)* if given.

    Raises a download error when both urllib and the Windows curl fallback fail.

    S6 — If *expected_sha256* is provided, the downloaded file's SHA-256 is
    computed and compared (case-insensitive). On mismatch the partially
    downloaded file is unlinked and ``ValueError`` is raised so the caller
    aborts before importing a tampered rootfs. When *expected_sha256* is
    None, a loud warning is emitted but the download is kept (legacy
    configs without a checksum should still work).
    """

    def _reporthook(block_num: int, block_size: int, total_size: int) -> None:
        if progress_cb is not None:
            downloaded = min(block_num * block_size, total_size if total_size > 0 else 0)
            progress_cb(downloaded, total_size)

    _download_host_file(url, dest, reporthook=_reporthook)

    if not expected_sha256:
        logger.warning(
            "rootfs downloaded from %s without sha256 verification — supply "
            "`rootfs_sha256` in your WSL profile for integrity protection.",
            url,
        )
        return

    actual = _sha256_of_file(dest)
    if actual.lower() != expected_sha256.lower():
        try:
            dest.unlink()
        except OSError:
            pass
        raise ValueError(
            f"rootfs SHA-256 mismatch for {url}: expected {expected_sha256.lower()}, "
            f"got {actual}. The downloaded file has been deleted; refusing to "
            f"import a potentially tampered rootfs."
        )


def _download_host_file(
    url: str,
    dest: Path,
    reporthook: Callable[[int, int, int], None] | None = None,
) -> None:
    """Download on the host, falling back to Windows' certificate-aware curl."""
    try:
        urllib.request.urlretrieve(url, str(dest), reporthook=reporthook)
        return
    except urllib.error.URLError:
        if os.name != "nt" or not url.startswith(("https://", "http://")):
            raise
        logger.warning("urllib could not download %s; retrying with curl.exe", url)

    subprocess.run(
        [
            "curl.exe",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--output",
            str(dest),
            url,
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )


def _sha256_of_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute the SHA-256 of *path* by streaming chunks (avoid loading large rootfs into RAM)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Default install path
# ---------------------------------------------------------------------------


def default_install_path(distro_name: str) -> Path:
    """Return the default Windows filesystem path for storing the distro data.

    Uses ``%LOCALAPPDATA%\\aar\\wsl-distros\\<distro_name>``.
    Falls back to the user home directory on non-Windows.
    """
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        return Path(local_app_data) / "aar" / "wsl-distros" / distro_name
    return Path.home() / ".aar" / "wsl-distros" / distro_name
