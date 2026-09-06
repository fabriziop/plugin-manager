"""Small plugin manager with lifecycle and optional task integration."""

from __future__ import annotations

import importlib
from importlib import metadata as importlib_metadata
import logging
import sys
import traceback
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any

try:  # package import
    from .plugin_base import PluginBaseCapability, PluginBaseConfig, PluginBase
    from .task_manager import TaskManager
except ImportError:  # flat src/py-modules import
    from plugin_base import PluginBaseCapability, PluginBaseConfig, PluginBase
    from task_manager import TaskManager

log = logging.getLogger("plugin_manager")


class PluginManagerError(Exception):
    """Base exception for plugin-manager failures."""


class PluginConfigError(PluginManagerError):
    """Report invalid plugin-manager or plugin configuration."""


class PluginLoadError(PluginManagerError):
    """Report failures while locating, importing, or creating a plugin."""


class PluginAttributeError(PluginManagerError):
    """Report access to an invalid or missing plugin attribute."""


class PluginLifecycleError(PluginManagerError):
    """Report failures during plugin startup or shutdown."""


class PluginState(str, Enum):
    """Describe the current lifecycle state of a plugin."""

    LOADED = "loaded"
    CREATED = "created"
    STARTED = "started"
    STOPPED = "stopped"
    CLOSED = "closed"
    FAILED = "failed"


class PluginManagerState(str, Enum):
    """Describe the manager lifecycle state."""

    NEW = "new"
    LOADED = "loaded"
    STARTED = "started"
    STOPPED = "stopped"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass(slots=True)
class Plugin:
    """Store one plugin's runtime objects, metadata, and state."""

    name: str
    description: str = ""
    version: str = ""
    author: str | None = None
    license: str | None = None
    api_version: str | None = None

    module: ModuleType | None = None
    cls: type[Any] | None = None
    instance: Any | None = None
    state: PluginState = PluginState.LOADED
    error: str | None = None
    source: str = "local"
    origin: str | None = None
    required: bool = True


@dataclass(frozen=True, slots=True)
class PluginManagerConfig:
    """Hold validated settings that control PluginManager behavior."""

    fail_policy: str = "fail-fast"
    plugin_dir: str = "plugins"
    task_manager: TaskManager | None = None
    entry_point_group: str = "plugin_manager.plugins"

    def __post_init__(self) -> None:
        """Validate values after dataclass field initialization."""

        if self.fail_policy not in {"fail-fast", "continue-on-error"}:
            raise PluginConfigError(
                "fail_policy must be 'fail-fast' or 'continue-on-error'")
        if not isinstance(self.entry_point_group, str) or not self.entry_point_group.strip():
            raise PluginConfigError("entry_point_group must be a non-empty string")


@dataclass(slots=True)
class PluginContext:
    """Runtime services passed to every plugin instance."""

    plugin_name: str
    manager: Any
    app: Any = None
    logger: Any = None
    app_config: Any = None
    queues: dict[str, Any] = field(default_factory=dict)


