"""
Simple usage of plugin-manager: the app uses the plugin to log and print
"Hello World!".
"""
import logging
from dataclasses import dataclass
from plugin_manager import PluginBaseCapability, PluginBaseConfig, PluginBase

PLUGIN_NAME = "hello_world"
PLUGIN_VERSION = "1.0.0"
PLUGIN_DESCRIPTION = "Print \"hello world!\""
PLUGIN_API_VERSION = "1.0"


class HelloworldCapability(PluginBaseCapability):
    """Describe the public hello-world capability exposed to applications."""
    CAPABILITY_ID = "hello_world"

    def hello(self) -> None:
        """Emit the configured hello-world message."""
        ...


@dataclass(frozen=True)
class HelloworldConfig(PluginBaseConfig):
    """Define hello-world metadata and configurable behavior."""
    name: str = PLUGIN_NAME
    version: str = PLUGIN_VERSION
    description: str = PLUGIN_DESCRIPTION
    api_version: str = PLUGIN_API_VERSION
    author: str | None = None
    license: str | None = None
    plugin_id: str = "hello_world"
    message: str = "HELLO WORLD!"
    module: str = "hello_world_plugin"


class Helloworld(PluginBase, HelloworldCapability):
    """Log and print a configurable hello-world message."""

    Config = HelloworldConfig

    def __init__(self, config, context):
        """Initialize the plugin and select its application logger."""
        super().__init__(config, context)
        self.logger = context.logger or logging.getLogger(__name__)

    def start(self):
        """Emit the configured greeting when the plugin starts."""
        self.logger.info(self.config.message)
        print(self.config.message)
        print(self.config.author)

    def stop(self):
        """Log a farewell when the plugin stops."""
        self.logger.info("Goodbye World!")


PLUGIN_CLASS = Helloworld
