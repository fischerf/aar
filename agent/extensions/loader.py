from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import importlib.util
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.extensions.api import ExtensionAPI
from agent.extensions.trust import env_trust_override, is_project_trusted, trust_project

logger = logging.getLogger(__name__)

#: Signature of the interactive trust prompt: it receives the extension
#: directory and the extensions discovered in it, and returns ``"yes"`` (this
#: run only), ``"always"`` (record the tree hash) or ``"no"``.
TrustPrompt = Callable[[Path, "list[ExtensionInfo]"], str]

_DEFAULT_USER_DIR = Path.home() / ".aar" / "extensions"
_DEFAULT_PROJECT_DIR = Path(".agent") / "extensions"


@dataclass
class ExtensionInfo:
    """Metadata about a discovered extension."""

    name: str
    source: str  # "entrypoint", "user", "project"
    path: str | None  # file path or entry point spec
    api: ExtensionAPI | None = field(default=None, repr=False)
    error: str | None = None


def discover_extensions(
    *,
    user_dir: Path | None = None,
    project_dir: Path | None = None,
    trust_prompt: TrustPrompt | None = None,
    force_trust: bool = False,
) -> list[ExtensionInfo]:
    """Discover extensions from all three tiers.

    User extensions shadow entry points.  C3 — project extensions no longer
    shadow anything, and are only returned at all once the project has been
    trusted.

    Args:
        trust_prompt: Interactive callback ``(dir, infos) -> "yes"|"always"|"no"``.
            Only interactive transports supply one; without it an untrusted
            project's extensions are skipped.
        force_trust: Bypass the prompt entirely (``--trust-project-extensions``
            or ``$AAR_TRUST_PROJECT_EXTENSIONS``).
    """
    user_dir = user_dir if user_dir is not None else _DEFAULT_USER_DIR
    project_dir = project_dir if project_dir is not None else _DEFAULT_PROJECT_DIR

    entrypoint_exts = _discover_entrypoints()
    user_exts = _discover_directory(user_dir, "user")
    project_exts = _discover_directory(project_dir, "project")
    project_exts = _gate_project_extensions(
        project_dir, project_exts, trust_prompt=trust_prompt, force_trust=force_trust
    )

    # Build a name -> info map, later tiers shadow earlier ones
    seen: dict[str, ExtensionInfo] = {}
    for info in entrypoint_exts:
        seen[info.name] = info
    for info in user_exts:
        if info.name in seen:
            logger.debug("User extension %r shadows entrypoint extension", info.name)
        seen[info.name] = info
    for info in project_exts:
        # C3(b) — Shadowing is legitimate for user → entrypoint (the user
        # installed both). A directory that arrived via ``git clone`` must not
        # be able to replace ``permission_gate`` (or any other installed
        # extension) with a no-op.
        if info.name in seen:
            logger.warning(
                "Ignoring project extension %r at %s: it would shadow the %s extension "
                "of the same name.",
                info.name,
                info.path,
                seen[info.name].source,
            )
            continue
        seen[info.name] = info

    result = list(seen.values())
    logger.info(
        "Discovered %d extension(s): %s",
        len(result),
        [f"{e.name} ({e.source})" for e in result],
    )
    return result


def _gate_project_extensions(
    project_dir: Path,
    infos: list[ExtensionInfo],
    *,
    trust_prompt: TrustPrompt | None = None,
    force_trust: bool = False,
) -> list[ExtensionInfo]:
    """Return *infos* only if the project's extension tree is trusted."""
    if not infos:
        return infos

    if force_trust or env_trust_override():
        logger.info(
            "Loading %d project extension(s) from %s (trust override)", len(infos), project_dir
        )
        return infos

    if is_project_trusted(project_dir):
        logger.info("Loading %d trusted project extension(s) from %s", len(infos), project_dir)
        return infos

    if trust_prompt is None:
        # Non-interactive transports (``run``, ``serve``, ``acp --http``): there
        # is nobody to ask, so executing repository-supplied code is not an
        # option.
        logger.warning(
            "Skipping %d untrusted project extension(s) in %s: %s. "
            "Run an interactive transport to approve them, or set %s=1.",
            len(infos),
            project_dir,
            [i.name for i in infos],
            "AAR_TRUST_PROJECT_EXTENSIONS",
        )
        return []

    try:
        answer = str(trust_prompt(Path(project_dir), infos) or "no").strip().lower()
    except Exception:
        logger.exception("Project extension trust prompt failed; treating as 'no'")
        return []

    if answer == "always":
        trust_project(Path(project_dir))
        return infos
    if answer in ("yes", "y", "once"):
        return infos
    logger.info("User declined %d project extension(s) in %s", len(infos), project_dir)
    return []


