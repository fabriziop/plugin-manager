"""Example direct greeter plugin."""
from __future__ import annotations

from dataclasses import dataclass

from plugin_manager import PluginBaseConfig, PluginBase


PLUGIN_NAME = "example_direct"
PLUGIN_VERSION = "1.0.0"
PLUGIN_DESCRIPTION = "Simple direct greeter plugin."
PLUGIN_AUTHOR = "Fabrizio Pollastri"
PLUGIN_LICENSE = "GPL-3.0-or-later"
PLUGIN_API_VERSION = "1.0"


@dataclass(frozen=True)
class DirectConfig(PluginBaseConfig):
    """Configure metadata and greeting text for the direct plugin."""

    name: str = ""
    description: str = ""
    author: str | None = None
    license: str | None = None
    greeting: str = "Hello"


class DirectGreeter(PluginBase):
    """Expose a direct greeting method to the application."""

    Config = DirectConfig

    def __init__(self, config: DirectConfig, context) -> None:
        """Store config and context and initialize state flags."""
        self.config = config
        self.context = context
        self.started = False
        self.stopped = False

    def start(self) -> None:
        """Mark the plugin as started."""
        self.started = True

    def stop(self) -> None:
        """Mark the plugin as stopped."""
        self.stopped = True

    def greet(self, name: str) -> str:
        """Return a greeting for the supplied name."""
        return f"{self.config.greeting}, {name}!"


PLUGIN_CLASS = DirectGreeter
