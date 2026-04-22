"""JSONL session persistence — save and load sessions to disk."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from agent.core.events import Event, deserialize_event
from agent.core.session import Session

logger = logging.getLogger(__name__)

# Bump this when the JSONL event format changes in a breaking way.
SCHEMA_VERSION = 1

# Session IDs are used as filenames and dict keys. Restrict to a safe charset so
# an attacker-controlled id (e.g. via ACP load_session) cannot traverse out of
# the session directory.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def validate_session_id(session_id: str) -> str:
    """Return *session_id* if it is safe to use as a filename/dict key.

    Raises ``ValueError`` otherwise. Accepts 1–128 chars of ``[A-Za-z0-9_-]``.
    """
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return session_id


class SessionStore:
    """Persists sessions as JSONL files."""

    def __init__(self, base_dir: Path | str = ".agent/sessions") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        validate_session_id(session_id)
        return self.base_dir / f"{session_id}.jsonl"

    def save(self, session: Session) -> Path:
        """Save a session to a JSONL file. Each line is one event."""
        path = self._session_path(session.session_id)

        header = {
            "_meta": True,
            "schema_version": SCHEMA_VERSION,
            "session_id": session.session_id,
            "run_id": session.run_id,
            "trace_id": session.trace_id,
            "state": session.state.value,
            "step_count": session.step_count,
            "metadata": session.metadata,
        }

        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")
            for event in session.events:
                f.write(event.model_dump_json() + "\n")

        logger.info("Saved session %s to %s", session.session_id, path)
        return path

    def load(self, session_id: str) -> Session:
        """Load a session from its JSONL file."""
        path = self._session_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")

        events: list[Event] = []
        header: dict[str, Any] = {}

        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("_meta"):
                    header = data
                else:
                    events.append(deserialize_event(data))

        # Schema version check
        file_version = header.get("schema_version", 0)
        if file_version > SCHEMA_VERSION:
            raise ValueError(
                f"Session '{session_id}' was saved with schema version {file_version}, "
                f"but this version of aar only supports up to version {SCHEMA_VERSION}. "
                f"Upgrade aar to load this session."
            )
        if file_version < SCHEMA_VERSION:
            logger.info(
                "Session '%s' uses schema version %d (current: %d) — migrating",
                session_id,
                file_version,
                SCHEMA_VERSION,
            )

        from agent.core.state import AgentState

        session = Session(
            session_id=header.get("session_id", session_id),
            run_id=header.get("run_id", ""),
            trace_id=header.get("trace_id", ""),
            state=AgentState(header.get("state", "idle")),
            step_count=header.get("step_count", 0),
            metadata=header.get("metadata", {}),
            events=events,
        )

        logger.info("Loaded session %s with %d events", session_id, len(events))
        return session

    def compact(self, session_id: str, max_events: int = 200) -> Session:
        """Truncate a session to its most recent *max_events* events and rewrite the file.

        Compaction keeps the session file from growing without bound in long-running
        or resumed conversations. The compacted session is saved back to disk and
        returned. Callers are responsible for re-injecting any system context that
        may have been pruned (e.g. via the system_prompt in AgentConfig).
        """
        session = self.load(session_id)
        if len(session.events) > max_events:
            session.events = session.events[-max_events:]
            logger.info("Compacted session %s to %d events", session_id, len(session.events))
        self.save(session)
        return session

    def list_sessions(self) -> list[str]:
        """List all stored session IDs."""
        return [p.stem for p in sorted(self.base_dir.glob("*.jsonl"))]

    def delete(self, session_id: str) -> bool:
        """Delete a session file."""
        path = self._session_path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False
