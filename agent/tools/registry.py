"""Tool registry — register, look up, and list tools."""

from __future__ import annotations

import inspect
from typing import Any, Callable

from agent.tools.schema import SideEffect, ToolSpec


class ToolRegistry:
    """Central registry for all available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str | None = None,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
        side_effects: list[SideEffect] | None = None,
        requires_approval: bool = False,
    ) -> Callable:
        """Decorator to register a tool function.

        Usage:
            @registry.register(name="read_file", description="Read a file")
            async def read_file(path: str) -> str:
                ...
        """

        def decorator(fn: Callable) -> Callable:
            tool_name = name or fn.__name__
            tool_desc = description or fn.__doc__ or ""
            schema = input_schema or _infer_schema(fn)
            spec = ToolSpec(
                name=tool_name,
                description=tool_desc,
                input_schema=schema,
                side_effects=side_effects or [SideEffect.NONE],
                requires_approval=requires_approval,
                handler=fn,
            )
            self._tools[tool_name] = spec
            return fn

        return decorator

    def add(self, spec: ToolSpec) -> None:
        """Register a tool from an existing ToolSpec."""
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def to_provider_schemas(self) -> list[dict[str, Any]]:
        """Return all tools in provider-compatible schema format."""
        return [t.to_provider_schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


def _infer_schema(fn: Callable) -> dict[str, Any]:
    """Infer a JSON schema from function type hints."""
    import typing

    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []

    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
    }

    # Resolve string annotations from `from __future__ import annotations`
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}

    for name, param in sig.parameters.items():
        annotation = hints.get(name, param.annotation)
        json_type = type_map.get(annotation, "string")
        properties[name] = {"type": json_type}
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }
