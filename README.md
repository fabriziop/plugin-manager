# Plugin Manager

A compact Python plugin manager for applications that load trusted plugins
either from a local application package or from installed Python entry points.
The application supplies plain configuration and context mappings, while
Plugin Manager validates plugin schemas, creates plugin instances, manages
plugin lifecycle, and registers plugin-requested tasks with an application-owned
task manager.

## Main responsibilities

Plugin Manager:

- validates configuration that can be checked without executing plugin code;
- loads enabled plugins from a local application package or installed Python entry points;
- builds each plugin's dataclass configuration;
- validates plugin version requirements through `PluginBaseConfig`;
- creates and starts plugin instances;
- registers non-direct execution tasks requested by plugins;
- exposes plugin attributes through `get_plugin_attribute()`;
- reports plugin state and registered task identifiers.

The application remains responsible for creating, configuring, starting,
stopping, resuming, removing, clearing, and inspecting the real task
scheduler.

## Package layout

```text
src/
  plugin_manager.py  Plugin loading, configuration, lifecycle, and diagnostics
  plugin_base.py     PluginBase, PluginBaseConfig, and optional capability base
  task_manager.py    Structural protocol required from an application adapter
examples/
  hello_world/       Minimal plugin example
  exec_modes/        Direct and scheduled modes with an in-process demo manager
  exec_modes_conductor/
                     Scheduled mode with system-installed Conductor
tests/
  test_plugin_manager.py
```

## Public API

The main exported classes are:

```python
from plugin_manager import (
    PluginAttributeError,
    PluginBase,
    PluginBaseCapability,
    PluginBaseConfig,
    PluginManager,
)
```

`PluginManager` accepts exactly two dictionaries:

```python
manager = PluginManager(main_config, context)
```

- `main_config` contains `PLUGIN_MANAGER` and `PLUGINS`.
- `context` contains runtime application objects and paths.

Both mappings are shallow-copied by the constructor.

## Main configuration

The top-level configuration must contain exactly these sections:

```python
main_config = {
    "PLUGIN_MANAGER": {...},
    "PLUGINS": {...},
}
```

### `PLUGIN_MANAGER`

The accepted fields are the fields of `PluginManagerConfig`:

```python
PLUGIN_MANAGER = {
    "fail_policy": "fail-fast",
    "plugin_dir": "plugins",
    "task_manager": task_manager_instance,
    "entry_point_group": "plugin_manager.plugins",
}
```

Defaults:

```text
fail_policy   "fail-fast"
plugin_dir    "plugins"
task_manager       None
entry_point_group    "plugin_manager.plugins"
```

`fail_policy` accepts:

- `"fail-fast"`
- `"continue-on-error"`

`task_manager` must be an already-created object compatible with the
`TaskManager` protocol. A class object is rejected. It may be `None` when the
configuration contains no scheduled tasks.

### `PLUGINS`

Each key is the logical plugin name. Every entry must contain an explicit
Boolean `enabled` field:

```python
PLUGINS = {
    "greeter": {
        "enabled": True,
        "greeting": "Ciao",
    },
}
```

Optional manager-level fields in a plugin entry include:

```text
source       Loading backend: "local" (default) or "entry-point"
module       Local Python module name; defaults to the plugin entry name
entry_point  Installed entry-point name; defaults to the plugin entry name
required     Whether startup failure is fatal; Boolean, defaults to True
requires     List of enabled plugin names that must start first
tasks        Mapping of task names to task specifications
```

`module` is valid only for `source="local"`. `entry_point` is valid only for
`source="entry-point"`. Local and entry-point plugins may be mixed in the same
`PLUGINS` mapping.

All other accepted values must correspond to fields in the plugin's config
dataclass. Unknown keys are rejected with `PluginConfigError`; they are not
silently ignored. For example, if a plugin declares a `timeout` field, a
misspelled `timeuot` entry causes loading to fail instead of falling back to
the dataclass default.