def _discover_entrypoints() -> list[ExtensionInfo]:
    """Discover extensions registered via ``aar_extensions`` entry-point group."""
    infos: list[ExtensionInfo] = []
    try:
        eps = importlib.metadata.entry_points(group="aar_extensions")
    except Exception:
        logger.debug("No entry-point group 'aar_extensions' found")
        return infos

    for ep in eps:
        infos.append(
            ExtensionInfo(
                name=ep.name,
                source="entrypoint",
                path=ep.value,
            )
        )
        logger.debug("Found entrypoint extension %r -> %s", ep.name, ep.value)
    return infos


def _discover_directory(directory: Path, source: str) -> list[ExtensionInfo]:
    """Scan a directory for ``.py`` files and packages (dirs with ``__init__.py``)."""
    infos: list[ExtensionInfo] = []
    if not directory.is_dir():
        logger.debug("Extension directory does not exist: %s", directory)
        return infos

    for child in sorted(directory.iterdir()):
        if child.name.startswith(("_", ".")):
            continue

        if child.is_file() and child.suffix == ".py":
            name = child.stem
            infos.append(ExtensionInfo(name=name, source=source, path=str(child)))
            logger.debug("Found %s extension file %r at %s", source, name, child)

        elif child.is_dir() and (child / "__init__.py").is_file():
            name = child.name
            infos.append(ExtensionInfo(name=name, source=source, path=str(child)))
            logger.debug("Found %s extension package %r at %s", source, name, child)

    return infos


async def load_extension(info: ExtensionInfo) -> ExtensionAPI:
    """Import the extension module, call ``register(api)``, return the populated API."""
    api = ExtensionAPI(name=info.name)

    try:
        register_fn = _import_register(info)
    except Exception as exc:
        raise RuntimeError(f"Failed to import extension {info.name!r}: {exc}") from exc

    try:
        if asyncio.iscoroutinefunction(register_fn):
            await register_fn(api)
        else:
            register_fn(api)
    except Exception as exc:
        raise RuntimeError(f"register() failed for extension {info.name!r}: {exc}") from exc

    logger.info(
        "Loaded extension %r (%s): %d tool(s), %d event handler group(s), %d command(s)",
        info.name,
        info.source,
        len(api._tools),
        len(api._event_handlers),
        len(api._commands),
    )
    return api


def _import_register(info: ExtensionInfo) -> Any:
    """Return the ``register`` callable for a given extension info."""
    if info.source == "entrypoint":
        # path is "module.path:register"
        assert info.path is not None
        if ":" in info.path:
            module_path, attr_name = info.path.rsplit(":", 1)
        else:
            module_path = info.path
            attr_name = "register"
        module = importlib.import_module(module_path)
        fn = getattr(module, attr_name, None)
        if fn is None:
            raise AttributeError(f"Module {module_path!r} has no attribute {attr_name!r}")
        return fn

    # File or package on disk
    assert info.path is not None
    path = Path(info.path)

    if path.is_dir():
        # Package — load __init__.py
        init_file = path / "__init__.py"
        module_name = f"aar_ext_{info.name}"
        spec = importlib.util.spec_from_file_location(module_name, init_file)
    else:
        module_name = f"aar_ext_{info.name}"
        spec = importlib.util.spec_from_file_location(module_name, path)

    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {info.path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    fn = getattr(module, "register", None)
    if fn is None:
        raise AttributeError(f"Extension module at {info.path!r} has no 'register' function")
    return fn


async def load_all_extensions(
    *,
    user_dir: Path | None = None,
    project_dir: Path | None = None,
    trust_prompt: TrustPrompt | None = None,
    force_trust: bool = False,
) -> list[ExtensionInfo]:
    """Discover and load all extensions, returning info with populated ``.api`` or ``.error``."""
    infos = discover_extensions(
        user_dir=user_dir,
        project_dir=project_dir,
        trust_prompt=trust_prompt,
        force_trust=force_trust,
    )

    for info in infos:
        try:
            info.api = await load_extension(info)
        except Exception as exc:
            info.error = str(exc)
            logger.warning("Failed to load extension %r: %s", info.name, exc)

    loaded = sum(1 for i in infos if i.api is not None)
    failed = sum(1 for i in infos if i.error is not None)
    logger.info("Extension loading complete: %d loaded, %d failed", loaded, failed)
    return infos
