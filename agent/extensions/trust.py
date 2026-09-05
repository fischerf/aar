"""C3 — trust gate for project-local extensions.

``.agent/extensions/*.py`` in the current working directory used to be imported
and executed on the first ``agent.run()`` with no prompt, allow-list or
signature check.  Cloning a repository and typing ``aar chat`` inside it ran
whatever Python that repository shipped, as the user, *before* any policy or
approval logic was consulted — and because the project tier shadowed both the
user tier and installed entry points, the same file could register under the
name of a safety extension and replace it with a no-op.

This module records per-project trust decisions in
``~/.aar/trusted_projects.json``, keyed by the project root's real path and
carrying a hash of the extension tree, so any later edit re-prompts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

TRUST_DB_PATH = Path.home() / ".aar" / "trusted_projects.json"

#: Set to 1/true/yes to trust project extensions without prompting (CI).
TRUST_ENV_VAR = "AAR_TRUST_PROJECT_EXTENSIONS"


def env_trust_override() -> bool:
    """True when ``$AAR_TRUST_PROJECT_EXTENSIONS`` opts in."""
    return os.environ.get(TRUST_ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def project_root(project_dir: Path) -> str:
    """The trust key for *project_dir* — the directory containing ``.agent``."""
    resolved = Path(project_dir).resolve()
    # ``.agent/extensions`` → project root is two levels up; be tolerant of a
    # caller passing a custom directory by walking up only what exists.
    return str(resolved.parent.parent if resolved.parent.name == ".agent" else resolved.parent)


def tree_hash(directory: Path) -> str:
    """Stable SHA-256 over every file in *directory* (path + contents).

    Any edit, addition or removal changes the digest, so "always trust" is
    scoped to the exact code the user was shown.
    """
    digest = hashlib.sha256()
    directory = Path(directory)
    if not directory.is_dir():
        return digest.hexdigest()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        try:
            rel = path.relative_to(directory).as_posix()
        except ValueError:  # pragma: no cover - defensive
            rel = path.name
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError as exc:  # pragma: no cover - unreadable file
            logger.debug("tree_hash: cannot read %s: %s", path, exc)
        digest.update(b"\0")
    return digest.hexdigest()


def load_trust_db(path: Path | None = None) -> dict[str, dict]:
    """Read the trust database, tolerating a missing or corrupt file."""
    db_path = path or TRUST_DB_PATH
    if not db_path.is_file():
        return {}
    try:
        data = json.loads(db_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable trust database %s: %s", db_path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_trust_db(db: dict[str, dict], path: Path | None = None) -> None:
    db_path = path or TRUST_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(db_path.suffix + ".tmp")
    tmp.write_text(json.dumps(db, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(db_path)


def is_project_trusted(project_dir: Path, *, db_path: Path | None = None) -> bool:
    """True when this exact extension tree was previously trusted."""
    entry = load_trust_db(db_path).get(project_root(project_dir))
    if not isinstance(entry, dict):
        return False
    return entry.get("sha256") == tree_hash(project_dir)


def trust_project(project_dir: Path, *, db_path: Path | None = None) -> None:
    """Record the current extension tree as trusted for this project."""
    db = load_trust_db(db_path)
    key = project_root(project_dir)
    db[key] = {"sha256": tree_hash(project_dir), "path": str(Path(project_dir).resolve())}
    save_trust_db(db, db_path)
    logger.info("Recorded trust for project extensions in %s", key)