Plugin-specific key validation happens after that enabled plugin's module is
imported, because its `Config` dataclass defines the accepted schema. It still
happens before the config object or plugin instance is constructed. Disabled
plugins are never imported, so their plugin-specific fields are intentionally
not schema-checked. Manager-owned fields such as `required` are validated for
all entries before imports begin.

### Per-plugin startup failure policy

Plugins are **required by default**. This preserves fail-fast lifecycle behavior
for existing configurations:

```python
PLUGINS = {
    "database": {
        "enabled": True,
    },
}
```

If `database.start()` raises, startup is rolled back, the manager enters
`failed`, and `PluginLifecycleError` is raised.

A non-critical plugin can opt into isolated startup failure with
`required=False`:

```python
PLUGINS = {
    "database": {
        "enabled": True,
    },
    "metrics": {
        "enabled": True,
        "required": False,
    },
}
```

If `metrics.start()` raises, that plugin is marked `failed` with its error in
`diagnostics()`, but startup continues and the manager can still enter
`started`. Other plugins are not rolled back. Scheduled tasks belonging to the
failed optional plugin are not registered.

An optional plugin that failed startup is not automatically retried by a later
`stop()` / `start()` cycle; it remains `failed` for that manager instance. Its
`close()` hook is still called during final manager cleanup.

`required` controls **startup-hook failures only**. Import, discovery, and
configuration failures occur during `load()` and remain governed by the
manager-wide `PLUGIN_MANAGER.fail_policy`. Keeping these policies separate
allows applications to choose independently whether a broken plugin package
may be skipped at load time and whether an instantiated plugin is essential at
runtime.

The diagnostics entry exposes the effective policy:

```python
manager.diagnostics()["plugins"]["metrics"]
# {"state": "failed", "error": "start: ...", "required": False, ...}
```

### Plugin dependencies

A plugin may declare direct startup dependencies with the manager-owned
`requires` field:

```python
PLUGINS = {
    "database": {
        "enabled": True,
    },
    "analytics": {
        "enabled": True,
        "requires": ["database"],
    },
    "dashboard": {
        "enabled": True,
        "requires": ["analytics"],
    },
}
```

Dependencies must name other **enabled** plugins. Self-dependencies, duplicate
names, missing/disabled dependencies, and dependency cycles are rejected with
`PluginConfigError` during the pre-import validation phase. No plugin code is
executed to validate the dependency graph.

At startup, the graph is topologically sorted, so dependencies start before
the plugins that require them regardless of their order in the configuration.
Plugins that are otherwise unrelated keep a stable order based on the
`PLUGINS` mapping. `stop()` already uses reverse successful-start order, so
dependents are stopped before their dependencies.

Dependency availability also follows the per-plugin startup failure policy. If
an optional dependency (`required=False`) fails to start, a dependent plugin is
not started. An optional dependent is marked `failed` and startup continues; a
required dependent makes manager startup fail and triggers the normal rollback.
Scheduled tasks are registered only for plugins that actually reached
`started`.

`diagnostics()` exposes each plugin's declared dependencies as `requires`.

## Plugin discovery and loading backends

Plugin discovery is explicit and intentionally limited to two trusted-code
backends. The existing local-package behavior remains the default, so existing
configurations need no changes.

### Local backend

With no `source` field, or with `source="local"`, Plugin Manager loads a module
from `PLUGIN_MANAGER.plugin_dir` exactly as before:

```python
PLUGINS = {
    "greeter": {
        "enabled": True,
        # source defaults to "local"
        # module defaults to "greeter"
    },
}
```

The configured plugin directory must be an importable package containing
`__init__.py`, and the selected module must define `PLUGIN_CLASS`.

During a local-plugin import, Plugin Manager temporarily places the required
application import root at the front of `sys.path` so package and relative
imports continue to work. The original `sys.path` membership and ordering are
restored immediately after the import, including when the import fails; loading
a plugin therefore does not permanently change the host application's import
resolution order.