class PluginManager:
    """Load configured plugins and manage their lifecycle.

    Application access to plugin-provided services is intentionally minimal:
    call ``get_plugin_attribute(plugin_name, attribute_name)``.  The manager
    does not know or index capability interfaces.
    """

    def __init__(
        self,
        main_config: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        """Build a manager from application configuration and context.

        The application supplies plain dictionaries.  The manager copies
        them, validates its own section, and prepares empty runtime state.
        """

        # Validate the two public constructor arguments before using them.
        if not isinstance(main_config, dict):
            raise PluginConfigError("main configuration must be a dict")
        if not isinstance(context, dict):
            raise PluginConfigError("context must be a dict")

        # Keep shallow copies so later top-level caller changes are isolated.
        self.main_config = dict(main_config)
        self.context = dict(context)

        # Extract and validate the PluginManager-specific configuration.
        pmcfg = self.main_config.get("PLUGIN_MANAGER", {})
        # The manager section must remain a plain mapping.
        if not isinstance(pmcfg, dict):
            raise PluginConfigError("plugin manager config must be a dict")
        # Build the dataclass so omitted values use its declared defaults.
        try:
            self.config = PluginManagerConfig(**pmcfg)
        except TypeError as exc:
            raise PluginConfigError(f"invalid manager config: {exc}") from exc

        # Resolve application and plugin package paths once at startup.
        self.app_root = Path(self.context.get("app_root", Path.cwd())).resolve()
        self.plugin_package = (
            self.config.plugin_dir.replace("/", ".")
            .replace("\\", ".")
            .strip(".")
        )
        self.plugin_dir = (self.app_root / self.config.plugin_dir).resolve()

        # Allocate runtime containers used during loading and lifecycle work.
        self.plugins: dict[str, Plugin] = {}
        self.started_order: list[str] = []
        self.state = PluginManagerState.NEW
        self._task_ids: list[str] = []
        self._tasks_registered = False


    def validate(self) -> None:
        """Validate all configuration that can be checked without importing plugins.

        This phase is deliberately side-effect free with respect to plugin code:
        no configured plugin module is imported or instantiated.  Plugin-specific
        dataclass validation still happens during ``load()`` because the Config
        class is defined by the plugin module itself.
        """
        self._validate_top_level_config()
        entries = self._enabled_plugin_entries()
        self._validate_plugin_sources(entries)
        self._validate_task_specs(entries)
        self._validate_task_manager(entries)

    def load(self) -> None:
        """Validate, import, and construct enabled plugins without starting.

        ``load()`` is valid only before the manager has been started. Repeated
        calls while already loaded are harmless; loading after start/stop or
        after close is rejected so lifecycle transitions stay explicit.
        """
        if self.state == PluginManagerState.CLOSED:
            raise PluginLifecycleError("cannot load a closed plugin manager")
        if self.state == PluginManagerState.LOADED:
            return
        if self.state != PluginManagerState.NEW:
            raise PluginLifecycleError(
                f"cannot load plugin manager while state is {self.state.value!r}"
            )

        self.validate()
        entries = self._enabled_plugin_entries()
        log.info("loading %d enabled plugins", len(entries))

        for name, pgcfg in entries.items():
            try:
                self._load_one(name, pgcfg)
            except Exception as exc:
                self._plugin_failure(name, "load", exc)
                if self.config.fail_policy == "fail-fast":
                    self.state = PluginManagerState.FAILED
                    raise
        self.state = PluginManagerState.LOADED


    def start(self) -> None:
        """Start or restart plugins and activate their configured tasks.

        The first start registers scheduled tasks exactly once. A later start
        after ``stop()`` resumes those task IDs instead of registering
        duplicates. Calling ``start()`` while already started is idempotent.
        """
        if self.state == PluginManagerState.CLOSED:
            raise PluginLifecycleError("cannot start a closed plugin manager")
        if self.state == PluginManagerState.STARTED:
            return
        if self.state == PluginManagerState.NEW:
            self.load()
        if self.state not in {PluginManagerState.LOADED, PluginManagerState.STOPPED}:
            raise PluginLifecycleError(
                f"cannot start plugin manager while state is {self.state.value!r}"
            )

        restarting = self.state == PluginManagerState.STOPPED
        self._validate_task_manager()
        if restarting and self._task_ids:
            self._validate_task_resumer()

        self.started_order.clear()
        try:
            for name, plugin in self.plugins.items():
                if plugin.instance is None or plugin.state == PluginState.FAILED:
                    continue
                if plugin.state == PluginState.CLOSED:
                    raise PluginLifecycleError(f"plugin {name!r} is already closed")
                log.info("starting plugin %s", name)
                try:
                    plugin.instance.start()
                except Exception as exc:
                    self._plugin_failure(name, "start", exc)
                    if plugin.required:
                        raise
                    log.warning(
                        "optional plugin %s failed during startup; continuing", name
                    )
                    continue
                plugin.state = PluginState.STARTED
                plugin.error = None
                self.started_order.append(name)

            if not self._tasks_registered:
                self._register_plugin_tasks()
                self._tasks_registered = True
            elif restarting:
                for task_id in self._task_ids:
                    self.config.task_manager.resume(task_id)

            self.state = PluginManagerState.STARTED
        except Exception as exc:
            self._plugin_failure("<manager>", "start", exc)
            self._stop_started_plugins()
            self._suspend_plugin_tasks()
            self.state = PluginManagerState.FAILED
            raise PluginLifecycleError(f"startup failed: {exc}") from exc


    def stop(self) -> None:
        """Suspend manager-created tasks and stop plugins without closing them.

        ``stop()`` is reversible: a subsequent ``start()`` restarts plugin
        instances and resumes previously registered task IDs. It is idempotent
        when the manager is new, loaded, or already stopped.
        """
        if self.state == PluginManagerState.CLOSED:
            return
        if self.state != PluginManagerState.STARTED:
            return

        self._suspend_plugin_tasks()
        stopped_cleanly = self._stop_started_plugins()
        self.state = (
            PluginManagerState.STOPPED
            if stopped_cleanly
            else PluginManagerState.FAILED
        )


    def close(self) -> None:
        """Permanently release plugin resources and close the manager.

        ``close()`` is final and idempotent. If the manager is running it first
        performs the reversible ``stop()`` transition, then calls ``close()``
        on every instantiated plugin in reverse load order.
        """
        if self.state == PluginManagerState.CLOSED:
            return
        if self.state == PluginManagerState.STARTED:
            self.stop()

        for plugin in reversed(list(self.plugins.values())):
            if plugin.instance is None or plugin.state == PluginState.CLOSED:
                continue
            try:
                log.info("closing plugin %s", plugin.name)
                plugin.instance.close()
                if plugin.state != PluginState.FAILED:
                    plugin.state = PluginState.CLOSED
            except Exception as exc:
                plugin.state = PluginState.FAILED
                plugin.error = f"close: {type(exc).__name__}: {exc}"
                log.exception("failed closing plugin %s", plugin.name)

        self.started_order.clear()
        self.state = PluginManagerState.CLOSED


    def _suspend_plugin_tasks(self) -> None:
        """Suspend all task IDs registered by this manager."""
        if not self._task_ids or self.config.task_manager is None:
            return
        for task_id in self._task_ids:
            try:
                self.config.task_manager.suspend(task_id, for_=0)
            except Exception:
                log.exception("failed suspending task %s", task_id)


    def _stop_started_plugins(self) -> bool:
        """Stop plugins in reverse successful-start order. Return success."""
        stopped_cleanly = True
        for name in reversed(self.started_order):
            plugin = self.plugins.get(name)
            if plugin is None or plugin.instance is None or plugin.state != PluginState.STARTED:
                continue
            try:
                log.info("stopping plugin %s", name)
                plugin.instance.stop()
                plugin.state = PluginState.STOPPED
            except Exception as exc:
                stopped_cleanly = False
                plugin.state = PluginState.FAILED
                plugin.error = f"stop: {type(exc).__name__}: {exc}"
                log.exception("failed stopping plugin %s", name)
        self.started_order.clear()
        return stopped_cleanly


    def _register_plugin_tasks(self) -> None:
        """Register configured non-direct tasks and remember only their IDs."""
        # Translate each plugin task declaration into a task-manager request.
        for plugin_name, entry in self._enabled_plugin_entries().items():
            plugin = self.plugins.get(plugin_name)
            if plugin is None or plugin.state != PluginState.STARTED:
                continue
            tasks = entry.get("tasks", {})
            if not isinstance(tasks, dict):
                raise PluginConfigError(f"plugin {plugin_name}: tasks must be a dict")
            for task_name, spec in tasks.items():
                if not isinstance(task_name, str) or not task_name.isidentifier():
                    raise PluginConfigError(
                        f"plugin {plugin_name}: task name must be a valid Python identifier"
                    )
                if not isinstance(spec, dict):
                    raise PluginConfigError(f"plugin {plugin_name}: task {task_name}: spec must be a dict")
                if spec.get("execution", "direct") == "direct":
                    continue
                method_name = spec.get("method") or ("run_once" if task_name == "default" else task_name)
                if not isinstance(method_name, str) or not method_name.isidentifier():
                    raise PluginConfigError(
                        f"plugin {plugin_name}: task {task_name}: method must be a valid Python identifier"
                    )
                requested_id = spec.get("task_id") or f"{plugin_name}:{task_name}"
                task_spec = {k: v for k, v in spec.items() if k not in {"execution", "method"}}
                task_spec["task"] = self.get_plugin_attribute(plugin_name, method_name)
                task_spec["task_id"] = requested_id
                task_id = self.config.task_manager.add_task(**task_spec)
                self._task_ids.append(str(task_id))

    def _validate_plugin_sources(self, entries: dict[str, dict[str, Any]]) -> None:
        """Validate enabled plugin sources without executing plugin code."""
        entry_points: dict[str, Any] | None = None
        has_local = any(entry.get("source", "local") == "local" for entry in entries.values())
        if has_local:
            if not self.plugin_dir.is_dir():
                raise PluginConfigError(
                    f"plugin directory does not exist: {self.plugin_dir}"
                )
            if not (self.plugin_dir / "__init__.py").is_file():
                raise PluginConfigError(
                    "plugin directory must be an importable package with "
                    f"__init__.py: {self.plugin_dir}"
                )

        for name, entry in entries.items():
            source = entry.get("source", "local")
            if source == "local":
                module_name = str(entry.get("module", name))
                path = self.plugin_dir / f"{module_name}.py"
                if not path.is_file():
                    raise PluginLoadError(f"plugin file not found: {path}")
                continue

            if entry_points is None:
                entry_points = self._entry_points_by_name()
            entry_point_name = str(entry.get("entry_point", name))
            if entry_point_name not in entry_points:
                raise PluginLoadError(
                    f"plugin {name}: entry point {entry_point_name!r} not found "
                    f"in group {self.config.entry_point_group!r}"
                )

    def _validate_task_specs(self, entries: dict[str, dict[str, Any]]) -> None:
        """Validate task declaration structure without resolving plugin methods."""
        for plugin_name, entry in entries.items():
            tasks = entry.get("tasks", {})
            if not isinstance(tasks, dict):
                raise PluginConfigError(f"plugin {plugin_name}: tasks must be a dict")
            for task_name, spec in tasks.items():
                if not isinstance(task_name, str) or not task_name.isidentifier():
                    raise PluginConfigError(
                        f"plugin {plugin_name}: task name must be a valid Python identifier"
                    )
                if not isinstance(spec, dict):
                    raise PluginConfigError(
                        f"plugin {plugin_name}: task {task_name}: spec must be a dict"
                    )
                execution = spec.get("execution", "direct")
                if not isinstance(execution, str):
                    raise PluginConfigError(
                        f"plugin {plugin_name}: task {task_name}: execution must be a string"
                    )
                method_name = spec.get("method") or (
                    "run_once" if task_name == "default" else task_name
                )
                if not isinstance(method_name, str) or not method_name.isidentifier():
                    raise PluginConfigError(
                        f"plugin {plugin_name}: task {task_name}: method must be a valid Python identifier"
                    )

    def _validate_task_manager(
        self, entries: dict[str, dict[str, Any]] | None = None
    ) -> None:
        """Validate optional task manager and enforce it when scheduled tasks exist."""
        task_manager = self.config.task_manager
        if task_manager is not None and (
            isinstance(task_manager, type)
            or not isinstance(task_manager, TaskManager)
        ):
            raise PluginConfigError(
                "PLUGIN_MANAGER.task_manager must be a TaskManager instance"
            )

        if task_manager is not None:
            return

        if entries is None:
            entries = self._enabled_plugin_entries()

        for plugin_name, entry in entries.items():
            tasks = entry.get("tasks", {})
            if not isinstance(tasks, dict):
                continue
            for task_name, spec in tasks.items():
                if not isinstance(spec, dict):
                    continue
                if spec.get("execution", "direct") != "direct":
                    raise PluginConfigError(
                        "PLUGIN_MANAGER.task_manager is required when "
                        f"plugin {plugin_name} task {task_name} uses "
                        "execution != 'direct'"
                    )

    def _validate_task_resumer(self) -> None:
        """Require task resume support only when a stopped manager restarts."""
        task_manager = self.config.task_manager
        resume = getattr(task_manager, "resume", None) if task_manager is not None else None
        if not callable(resume):
            raise PluginLifecycleError(
                "PLUGIN_MANAGER.task_manager must provide resume(task_id) "
                "to restart a stopped manager with scheduled tasks"
            )


    def get_plugin_attribute(self, plugin_name: str, attribute_name: str) -> Any:
        """Return one attribute from one loaded plugin instance.

        Raises ``PluginLoadError`` if the plugin is unknown/not instantiated and
        ``PluginAttributeError`` if the attribute name is invalid or missing.
        """
        if self.state == PluginManagerState.CLOSED:
            raise PluginLifecycleError("cannot access plugins after manager.close()")
        plugin = self.plugins.get(plugin_name)
        if plugin is None or plugin.instance is None:
            raise PluginLoadError(f"plugin {plugin_name!r} is not loaded")
        try:
            if attribute_name:
                if not isinstance(attribute_name, str) or not attribute_name.isidentifier():
                    raise PluginAttributeError(f"invalid plugin attribute name: {attribute_name!r}")
                return getattr(plugin.instance, attribute_name)
            else:
                return plugin.instance
        except AttributeError as exc:
            raise PluginAttributeError(
                f"plugin {plugin_name!r} has no attribute {attribute_name!r}"
            ) from exc


    def diagnostics(self) -> dict[str, Any]:
        """Return serializable plugin states and registered task IDs."""
        return {
            "state": self.state.value,
            "plugins": {
                name: {
                    "state": plugin.state.value,
                    "error": plugin.error,
                    "required": plugin.required,
                    "metadata": {
                        "name": plugin.name,
                        "description": plugin.description,
                        "author": plugin.author,
                        "license": plugin.license,
                        "version": plugin.version,
                        "api_version": plugin.api_version,
                        "source": plugin.source,
                        "origin": plugin.origin,
                    },
                }
                for name, plugin in self.plugins.items()
            },
            "tasks": list(self._task_ids),
        }


    #### private methods

    def _validate_top_level_config(self) -> None:
        """Validate top-level sections and the configured plugin package."""

        if not isinstance(self.main_config, dict):
            raise PluginConfigError("configuration must be a dict")
        unknown = set(self.main_config) - {"PLUGIN_MANAGER", "PLUGINS"}
        if unknown:
            raise PluginConfigError(f"unknown top-level config keys: {sorted(unknown)}")
        if "PLUGIN_MANAGER" not in self.main_config or not isinstance(self.main_config["PLUGIN_MANAGER"], dict):
            raise PluginConfigError("configuration must contain dict 'PLUGIN_MANAGER'")
        if "PLUGINS" not in self.main_config or not isinstance(self.main_config["PLUGINS"], dict):
            raise PluginConfigError("configuration must contain dict 'PLUGINS'")


    def _enabled_plugin_entries(self) -> dict[str, dict[str, Any]]:
        """Return validated configuration entries for enabled plugins."""
        out: dict[str, dict[str, Any]] = {}
        for name, entry in self.main_config["PLUGINS"].items():
            if not isinstance(name, str) or not name.isidentifier():
                raise PluginConfigError(f"plugin name must be a valid Python identifier: {name!r}")
            if not isinstance(entry, dict):
                raise PluginConfigError(f"plugin {name}: entry must be a dict")
            if "enabled" not in entry:
                raise PluginConfigError(f"plugin {name}: missing mandatory 'enabled'")
            if not isinstance(entry["enabled"], bool):
                raise PluginConfigError(f"plugin {name}: 'enabled' must be bool")
            if "required" in entry and not isinstance(entry["required"], bool):
                raise PluginConfigError(f"plugin {name}: 'required' must be bool")
            source = entry.get("source", "local")
            if source not in {"local", "entry-point"}:
                raise PluginConfigError(
                    f"plugin {name}: source must be 'local' or 'entry-point'"
                )

            if source == "local":
                module_name = entry.get("module", name)
                if not isinstance(module_name, str) or not module_name.isidentifier():
                    raise PluginConfigError(
                        f"plugin {name}: module must be a valid Python identifier"
                    )
                if "entry_point" in entry:
                    raise PluginConfigError(
                        f"plugin {name}: entry_point is only valid for source='entry-point'"
                    )
            else:
                entry_point_name = entry.get("entry_point", name)
                if not isinstance(entry_point_name, str) or not entry_point_name.strip():
                    raise PluginConfigError(
                        f"plugin {name}: entry_point must be a non-empty string"
                    )
                if "module" in entry:
                    raise PluginConfigError(
                        f"plugin {name}: module is only valid for source='local'"
                    )

            if entry["enabled"]:
                out[name] = entry
        return out


    def _load_one(self, name: str, pgcfg: dict[str, Any]) -> None:
        """Import, configure, instantiate, and record one plugin."""

        # Load the plugin through the configured source backend. Existing
        # configurations default to the local-package backend.
        pg_module, pg_main_class, source, origin = self._load_plugin_target(name, pgcfg)

        # Merge application values and prominent module constants.
        config_obj = self._build_plugin_config(name, pg_main_class, pg_module, pgcfg)
        metadata = self._metadata_from_config(name, config_obj)

        # Build the runtime context passed to the plugin constructor.
        context = PluginContext(
            plugin_name=name,
            manager=self,
            app=self.context.get("app"),
            logger=self.context.get("logger"),
            app_config=self.context.get("app_config"),
        )
        instance = self._instantiate_plugin(name, pg_main_class, config_obj, context)
        self.plugins[name] = Plugin(
            module=pg_module,
            cls=pg_main_class,
            instance=instance,
            state=PluginState.CREATED,
            source=source,
            origin=origin,
            required=bool(pgcfg.get("required", True)),
            **metadata,
        )
        log.debug("plugin %s merged config: %s", name, self._safe_config_dict(config_obj))

    def _load_plugin_target(
        self, name: str, pgcfg: dict[str, Any]
    ) -> tuple[ModuleType, type[Any], str, str]:
        """Return a plugin module and class from the selected loading backend."""
        source = str(pgcfg.get("source", "local"))
        if source == "local":
            module_name = str(pgcfg.get("module", name))
            module = self._import_plugin_module(module_name)
            cls = self._plugin_class_from_module(name, module)
            return module, cls, source, module_name

        entry_point_name = str(pgcfg.get("entry_point", name))
        entry_point = self._entry_points_by_name().get(entry_point_name)
        if entry_point is None:
            raise PluginLoadError(
                f"plugin {name}: entry point {entry_point_name!r} not found "
                f"in group {self.config.entry_point_group!r}"
            )
        try:
            loaded = entry_point.load()
        except Exception as exc:
            raise PluginLoadError(
                f"plugin {name}: entry point {entry_point_name!r} failed to load: {exc}"
            ) from exc

        if isinstance(loaded, ModuleType):
            module = loaded
            cls = self._plugin_class_from_module(name, module)
        elif isinstance(loaded, type):
            cls = loaded
            module = sys.modules.get(cls.__module__)
            if module is None:
                try:
                    module = importlib.import_module(cls.__module__)
                except Exception as exc:
                    raise PluginLoadError(
                        f"plugin {name}: could not resolve module for entry-point class: {exc}"
                    ) from exc
            self._validate_plugin_class(name, cls, "entry point")
        else:
            raise PluginLoadError(
                f"plugin {name}: entry point must resolve to a PluginBase subclass "
                "or a module containing PLUGIN_CLASS"
            )
        return module, cls, source, entry_point_name

    def _entry_points_by_name(self) -> dict[str, Any]:
        """Return installed entry points for the configured group without loading them."""
        discovered = importlib_metadata.entry_points()
        if hasattr(discovered, "select"):
            selected = discovered.select(group=self.config.entry_point_group)
        else:  # Python/importlib.metadata compatibility path
            selected = discovered.get(self.config.entry_point_group, ())
        return {entry_point.name: entry_point for entry_point in selected}

    def _plugin_class_from_module(self, name: str, module: ModuleType) -> type[Any]:
        """Resolve the conventional PLUGIN_CLASS object from a plugin module."""
        cls = getattr(module, "PLUGIN_CLASS", None)
        if cls is None:
            raise PluginLoadError(f"plugin {name}: missing PLUGIN_CLASS")
        self._validate_plugin_class(name, cls, "PLUGIN_CLASS")
        return cls

    @staticmethod
    def _validate_plugin_class(name: str, cls: Any, label: str) -> None:
        """Validate a class supplied by either plugin loading backend."""
        if not isinstance(cls, type):
            raise PluginLoadError(f"plugin {name}: {label} must be a class")
        if not issubclass(cls, PluginBase):
            raise PluginLoadError(
                f"plugin {name}: {label} must inherit from PluginBase"
            )

    def _instantiate_plugin(
        self,
        name: str,
        cls: type[Any],
        config_obj: Any,
        context: PluginContext,
    ) -> Any:
        """Instantiate a plugin with its validated config and context."""
        try:
            return cls(config_obj, context)
        except TypeError as exc:
            raise PluginLoadError(
                f"plugin {name}: PLUGIN_CLASS constructor must accept (config, context)"
            ) from exc

    def _import_plugin_module(self, module_name: str) -> ModuleType:
        """Import one plugin module from the configured plugin package."""
        path = self.plugin_dir / f"{module_name}.py"
        if not path.is_file():
            raise PluginLoadError(f"plugin file not found: {path}")

        if self.plugin_package:
            qualified_name = f"{self.plugin_package}.{module_name}"
            import_root = self.app_root
        else:
            qualified_name = module_name
            import_root = self.plugin_dir

        # Temporarily put the application import root first.  Local plugins may
        # need this for package/relative imports, but loading a plugin must not
        # permanently change the host application's import resolution order.
        root = str(import_root)
        original_sys_path = list(sys.path)
        sys.path[:] = [item for item in sys.path if item != root]
        sys.path.insert(0, root)

        try:
            # Reuse only a cached module that resolves to the expected file.
            existing = sys.modules.get(qualified_name)
            if existing is not None:
                existing_file = getattr(existing, "__file__", None)
                if existing_file is not None and Path(existing_file).resolve() == path.resolve():
                    return existing
                sys.modules.pop(qualified_name, None)

            if self.plugin_package:
                package = sys.modules.get(self.plugin_package)
                if package is not None:
                    package_paths = [Path(p).resolve() for p in getattr(package, "__path__", [])]
                    if self.plugin_dir not in package_paths:
                        sys.modules.pop(self.plugin_package, None)

            # Import after invalidating caches so newly written plugins are seen.
            importlib.invalidate_caches()
            try:
                return importlib.import_module(qualified_name)
            except Exception as exc:
                raise PluginLoadError(
                    f"plugin module {module_name}: import failed: {exc}"
                ) from exc
        finally:
            # Restore both membership and ordering exactly, including failure and
            # cached-module return paths.
            sys.path[:] = original_sys_path

    @staticmethod
    def _module_config_values(module: ModuleType, config_cls: type[Any]) -> dict[str, Any]:
        """Return prominent module constants mapped to matching config fields.

        PluginManager deliberately does not validate these values.  They are
        construction inputs for the plugin config dataclass, whose
        ``__post_init__`` owns any semantic validation.
        """
        field_names = {item.name for item in fields(config_cls)}
        mapping = {
            "PLUGIN_NAME": "name",
            "PLUGIN_VERSION": "version",
            "PLUGIN_DESCRIPTION": "description",
            "PLUGIN_AUTHOR": "author",
            "PLUGIN_LICENSE": "license",
            "PLUGIN_API_VERSION": "api_version",
        }
        values: dict[str, Any] = {}
        for constant_name, field_name in mapping.items():
            if field_name in field_names and hasattr(module, constant_name):
                values[field_name] = getattr(module, constant_name)
        return values

    @staticmethod
    def _metadata_from_config(name: str, config_obj: Any) -> dict[str, Any]:
        """Build diagnostic metadata from the already-created config instance."""
        return {
            "name": getattr(config_obj, "name", name),
            "description": getattr(config_obj, "description", ""),
            "version": getattr(config_obj, "version", ""),
            "author": getattr(config_obj, "author", None),
            "license": getattr(config_obj, "license", None),
            "api_version": getattr(config_obj, "api_version", None),
        }


    def _build_plugin_config(
        self,
        name: str,
        cls: type[Any],
        module: ModuleType,
        pgcfgin: dict[str, Any],
    ) -> Any:
        """Build and validate the plugin-specific config dataclass."""
        # Use the plugin config class, falling back to the common base.
        pgcfg_class = getattr(cls, "Config", None) or PluginBaseConfig

        if not is_dataclass(pgcfg_class):
            raise PluginConfigError(f"plugin {name}: Config must be a dataclass")

        if not issubclass(pgcfg_class, PluginBaseConfig):
            raise PluginConfigError(f"plugin {name}: Config must inherit from PluginBaseConfig")

        # Reject misspelled or unsupported plugin configuration instead of
        # silently discarding it. ``tasks`` belongs to PluginManager rather
        # than to the plugin Config dataclass, so it is the sole entry-level
        # key accepted in addition to declared Config fields.
        allowed = {f.name for f in fields(pgcfg_class)}
        manager_keys = {"tasks", "source", "entry_point", "required"}
        unknown = set(pgcfgin) - allowed - manager_keys
        if unknown:
            raise PluginConfigError(
                f"plugin {name}: unknown config keys: {sorted(unknown)}"
            )

        # Copy declared dataclass fields from the application config.
        values = {k: v for k, v in pgcfgin.items() if k in allowed}
        # Prominent PLUGIN_* constants define the corresponding metadata
        # fields and therefore take precedence over input dictionary values.
        values.update(self._module_config_values(module, pgcfg_class))

        # Detect missing fields before construction for a clearer error.
        required = {
            item.name
            for item in fields(pgcfg_class)
            if item.default is MISSING
            and item.default_factory is MISSING
        }
        missing = required - set(values)
        if missing:
            raise PluginConfigError(f"plugin {name}: missing required config fields: {sorted(missing)}")

        try:
            # Preserve an explicit module value when constants did not set it.
            if "module" not in values and "module" in pgcfgin:
                values["module"] = entry["module"]
            pgcfg = pgcfg_class(**values)
        except TypeError as exc:
            raise PluginConfigError(f"plugin {name}: invalid config: {exc}") from exc
        return pgcfg

    @staticmethod
    def _safe_config_dict(config_obj: Any) -> dict[str, Any] | None:
        """Convert a dataclass config to a safe diagnostic dictionary."""
        if config_obj is None:
            return None
        if is_dataclass(config_obj):
            return asdict(config_obj)
        return None

    def _plugin_failure(
        self,
        name: str,
        phase: str,
        exc: Exception,
    ) -> None:
        """Record a plugin failure and emit concise diagnostic logging."""
        plugin = self.plugins.get(name)
        if plugin is None:
            configured = self.main_config.get("PLUGINS", {}).get(name, {})
            required = (
                configured.get("required", True)
                if isinstance(configured, dict)
                else True
            )
            plugin = Plugin(
                name=name,
                state=PluginState.FAILED,
                required=required if isinstance(required, bool) else True,
            )
            self.plugins[name] = plugin
        plugin.state = PluginState.FAILED
        plugin.error = f"{phase}: {type(exc).__name__}: {exc}"
        log.error("plugin %s failed during %s: %s", name, phase, exc)
        log.debug("plugin failure traceback:\n%s", traceback.format_exc())

####  END
