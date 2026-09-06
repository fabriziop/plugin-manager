# Plugin Manager

A compact Python plugin manager for applications that load plugins from a
local package. The application supplies plain configuration and context
mappings, while Plugin Manager validates plugin schemas, creates plugin
instances, manages plugin lifecycle, and registers plugin-requested tasks with
an application-owned task manager.

## Main responsibilities

Plugin Manager:

- validates configuration that can be checked without executing plugin code;
- loads enabled plugin modules from an application package;
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
}
```

Defaults:

```text
fail_policy   "fail-fast"
plugin_dir    "plugins"
task_manager  None
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
module  Python module name; defaults to the plugin entry name
tasks   Mapping of task names to task specifications
```

All other accepted values must correspond to fields in the plugin's config
dataclass. Unknown keys are rejected with `PluginConfigError`; they are not
silently ignored. For example, if a plugin declares a `timeout` field, a
misspelled `timeuot` entry causes loading to fail instead of falling back to
the dataclass default.

Plugin-specific key validation happens after that enabled plugin's module is
imported, because its `Config` dataclass defines the accepted schema. It still
happens before the config object or plugin instance is constructed. Disabled
plugins are never imported, so their plugin-specific fields are intentionally
not schema-checked.

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
- module names and the existence of module files for enabled plugins;
- task declaration structure, task names, execution values, and method names;
- whether a compatible task-manager instance is configured when scheduled
  tasks require one.

Disabled plugins are not imported or instantiated. Their entry structure is
still validated where possible, but module-file existence is checked only for
enabled plugins.

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
    manager.stop()
```

`start()` loads plugins automatically when they have not already been loaded.
`stop()` is idempotent, stops plugins in reverse startup order, and closes all
created plugin instances.

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

for each task registered by that manager. Plugin Manager does not resume,
remove, clear, start, or stop scheduler tasks. Those operations remain the
application's responsibility.

## Diagnostics

```python
data = manager.diagnostics()
```

The result contains:

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