### Entry-point backend

Installed distributions can publish plugins through standard Python package
entry points. Select the backend per plugin:

```python
PLUGIN_MANAGER = {
    "entry_point_group": "plugin_manager.plugins",
}

PLUGINS = {
    "metrics": {
        "enabled": True,
        "source": "entry-point",
        "entry_point": "prometheus_metrics",
    },
}
```

The `entry_point` value defaults to the logical plugin name when omitted. The
entry point may resolve either directly to a `PluginBase` subclass or to a
module containing the conventional `PLUGIN_CLASS`. For example, an installed
distribution can publish a class in `pyproject.toml`:

```toml
[project.entry-points."plugin_manager.plugins"]
prometheus_metrics = "my_metrics.plugin:MetricsPlugin"
```

or publish a module:

```toml
[project.entry-points."plugin_manager.plugins"]
prometheus_metrics = "my_metrics.plugin"
```

Entry-point metadata is inspected during `validate()` without calling
`EntryPoint.load()`, so validation can confirm that every configured installed
plugin exists before any entry-point plugin code executes. Actual loading is
deferred to `load()`. An application using only entry-point plugins does not
need a local plugin directory.

This backend is discovery and loading, not sandboxing: installed plugins execute
in the application process with the application's Python permissions.

## Runtime context

A typical context is:

```python
from pathlib import Path

context = {
    "app_root": Path(__file__).resolve().parent,
    "app": application,
    "logger": logger,
    "app_config": application_config,
}
```

`app_root` defaults to the current working directory. The configured
`plugin_dir` is resolved relative to it.

Each plugin receives a `PluginContext` containing:

```text
plugin_name
manager
app
logger
app_config
queues
```

## Defining a plugin

A plugin module must define `PLUGIN_CLASS`. The class must inherit from
`PluginBase` and its constructor must accept `(config, context)`.

```python
from dataclasses import dataclass

from plugin_manager import PluginBase, PluginBaseConfig

PLUGIN_NAME = "greeter"
PLUGIN_VERSION = "1.0.0"
PLUGIN_DESCRIPTION = "Simple greeting plugin."
PLUGIN_AUTHOR = "Example Author"
PLUGIN_LICENSE = "GPL-3.0-or-later"
PLUGIN_API_VERSION = "1.0"


@dataclass(frozen=True)
class GreeterConfig(PluginBaseConfig):
    name: str = ""
    description: str = ""
    author: str | None = None
    license: str | None = None
    greeting: str = "Hello"


class GreeterPlugin(PluginBase):
    Config = GreeterConfig

    def greet(self, person: str) -> str:
        return f"{self.config.greeting}, {person}!"


PLUGIN_CLASS = GreeterPlugin
```

A plugin-specific `Config` class must:

- be a dataclass;
- inherit from `PluginBaseConfig`.

When no `Config` class is supplied, `PluginBaseConfig` is used.

## Prominent plugin constants

The following module constants are optional:

```text
PLUGIN_NAME
PLUGIN_VERSION
PLUGIN_DESCRIPTION
PLUGIN_AUTHOR
PLUGIN_LICENSE
PLUGIN_API_VERSION
```

Plugin Manager does not validate these constants directly. If the plugin
config dataclass defines the corresponding field, the constant is copied into
that field:

```text
PLUGIN_NAME         -> name
PLUGIN_VERSION      -> version
PLUGIN_DESCRIPTION  -> description
PLUGIN_AUTHOR       -> author
PLUGIN_LICENSE      -> license
PLUGIN_API_VERSION  -> api_version
```

Constants take precedence over values with the same field name in the
`PLUGINS` entry. Any validation belongs in the config dataclass, normally in
`__post_init__()`.

## Required and optional config fields

Dataclass fields without a default are required when the plugin config is
constructed. Fields with a default or `default_factory` are optional.

