"""Built-in adapters and the scanner-facing adapter registry."""

from __future__ import annotations

from typing import Dict, Tuple

from sidecar.adapters.base import Adapter

registry: Dict[str, Adapter] = {}
ADAPTERS = registry


def register_adapter(adapter: Adapter, replace: bool = False) -> Adapter:
    """Register an adapter under its canonical name and emitted agent names."""

    names = tuple(dict.fromkeys((adapter.name,) + tuple(adapter.agent_names)))
    conflicts = {
        name: registry[name]
        for name in names
        if name in registry and registry[name] is not adapter
    }
    if conflicts and not replace:
        raise ValueError("adapter name already registered: {}".format(sorted(conflicts)[0]))
    if replace:
        replaced = {id(current) for current in conflicts.values()}
        for name, current in tuple(registry.items()):
            if id(current) in replaced:
                del registry[name]
    for name in names:
        registry[name] = adapter
    return adapter


def get_adapter(agent_name: str) -> Adapter:
    return registry[agent_name]


def iter_adapters() -> Tuple[Adapter, ...]:
    """Return each adapter once, even though aliases share registry entries."""

    unique = []
    seen = set()
    for adapter in registry.values():
        identity = id(adapter)
        if identity not in seen:
            seen.add(identity)
            unique.append(adapter)
    return tuple(unique)


from sidecar.adapters.claude import ClaudeAdapter  # noqa: E402
from sidecar.adapters.codex import CodexAdapter  # noqa: E402
from sidecar.adapters.copilot import CopilotAdapter  # noqa: E402
from sidecar.adapters.cursor import CursorAdapter  # noqa: E402
from sidecar.adapters.dsh import DSHAdapter  # noqa: E402
from sidecar.adapters.kimi import KimiAdapter  # noqa: E402

_BUILTIN_ADAPTERS = (
    CursorAdapter(),
    ClaudeAdapter(),
    CodexAdapter(),
    CopilotAdapter(),
    DSHAdapter(),
    KimiAdapter(),
)
for _adapter in _BUILTIN_ADAPTERS:
    register_adapter(_adapter)

__all__ = [
    "ADAPTERS",
    "Adapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "CopilotAdapter",
    "CursorAdapter",
    "DSHAdapter",
    "KimiAdapter",
    "get_adapter",
    "iter_adapters",
    "register_adapter",
    "registry",
]
