"""Public API for the plugin_manager package."""

try:  # package import
    from .plugin_base import PluginBaseCapability, PluginBaseConfig, PluginBase
    from .plugin_manager import PluginAttributeError, PluginManager
except ImportError:  # flat src/py-modules import
    from plugin_base import PluginBaseCapability, PluginBaseConfig, PluginBase
    from plugin_manager import PluginAttributeError, PluginManager

__all__ = [
    "PluginBaseCapability",
    "PluginBaseConfig",
    "PluginAttributeError",
    "PluginBase",
    "PluginManager",
]
