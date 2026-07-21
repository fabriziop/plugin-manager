"""Run the minimal hello-world PluginManager example."""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from plugin_manager import PluginManager


class AppTaskManager:
    """Application-owned no-op manager for an example with no scheduled tasks."""

    def add_task(self, **task_spec):
        """Return the requested ID; this example schedules no tasks."""
        return str(task_spec["task_id"])

    def suspend(self, task_id, for_=0):
        """Accept PM shutdown; this example has no scheduled tasks."""
        return "forever"


CONFIG = {
    "PLUGIN_MANAGER": {
        "plugin_dir": "",
        "task_manager": AppTaskManager(),
    },
    "PLUGINS": {
        "hello_world": {
            "enabled": True,
            "module": "hello_world_plugin",
        }
    }
}

def main() -> None:
    """Load, start, and stop the minimal hello-world plugin."""
    # Configure the application service passed through plugin context.
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    # Build and run the manager with application-owned dictionaries.
    pm = PluginManager(
        CONFIG,
        {"logger": logger, "app_root": Path(__file__).resolve().parent}
    )
    pm.load()
    pm.start()
    pm.stop()

if __name__ == "__main__":
    main()