```python
@dataclass(frozen=True)
class ServiceConfig(PluginBaseConfig):
    endpoint: str
    timeout: float = 10.0
```

The following entry must define `endpoint`:

```python
"service": {
    "enabled": True,
    "endpoint": "https://example.invalid/api",
}
```

## Version requirements

`PluginBaseConfig` provides:

```text
version
req_version
api_version
req_api_version
```

When a requirement is not `None`, `PluginBaseConfig.__post_init__()` checks the
corresponding version with the `packaging` library and PEP 440 specifiers.

```python
PLUGINS = {
    "greeter": {
        "enabled": True,
        "req_version": ">=1.0,<2.0",
        "req_api_version": ">=1.0",
    },
}
```

A subclass that defines its own `__post_init__()` must call the base method:

```python
def __post_init__(self) -> None:
    super().__post_init__()
    # Additional plugin-specific checks follow.
```

## Validation phase

`PluginManager.validate()` performs a pre-import validation pass. It checks all
configuration that the manager can verify without importing or instantiating a
plugin, so structural configuration errors are rejected before any configured
plugin code can execute.

```python
manager = PluginManager(main_config, context)
manager.validate()
manager.load()
manager.start()
```

Calling `validate()` explicitly is optional. `load()` always calls it before
importing enabled plugins, and `start()` calls `load()` automatically when no
plugins have been loaded yet.

The pre-import phase validates:

- the top-level `PLUGIN_MANAGER` and `PLUGINS` configuration structure;
- every plugin entry, including the required Boolean `enabled` field;
- source-specific discovery settings for each enabled plugin;
- dependency declarations, enabled dependency targets, and cycle detection;
- local module names and module-file existence for local plugins;
- entry-point presence in the configured group for installed plugins, without loading them;
- task declaration structure, task names, execution values, and method names;
- whether a compatible task-manager instance is configured when scheduled
  tasks require one.

Disabled plugins are not imported or instantiated. Their entry structure is
still validated where possible, but local module-file existence and installed
entry-point presence are checked only for enabled plugins.

Plugin-specific dataclass validation cannot run during this phase because the
plugin's `Config` class is defined inside the plugin module. That validation
therefore occurs during `load()`, after the pre-import checks have succeeded.
This includes required plugin-specific dataclass fields, custom
`__post_init__()` checks, and version/API-version requirements.

A useful consequence is that configuration is checked as a set before imports
begin. For example, if the first enabled plugin is valid but a later enabled
plugin refers to a missing module file, `load()` raises before importing the
first plugin.

## Application usage

```python
from pathlib import Path

from plugin_manager import PluginManager

main_config = {
    "PLUGIN_MANAGER": {
        "plugin_dir": "plugins",
    },
    "PLUGINS": {
        "greeter": {
            "enabled": True,
            "greeting": "Ciao",
        },
    },
}

context = {
    "app_root": Path(__file__).resolve().parent,
}

manager = PluginManager(main_config, context)
manager.start()

try:
    greet = manager.get_plugin_attribute("greeter", "greet")
    print(greet("Fabrizio"))
finally:
    manager.close()
```

`start()` loads plugins automatically when they have not already been loaded.
The manager has an explicit reusable lifecycle:

```text
NEW -> LOADED -> STARTED -> STOPPED -> STARTED -> ...
                         \-> CLOSED
NEW/LOADED/STOPPED/FAILED -> CLOSED
```

The public manager states are `new`, `loaded`, `started`, `stopped`, `failed`,
and `closed`. `manager.state` exposes the corresponding `PluginManagerState`.

- `load()` performs validation and constructs enabled plugins. Repeating it
  while already loaded is a no-op; calling it after start/stop or close is an
  invalid transition.
- `start()` starts loaded plugins in dependency order. Calling it while already
  started is idempotent. Calling it after `stop()` restarts the same plugin
  instances in the same dependency-respecting order.
- `stop()` is reversible: it suspends manager-created tasks and calls plugin
  `stop()` hooks in reverse startup order, but does **not** call `close()`.
