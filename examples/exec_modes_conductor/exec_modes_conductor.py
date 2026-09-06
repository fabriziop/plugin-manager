"""Run the execution-modes example with system-installed Conductor.

Run from the project root with::

    python examples/exec_modes_conductor/exec_modes_conductor.py

The application owns the real Conductor scheduler and its lifecycle.
PluginManager only registers tasks requested by plugins. The application
owns task suspension, removal, cleanup, and scheduler lifecycle.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from queue import Empty
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    import conductor
except ImportError as exc:
    raise SystemExit(
        "Conductor is not installed for this Python interpreter. "
        "Install the system package and verify it with: "
        "python3 -c 'import conductor; print(conductor.Scheduler)'"
    ) from exc

from plugin_manager import PluginManager


class ConductorTaskManager:
    """Adapt a real Conductor scheduler for PM task registration."""

    def __init__(self, scheduler: Any) -> None:
        """Store the application-owned scheduler."""
        self.scheduler = scheduler

    def add_task(self, **task_spec: Any) -> str:
        """Register one plugin-requested task with Conductor."""
        return str(self.scheduler.add_task(**task_spec))

    def suspend(self, task_id: str, for_: int = 0) -> Any:
        """Suspend one PM-created task when it is still active.

        Conductor rejects suspension of a finite task that has already
        completed. Treat that state as a successful no-op so shutdown is
        idempotent, while allowing unrelated scheduler errors to propagate.
        """
        try:
            return self.scheduler.suspend(task_id=task_id, for_=for_)
        except RuntimeError as exc:
            if "cannot suspend inactive task" in str(exc):
                return None
            raise



def drain_queue(queue: Any) -> list[Any]:
    """Return all items currently available from a queue."""
    items: list[Any] = []
    while True:
        try:
            items.append(queue.get_nowait())
        except Empty:
            return items


def wait_until_finished(scheduler: Any) -> None:
    """Wait until finite Conductor tasks finish naturally."""
    while scheduler.read_stats()["running"]:
        time.sleep(0.01)


def main() -> None:
    """Run direct and periodic plugins through real Conductor."""
    import plugin_config

    # The application creates and configures the concrete scheduler.
    scheduler = conductor.Scheduler(default_pool_workers=1)
    scheduler.create_pool("fast", workers=2)
    task_manager = ConductorTaskManager(scheduler)

    # Inject the already-created manager into PluginManager configuration.
    config = {
        "PLUGIN_MANAGER": {
            **plugin_config.PLUGIN_MANAGER,
            "task_manager": task_manager,
        },
        "PLUGINS": plugin_config.PLUGINS,
    }
    context = {"app_root": EXAMPLE_ROOT}
    manager = PluginManager(config, context)

    # PM starts plugins and registers only plugin-requested tasks.
    manager.start()

    # The application owns Conductor startup, waiting, and shutdown.
    scheduler.start_engine()
    try:
        wait_until_finished(scheduler)

        greet = manager.get_plugin_attribute(
            "example_direct",
            "greet",
        )
        print(greet("Fabrizio"))

        events = manager.get_plugin_attribute(
            "example_periodic",
            "events",
        )
        print(f"worker events: {drain_queue(events)}")
        print(f"task jobs: {scheduler.read_jobs()}")
    finally:
        scheduler.stop()
        manager.close()

        # Task cleanup is an explicit application decision.
        scheduler.clear_tasks()


if __name__ == "__main__":
    main()
