"""JSONL session persistence — save and load sessions to disk."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agent.core.events import Event, deserialize_event
from agent.core.session import Session

logger = logging.getLogger(__name__)


class SessionStore:
    """Persists sessions as JSONL files."""

    def __init__(self, base_dir: Path | str = ".agent/sessions") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        return self.base_dir / f"{session_id}.jsonl"

    def save(self, session: Session) -> Path:
        """Save a session to a JSONL file. Each line is one event."""
        path = self._session_path(session.session_id)

        # Write header line with session metadata
        header = {
            "_meta": True,
            "session_id": session.session_id,
            "run_id": session.run_id,
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
            for line_num, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("_meta"):
                    header = data
                else:
                    events.append(deserialize_event(data))

        from agent.core.state import AgentState

        session = Session(
            session_id=header.get("session_id", session_id),
            run_id=header.get("run_id", ""),
            state=AgentState(header.get("state", "idle")),
            step_count=header.get("step_count", 0),
            metadata=header.get("metadata", {}),
            events=events,
        )

        logger.info("Loaded session %s with %d events", session_id, len(events))
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