- `close()` is the final, idempotent cleanup operation. If necessary it first
  stops the manager, then calls every instantiated plugin's `close()` hook.
  `load()`, `start()`, and plugin access are rejected after close.
- A load failure governed by fail-fast policy, a required-plugin startup
  failure, or a stop failure places the manager in `failed`; final `close()` is
  still allowed, but restarting from a partially failed manager lifecycle is
  rejected. An optional (`required=False`) plugin startup failure is isolated
  to that plugin instead.

Plugin states likewise include `created`, `started`, `stopped`, `closed`, and
`failed`, making diagnostics reflect the lifecycle explicitly.

## Task-manager protocol

The application supplies an object structurally compatible with this protocol:

```python
class TaskManager(Protocol):
    def add_task(self, **task_spec: Any) -> str:
        ...

    def suspend(self, task_id: str, for_: int = 0) -> Any:
        ...
```

Inheritance from a Plugin Manager class is not required. A foreign class passes
the runtime protocol check when an instance exposes the required attributes.
The application adapter should treat suspension of an already inactive or
already suspended task as a successful no-op.

## Configuring plugin tasks

A task specification is placed under the plugin entry:

```python
PLUGINS = {
    "example_periodic": {
        "enabled": True,
        "message": "scheduled",
        "tasks": {
            "periodic": {
                "execution": "task",
                "method": "run_once",
                "period_us": 100_000,
                "start": "now",
                "count": 3,
                "pool_id": "fast",
                "overlap_policy": "serial",
            },
        },
    },
}
```

Task behavior:

- `execution` defaults to `"direct"`; direct entries are not registered.
- A non-direct task requires a configured task-manager instance.
- `method` names the plugin method passed as the scheduler task.
- If `method` is omitted, the task name is used, except `default`, which maps
  to `run_once`.
- `task_id` defaults to `<plugin_name>:<task_name>`.
- Remaining fields are forwarded unchanged to `add_task()`.
- Returned task IDs are stored for diagnostics and later suspension.

When `PluginManager.stop()` runs, it calls:

```python
manager.config.task_manager.suspend(task_id, for_=0)
```

for each task registered by that manager. Task IDs are registered only once.
If the stopped manager is started again and it owns scheduled tasks, the task
manager must also provide:

```python
def resume(self, task_id: str) -> Any:
    ...
```

`PluginManager.start()` then resumes the existing task IDs instead of adding
duplicates. The extra `resume()` capability is required only for restart; a
single start/stop/close lifecycle still needs only `add_task()` and
`suspend()`. Global scheduler start/stop/remove/clear operations remain the
application's responsibility.

## Diagnostics

```python
data = manager.diagnostics()
```

The result contains:

- the manager lifecycle `state`;
- each plugin's state, error, and config-derived metadata;
- the task IDs registered by this Plugin Manager instance.

Diagnostics do not query global scheduler state.

## Examples

Run from the project root.

Minimal plugin:

```bash
python examples/hello_world/hello_world_main.py
```

Direct and scheduled execution with the demo task manager:

```bash
python examples/exec_modes/exec_modes.py
```

Real system-level Conductor scheduler:

```bash
python examples/exec_modes_conductor/exec_modes_conductor.py
```

The Conductor example creates and owns the scheduler in application code. Its
adapter implements `add_task()` and an idempotent `suspend()` operation.
An adapter that wants to support `manager.stop(); manager.start()` with
scheduled tasks must additionally implement `resume(task_id)`.

## Testing

```bash
pytest -q
```

## Installation dependency

Version checks require:

```text
packaging>=23
```

The real Conductor example additionally requires a system installation of the
`conductor` Python package.

## License

This project is licensed under the GNU General Public License version 3 or
later. See [LICENSE](LICENSE).

## Author

Fabrizio Pollastri <mxgbot@gmail.com>

## Copyright

Copyright (C) 2026 Fabrizio Pollastri
