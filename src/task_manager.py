"""Task-manager protocol consumed by :mod:`plugin_manager`.

The application owns the concrete task manager and its global lifecycle.
PluginManager adds tasks requested by plugins and suspends those tasks when
it stops. Starting, stopping, resuming, removing, and clearing tasks remain
application responsibilities.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TaskManager(Protocol):
    """Minimal structural interface required by :class:`PluginManager`."""

    def add_task(self, **task_spec: Any) -> str:
        """Register one plugin-requested task and return its identifier."""
        ...

    def suspend(self, task_id: str, for_: int = 0) -> Any:
        """Suspend one task until the application explicitly resumes it.

        Implementations should treat an already inactive or suspended task
        as a successful no-op so PluginManager shutdown remains idempotent.
        """
        ...
