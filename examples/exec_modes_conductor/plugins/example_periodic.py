"""Example task-executed plugin with a queue-backed worker."""
from __future__ import annotations

from dataclasses import dataclass
from queue import Queue
from typing import Any

from plugin_manager import PluginBaseConfig, PluginBase


PLUGIN_NAME = "example_periodic"
PLUGIN_VERSION = "1.0.0"
PLUGIN_DESCRIPTION = "Periodic worker intended for task scheduling."
PLUGIN_AUTHOR = "Fabrizio Pollastri"
PLUGIN_LICENSE = "GPL-3.0-or-later"
PLUGIN_API_VERSION = "1.0"


@dataclass(frozen=True)
class WorkerConfig(PluginBaseConfig):
    """Configure metadata and queue behavior for the periodic plugin."""

    name: str = ""
    description: str = ""
    author: str | None = None
    license: str | None = None
    message: str = "tick"
    queue_name: str = "events"


class PeriodicExample(PluginBase):
    """Publish periodic work results through a queue."""

    Config = WorkerConfig

    def __init__(self, config: WorkerConfig, context) -> None:
        """Create the queue and initialize the run counter."""
        self.config = config
        self.context = context
        self.count = 0
        context.queues[config.queue_name] = Queue[Any]()

    def run_once(self) -> None:
        """Publish one numbered message to the configured queue."""
        self.count += 1
        self.context.queues[self.config.queue_name].put((self.count, self.config.message))

    @property
    def events(self):
        """Return the queue exposed to the application."""
        return self.context.queues[self.config.queue_name]


PLUGIN_CLASS = PeriodicExample
