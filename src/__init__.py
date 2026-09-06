"""Public API for the plugin_manager package."""

try:  # package import
    from .plugin_base import PluginBaseCapability, PluginBaseConfig, PluginBase
    from .plugin_manager import (
        PluginAttributeError,
        PluginLifecycleError,
        PluginManager,
        PluginManagerState,
        PluginState,
    )
except ImportError:  # flat src/py-modules import
    from plugin_base import PluginBaseCapability, PluginBaseConfig, PluginBase
    from plugin_manager import (
        PluginAttributeError,
        PluginLifecycleError,
        PluginManager,
        PluginManagerState,
        PluginState,
    )

__all__ = [
    "PluginBaseCapability",
    "PluginBaseConfig",
    "PluginAttributeError",
    "PluginLifecycleError",
    "PluginBase",
    "PluginManager",
    "PluginManagerState",
    "PluginState",
]
