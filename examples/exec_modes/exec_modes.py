"""End-to-end use case using the same plugins and configuration as the tests.

Run from the project root with:

    python examples/use_case_same_plugins.py

The example injects a tiny in-process task-backend stand-in so it is runnable even
when the real task backend is not installed. The plugin manager
API is exercised the same way an application would use it: load configured
plugins, read plugin attributes, start task execution, read queue output, and stop
cleanly.
"""
from __future__ import annotations

import sys
from pathlib import Path
from queue import Empty
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from plugin_manager import PluginManager


class DemoScheduler:
    """Small synchronous stand-in for the backend scheduler used by the example.

    It accepts the same subset of Scheduler methods used by PluginManager and
    executes finite-count tasks immediately during start_engine(). This keeps the
    example deterministic while preserving the real integration boundary.
    """

    def __init__(self, default_pool_workers: int = 1) -> None:
        """Initialize scheduler state used by the demonstration."""
        self.default_pool_workers = default_pool_workers
        self.pools: dict[str, int] = {"default": default_pool_workers}
        self.tasks: list[dict[str, Any]] = []
        self.started = False

    def create_pool(self, pool_id: str, workers: int = 1) -> None:
        """Record a named worker pool."""
        self.pools[pool_id] = workers

    def add_task(self, **kwargs: Any) -> str:
        """Record a task declaration and return its identifier."""
        task_id = str(kwargs.get("task_id"))
        self.tasks.append(kwargs)
        return task_id

    def start_engine(self) -> None:
        """Execute each recorded task synchronously."""
        if not self.tasks:
            raise RuntimeError("no tasks scheduled")
        self.started = True
        for spec in self.tasks:
            task = spec["task"]
            count = spec.get("count")
            runs = count if isinstance(count, int) and count > 0 else 1
            for _ in range(runs):
                task()

    def stop(self) -> None:
        """Mark the demonstration scheduler as stopped."""
        self.started = False

    def clear_tasks(self) -> None:
        """Discard all recorded tasks."""
        self.tasks.clear()

    def read_stats(self) -> dict[str, Any]:
        return {"running": self.started, "task_count": len(self.tasks)}

    def read_jobs(self) -> list[dict[str, Any]]:
        return [
            {
                "id": task.get("task_id"),
                "pool_id": task.get("pool_id") or "default",
                "period_us": task.get("period_us"),
                "task_error": "",
            }
            for task in self.tasks
        ]

    def read_pool_stats(self, pool_id: str | None = None) -> Any:
        if pool_id is None:
            return {name: {"workers": workers} for name, workers in self.pools.items()}
        return {"workers": self.pools[pool_id]}


class DemoTaskManager:
    """Application-owned manager used only for task registration by PM."""

    def __init__(self) -> None:
        """Create the application-owned demonstration scheduler."""
        self.scheduler = DemoScheduler()

    def add_task(self, **task_spec: Any) -> str:
        return self.scheduler.add_task(**task_spec)

    def suspend(self, task_id: str, for_: int = 0) -> str:
        for task in self.scheduler.tasks:
            if str(task.get("task_id")) == task_id:
                task["suspended"] = True
                break
        return "forever" if for_ == 0 else str(for_)



def drain_queue(q: Any) -> list[Any]:
    """Return all currently available items from a queue."""
    items: list[Any] = []
    while True:
        try:
            items.append(q.get_nowait())
        except Empty:
            return items


def main() -> None:
    """Run the complete direct and scheduled-task example."""
    import plugin_config

    # Combine the example config with an application-owned task manager.
    config = {
        "PLUGIN_MANAGER": {**plugin_config.PLUGIN_MANAGER, "task_manager": DemoTaskManager()},
        "PLUGINS": plugin_config.PLUGINS,
    }
    manager = PluginManager(config, {"app_root": Path(__file__).resolve().parent})

    # PM owns plugin lifecycle and registration of plugin-requested tasks.
    manager.start()
    task_manager = manager.config.task_manager
    task_manager.scheduler.start_engine()  # application-owned lifecycle
    try:
        # Application code does not import plugin modules or plugin classes.
        # It asks the manager for named attributes of named plugin instances.
        greet = manager.get_plugin_attribute("example_direct", "greet")
        print(greet("Fabrizio"))

        event_queue = manager.get_plugin_attribute("example_periodic", "events")
        events = drain_queue(event_queue)
        print(f"worker events: {events}")

        print(f"task jobs: {task_manager.scheduler.read_jobs()}")
    finally:
        task_manager.scheduler.stop()  # application-owned lifecycle
        manager.close()


if __name__ == "__main__":
    main()
