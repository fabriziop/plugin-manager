"""Regression tests for PluginManager loading and lifecycle behavior."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from plugin_manager import (
    PluginAttributeError,
    PluginCapabilityError,
    PluginConfigError,
    PluginLifecycleError,
    PluginLoadError,
    PluginManager,
    PluginManagerState,
    PluginState,
    PluginBase,
)




class FakeEntryPoint:
    """Minimal importlib.metadata EntryPoint stand-in."""

    def __init__(self, name: str, value: object) -> None:
        self.name = name
        self.value = value
        self.load_calls = 0

    def load(self) -> object:
        self.load_calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeEntryPoints(list[FakeEntryPoint]):
    """Support the modern EntryPoints.select() API."""

    def __init__(self, group: str, values: list[FakeEntryPoint]) -> None:
        super().__init__(values)
        self.group = group

    def select(self, **params: object) -> "FakeEntryPoints":
        if params.get("group") == self.group:
            return self
        return FakeEntryPoints(self.group, [])


class FakeScheduler:
    """Provide a deterministic scheduler stand-in for integration tests."""

    def __init__(self, default_pool_workers: int = 1) -> None:
        self.default_pool_workers = default_pool_workers
        self.pools: dict[str, int] = {"default": default_pool_workers}
        self.tasks: list[dict[str, object]] = []
        self.started = False
        self.stopped = False
        self.cleared = False

    def create_pool(self, pool_id: str, workers: int = 1) -> None:
        self.pools[pool_id] = workers

    def add_task(self, **kwargs: object) -> str:
        if self.started:
            raise RuntimeError("cannot add tasks while scheduler is running")
        self.tasks.append(kwargs)
        return str(kwargs.get("task_id"))

    def start_engine(self) -> None:
        if not self.tasks:
            raise RuntimeError("no tasks scheduled")
        self.started = True

    def stop(self) -> None:
        self.stopped = True
        self.started = False

    def read_stats(self) -> dict[str, object]:
        return {"running": self.started, "task_count": len(self.tasks)}

    def read_jobs(self) -> list[dict[str, object]]:
        return [{"id": t.get("task_id"), "task_error": ""} for t in self.tasks]

    def read_pool_stats(self, pool_id: str | None = None) -> object:
        return self.pools if pool_id is None else {pool_id: self.pools[pool_id]}


class NoOpTaskManager:
    """Record task registrations without running a scheduler."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, object]] = {}
        self.suspended: list[str] = []
        self.resumed: list[str] = []

    def add_task(self, **task_spec: object) -> str:
        task_id = str(task_spec["task_id"])
        self.tasks[task_id] = dict(task_spec)
        return task_id

    def suspend(self, task_id: str, for_: int = 0) -> str:
        self.suspended.append(task_id)
        return "forever" if for_ == 0 else str(for_)

    def resume(self, task_id: str) -> str:
        self.resumed.append(task_id)
        return task_id


class FakeTaskManager(NoOpTaskManager):
    """Adapt FakeScheduler to the task-manager protocol used by PM."""

    def __init__(self) -> None:
        super().__init__()
        self.scheduler = FakeScheduler(default_pool_workers=1)

    def add_task(self, **task_spec: object) -> str:
        task_id = self.scheduler.add_task(**task_spec)
        self.tasks[task_id] = dict(task_spec)
        return task_id


class TempProject(unittest.TestCase):
    """Create isolated plugin packages for each PluginManager test."""

    def setUp(self) -> None:
        """Create a temporary importable plugin package."""
        self.tmp = Path(tempfile.mkdtemp())
        self.plugins = self.tmp / "plugins"
        self.plugins.mkdir()
        (self.plugins / "__init__.py").write_text('"""Test plugin package."""\n', encoding="utf-8")
        sys.path.insert(0, str(self.tmp))

    def tearDown(self) -> None:
        """Remove temporary paths, modules, and files after each test."""
        try:
            sys.path.remove(str(self.tmp))
        except ValueError:
            pass
        shutil.rmtree(self.tmp)

    def write_plugin(self, name: str, source: str) -> None:
        """Write one plugin module into the temporary package."""
        body = dedent(source)
        if "PLUGIN_NAME" not in body:
            body = (
                f'PLUGIN_NAME = "{name}"\n'
                'PLUGIN_VERSION = "1.0.0"\n'
                'PLUGIN_DESCRIPTION = "Test plugin."\n'
                'PLUGIN_API_VERSION = "1.0"\n'
                + body
            )
        (self.plugins / f"{name}.py").write_text(body, encoding="utf-8")

    def config(self, plugins: dict[str, object], manager: dict[str, object] | None = None) -> dict[str, object]:
        manager_config: dict[str, object] = {
            "plugin_dir": "plugins",
            "task_manager": NoOpTaskManager(),
        }
        manager_config.update(manager or {})
        return {
            "PLUGIN_MANAGER": manager_config,
            "PLUGINS": plugins,
        }

    def context(self) -> dict[str, object]:
        """Return the minimal runtime context used by test managers."""
        return {"app_root": self.tmp}

    def test_plugin_attribute_and_config_merge(self) -> None:
        self.write_plugin(
            "p1",
            '''
            from dataclasses import dataclass
            from plugin_manager import PluginBaseConfig, PluginBase
            @dataclass(frozen=True)
            class Cfg(PluginBaseConfig):
                greeting: str = "Hello"
            class P(PluginBase):
                Config = Cfg
                VERSION = "1"
                DESCRIPTION = "desc"
                def greet(self, name: str) -> str:
                    return f"{self.config.greeting}, {name}"
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"p1": {"enabled": True, "greeting": "Hi"}}),
            self.context(),
        )
        manager.start()
        try:
            greet = manager.get_plugin_attribute("p1", "greet")
            self.assertEqual(greet("Ada"), "Hi, Ada")
        finally:
            manager.stop()

    def test_missing_attribute_raises(self) -> None:
        self.write_plugin(
            "p1",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(self.config({"p1": {"enabled": True}}), self.context())
        manager.load()
        with self.assertRaises(PluginAttributeError):
            manager.get_plugin_attribute("p1", "missing")
        with self.assertRaises(PluginLoadError):
            manager.get_plugin_attribute("unknown", "x")

    def test_capability_registry_resolves_bound_method(self) -> None:
        self.write_plugin(
            "mailer",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                CAPABILITIES = {"mail.send": "send"}
                def send(self, recipient: str) -> str:
                    return f"sent:{recipient}"
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"mailer": {"enabled": True}}),
            self.context(),
        )
        manager.load()

        send = manager.get_capability("mail.send")

        self.assertEqual(send("ada@example.test"), "sent:ada@example.test")
        self.assertEqual(manager.diagnostics()["capabilities"], {"mail.send": "mailer"})
        self.assertEqual(
            manager.diagnostics()["plugins"]["mailer"]["capabilities"],
            ["mail.send"],
        )

    def test_capability_registry_can_return_service_object(self) -> None:
        self.write_plugin(
            "storage",
            '''
            from plugin_manager import PluginBase, PluginBaseCapability
            class Storage(PluginBaseCapability):
                CAPABILITY_ID = "storage.object"
                def get(self, key: str) -> str:
                    return f"value:{key}"
            class P(PluginBase):
                CAPABILITIES = {"storage.object": "storage"}
                def __init__(self, config, context):
                    super().__init__(config, context)
                    self.storage = Storage()
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"storage": {"enabled": True}}),
            self.context(),
        )
        manager.load()

        storage = manager.get_capability("storage.object")

        self.assertEqual(storage.get("answer"), "value:answer")

    def test_unknown_or_invalid_capability_lookup_raises(self) -> None:
        manager = PluginManager(self.config({}), self.context())
        manager.load()

        with self.assertRaises(PluginCapabilityError):
            manager.get_capability("mail.send")
        with self.assertRaises(PluginCapabilityError):
            manager.get_capability("not a capability")

    def test_duplicate_capability_provider_is_rejected(self) -> None:
        plugin_source = '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                CAPABILITIES = {"clock.now": "now"}
                def now(self):
                    return "now"
            PLUGIN_CLASS = P
        '''
        self.write_plugin("clock1", plugin_source)
        self.write_plugin("clock2", plugin_source)
        manager = PluginManager(
            self.config({
                "clock1": {"enabled": True},
                "clock2": {"enabled": True},
            }),
            self.context(),
        )

        with self.assertRaises(PluginCapabilityError):
            manager.load()

        self.assertEqual(manager.diagnostics()["capabilities"], {"clock.now": "clock1"})
        self.assertEqual(manager.plugins["clock2"].state, PluginState.FAILED)

    def test_invalid_capability_declaration_is_rejected_during_load(self) -> None:
        self.write_plugin(
            "badcap",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                CAPABILITIES = {"mail.send": "missing"}
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"badcap": {"enabled": True}}),
            self.context(),
        )

        with self.assertRaises(PluginCapabilityError):
            manager.load()

        self.assertEqual(manager.plugins["badcap"].state, PluginState.FAILED)

    def test_failed_optional_plugin_capability_is_unavailable(self) -> None:
        self.write_plugin(
            "optional_service",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                CAPABILITIES = {"service.ping": "ping"}
                def ping(self):
                    return "pong"
                def start(self):
                    raise RuntimeError("offline")
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"optional_service": {"enabled": True, "required": False}}),
            self.context(),
        )
        manager.start()

        with self.assertRaises(PluginCapabilityError):
            manager.get_capability("service.ping")

    def test_constructor_signature_error_is_reported_before_instantiation(self) -> None:
        marker = self.tmp / "constructed"
        self.write_plugin(
            "bad_constructor",
            f'''
            from pathlib import Path
            from plugin_manager import PluginBase
            class P(PluginBase):
                def __init__(self, config):
                    Path({str(marker)!r}).write_text("constructed", encoding="utf-8")
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"bad_constructor": {"enabled": True}}),
            self.context(),
        )

        with self.assertRaisesRegex(
            PluginLoadError,
            r"constructor must accept \(config, context\)",
        ):
            manager.load()

        self.assertFalse(marker.exists())
        self.assertEqual(manager.plugins["bad_constructor"].state, PluginState.FAILED)

    def test_type_error_inside_constructor_is_not_rewritten(self) -> None:
        self.write_plugin(
            "constructor_bug",
            """
            from plugin_manager import PluginBase
            class P(PluginBase):
                def __init__(self, config, context):
                    raise TypeError("internal constructor bug")
            PLUGIN_CLASS = P
            """,
        )
        manager = PluginManager(
            self.config({"constructor_bug": {"enabled": True}}),
            self.context(),
        )

        with self.assertRaisesRegex(TypeError, "internal constructor bug"):
            manager.load()

        self.assertEqual(manager.plugins["constructor_bug"].state, PluginState.FAILED)
        self.assertIn("TypeError: internal constructor bug", manager.plugins["constructor_bug"].error or "")

    def test_missing_plugin_file(self) -> None:
        manager = PluginManager(self.config({"missing": {"enabled": True}}), self.context())
        with self.assertRaises(PluginLoadError):
            manager.load()

    def test_missing_enabled_is_rejected_before_any_plugin_import(self) -> None:
        marker = self.tmp / "first-imported"
        self.write_plugin(
            "first",
            f'''
            from pathlib import Path
            from plugin_manager import PluginBase
            Path({str(marker)!r}).write_text("imported", encoding="utf-8")
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            ''',
        )
        self.write_plugin(
            "invalid",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"first": {"enabled": True}, "invalid": {}}),
            self.context(),
        )

        with self.assertRaises(PluginConfigError):
            manager.load()

        self.assertFalse(marker.exists())
        self.assertNotIn("plugins.first", sys.modules)

    def test_invalid_task_config_is_rejected_before_any_plugin_import(self) -> None:
        marker = self.tmp / "worker-imported"
        self.write_plugin(
            "worker",
            f'''
            from pathlib import Path
            from plugin_manager import PluginBase
            Path({str(marker)!r}).write_text("imported", encoding="utf-8")
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"worker": {"enabled": True, "tasks": ["bad"]}}),
            self.context(),
        )

        with self.assertRaises(PluginConfigError):
            manager.load()

        self.assertFalse(marker.exists())
        self.assertNotIn("plugins.worker", sys.modules)

    def test_missing_later_plugin_file_prevents_earlier_plugin_import(self) -> None:
        marker = self.tmp / "first-imported"
        self.write_plugin(
            "first",
            f'''
            from pathlib import Path
            from plugin_manager import PluginBase
            Path({str(marker)!r}).write_text("imported", encoding="utf-8")
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"first": {"enabled": True}, "missing": {"enabled": True}}),
            self.context(),
        )

        with self.assertRaises(PluginLoadError):
            manager.load()

        self.assertFalse(marker.exists())
        self.assertNotIn("plugins.first", sys.modules)

    def test_validate_does_not_import_plugins(self) -> None:
        marker = self.tmp / "validated-imported"
        self.write_plugin(
            "validated",
            f'''
            from pathlib import Path
            from plugin_manager import PluginBase
            Path({str(marker)!r}).write_text("imported", encoding="utf-8")
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"validated": {"enabled": True}}),
            self.context(),
        )

        manager.validate()

        self.assertFalse(marker.exists())
        self.assertEqual(manager.plugins, {})

    def test_disabled_plugin_is_not_imported_or_loaded(self) -> None:
        marker = self.tmp / "disabled-imported"
        self.write_plugin(
            "disabled",
            f'''
            from pathlib import Path
            from plugin_manager import PluginBase
            Path({str(marker)!r}).write_text("imported", encoding="utf-8")
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"disabled": {"enabled": False}}),
            self.context(),
        )

        manager.load()

        self.assertFalse(marker.exists())
        self.assertNotIn("disabled", manager.plugins)
        self.assertNotIn("plugins.disabled", sys.modules)

    def test_local_plugin_load_restores_sys_path(self) -> None:
        self.write_plugin(
            "pathsafe",
            """
            from plugin_manager import PluginBase
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            """,
        )
        root = str(self.tmp)
        sys.path[:] = [item for item in sys.path if item != root]
        before = list(sys.path)
        manager = PluginManager(
            self.config({"pathsafe": {"enabled": True}}),
            self.context(),
        )

        manager.load()

        self.assertEqual(sys.path, before)

    def test_local_plugin_import_failure_restores_sys_path(self) -> None:
        self.write_plugin(
            "broken_path",
            """
            raise RuntimeError("boom")
            """,
        )
        root = str(self.tmp)
        sys.path[:] = [item for item in sys.path if item != root]
        before = list(sys.path)
        manager = PluginManager(
            self.config({"broken_path": {"enabled": True}}),
            self.context(),
        )

        with self.assertRaises(PluginLoadError):
            manager.load()

        self.assertEqual(sys.path, before)

    def test_local_plugin_preserves_existing_sys_path_position(self) -> None:
        self.write_plugin(
            "positioned",
            """
            from plugin_manager import PluginBase
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            """,
        )
        root = str(self.tmp)
        sys.path[:] = [item for item in sys.path if item != root]
        insert_at = min(2, len(sys.path))
        sys.path.insert(insert_at, root)
        before = list(sys.path)
        manager = PluginManager(
            self.config({"positioned": {"enabled": True}}),
            self.context(),
        )

        manager.load()

        self.assertEqual(sys.path, before)

    def test_local_plugin_relative_import_works_without_persistent_sys_path(self) -> None:
        (self.plugins / "helper.py").write_text(
            'VALUE = "relative import works"\n', encoding="utf-8"
        )
        self.write_plugin(
            "relative",
            """
            from plugin_manager import PluginBase
            from .helper import VALUE
            class P(PluginBase):
                def value(self):
                    return VALUE
            PLUGIN_CLASS = P
            """,
        )
        root = str(self.tmp)
        sys.path[:] = [item for item in sys.path if item != root]
        before = list(sys.path)
        manager = PluginManager(
            self.config({"relative": {"enabled": True}}),
            self.context(),
        )

        manager.load()

        self.assertEqual(manager.get_plugin_attribute("relative", "value")(), "relative import works")
        self.assertEqual(sys.path, before)

    def test_missing_plugin_class(self) -> None:
        self.write_plugin("bad", "X = 1")
        manager = PluginManager(self.config({"bad": {"enabled": True}}), self.context())
        with self.assertRaises(PluginLoadError):
            manager.load()

    def test_invalid_plugin_class(self) -> None:
        self.write_plugin("bad", "PLUGIN_CLASS = 3")
        manager = PluginManager(self.config({"bad": {"enabled": True}}), self.context())
        with self.assertRaises(PluginLoadError):
            manager.load()

    def test_unknown_config_param_rejected(self) -> None:
        self.write_plugin(
            "p1",
            '''
            from dataclasses import dataclass
            from plugin_manager import PluginBase, PluginBaseConfig
            @dataclass(frozen=True)
            class Cfg(PluginBaseConfig):
                x: int = 1
            class P(PluginBase):
                Config = Cfg
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(self.config({"p1": {"enabled": True, "y": 2}}), self.context())
        with self.assertRaisesRegex(PluginConfigError, r"unknown config keys: \['y'\]"):
            manager.load()

    def test_unknown_config_typo_is_not_silently_ignored(self) -> None:
        self.write_plugin(
            "p1",
            '''
            from dataclasses import dataclass
            from plugin_manager import PluginBase, PluginBaseConfig
            @dataclass(frozen=True)
            class Cfg(PluginBaseConfig):
                timeout: float = 10.0
            class P(PluginBase):
                Config = Cfg
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"p1": {"enabled": True, "timeuot": 30.0}}),
            self.context(),
        )

        with self.assertRaisesRegex(
            PluginConfigError, r"unknown config keys: \['timeuot'\]"
        ):
            manager.load()

        self.assertIn("p1", manager.plugins)
        self.assertIsNone(manager.plugins["p1"].instance)
        self.assertEqual(manager.plugins["p1"].state, PluginState.FAILED)

    def test_tasks_key_is_allowed_outside_plugin_config_dataclass(self) -> None:
        self.write_plugin(
            "p1",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def run_once(self):
                    return None
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config(
                {
                    "p1": {
                        "enabled": True,
                        "tasks": {
                            "default": {
                                "execution": "direct",
                            }
                        },
                    }
                }
            ),
            self.context(),
        )

        manager.load()

        self.assertIn("p1", manager.plugins)

    def test_entry_point_validate_does_not_load_plugin_code(self) -> None:
        class InstalledPlugin(PluginBase):
            pass

        ep = FakeEntryPoint("installed", InstalledPlugin)
        manager = PluginManager(
            self.config(
                {"installed": {"enabled": True, "source": "entry-point"}},
                {"entry_point_group": "example.plugins"},
            ),
            self.context(),
        )
        from unittest.mock import patch
        with patch(
            "plugin_manager.importlib_metadata.entry_points",
            return_value=FakeEntryPoints("example.plugins", [ep]),
        ):
            manager.validate()
        self.assertEqual(ep.load_calls, 0)
        self.assertEqual(manager.plugins, {})

    def test_entry_point_backend_loads_plugin_class(self) -> None:
        class InstalledPlugin(PluginBase):
            def greet(self, name: str) -> str:
                return f"Installed hello, {name}"

        ep = FakeEntryPoint("installed", InstalledPlugin)
        manager = PluginManager(
            self.config(
                {"logical_name": {
                    "enabled": True,
                    "source": "entry-point",
                    "entry_point": "installed",
                }},
                {"entry_point_group": "example.plugins"},
            ),
            self.context(),
        )
        from unittest.mock import patch
        with patch(
            "plugin_manager.importlib_metadata.entry_points",
            return_value=FakeEntryPoints("example.plugins", [ep]),
        ):
            manager.load()
        self.assertEqual(ep.load_calls, 1)
        self.assertEqual(
            manager.get_plugin_attribute("logical_name", "greet")("Ada"),
            "Installed hello, Ada",
        )
        diagnostic = manager.diagnostics()["plugins"]["logical_name"]["metadata"]
        self.assertEqual(diagnostic["source"], "entry-point")
        self.assertEqual(diagnostic["origin"], "installed")

    def test_entry_point_backend_does_not_require_local_plugin_directory(self) -> None:
        class InstalledPlugin(PluginBase):
            pass

        shutil.rmtree(self.plugins)
        ep = FakeEntryPoint("installed", InstalledPlugin)
        manager = PluginManager(
            self.config(
                {"installed": {"enabled": True, "source": "entry-point"}},
                {"entry_point_group": "example.plugins"},
            ),
            self.context(),
        )
        from unittest.mock import patch
        with patch(
            "plugin_manager.importlib_metadata.entry_points",
            return_value=FakeEntryPoints("example.plugins", [ep]),
        ):
            manager.load()
        self.assertIn("installed", manager.plugins)

    def test_local_and_entry_point_plugins_can_be_mixed(self) -> None:
        self.write_plugin(
            "local_plugin",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            ''',
        )
        class InstalledPlugin(PluginBase):
            pass
        ep = FakeEntryPoint("installed", InstalledPlugin)
        manager = PluginManager(
            self.config(
                {
                    "local_plugin": {"enabled": True},
                    "installed_plugin": {
                        "enabled": True,
                        "source": "entry-point",
                        "entry_point": "installed",
                    },
                },
                {"entry_point_group": "example.plugins"},
            ),
            self.context(),
        )
        from unittest.mock import patch
        with patch(
            "plugin_manager.importlib_metadata.entry_points",
            return_value=FakeEntryPoints("example.plugins", [ep]),
        ):
            manager.load()
        self.assertEqual(set(manager.plugins), {"local_plugin", "installed_plugin"})
        self.assertEqual(manager.plugins["local_plugin"].source, "local")
        self.assertEqual(manager.plugins["installed_plugin"].source, "entry-point")

    def test_missing_entry_point_is_rejected_before_loading_any_entry_point(self) -> None:
        first = FakeEntryPoint("first", type("First", (PluginBase,), {}))
        manager = PluginManager(
            self.config(
                {
                    "first": {"enabled": True, "source": "entry-point"},
                    "missing": {"enabled": True, "source": "entry-point"},
                },
                {"entry_point_group": "example.plugins"},
            ),
            self.context(),
        )
        from unittest.mock import patch
        with patch(
            "plugin_manager.importlib_metadata.entry_points",
            return_value=FakeEntryPoints("example.plugins", [first]),
        ):
            with self.assertRaisesRegex(PluginLoadError, "entry point 'missing' not found"):
                manager.load()
        self.assertEqual(first.load_calls, 0)

    def test_invalid_plugin_source_is_rejected(self) -> None:
        manager = PluginManager(
            self.config({"p1": {"enabled": True, "source": "remote"}}),
            self.context(),
        )
        with self.assertRaisesRegex(
            PluginConfigError, "source must be 'local' or 'entry-point'"
        ):
            manager.validate()

    def test_entry_point_source_rejects_module_key(self) -> None:
        manager = PluginManager(
            self.config({"p1": {
                "enabled": True,
                "source": "entry-point",
                "module": "p1",
            }}),
            self.context(),
        )
        with self.assertRaisesRegex(
            PluginConfigError, "module is only valid for source='local'"
        ):
            manager.validate()

    def test_lifecycle_order_and_reverse_shutdown(self) -> None:
        for name in ["a", "b"]:
            self.write_plugin(
                name,
                f'''
                from plugin_manager import PluginBase
                class P(PluginBase):
                    def start(self):
                        self.context.manager.order.append("start:{name}")
                    def stop(self):
                        self.context.manager.order.append("stop:{name}")
                PLUGIN_CLASS = P
                ''',
            )
        manager = PluginManager(self.config({"a": {"enabled": True}, "b": {"enabled": True}}), self.context())
        manager.order = []  # type: ignore[attr-defined]
        manager.start()
        manager.stop()
        self.assertEqual(manager.order, ["start:a", "start:b", "stop:b", "stop:a"])  # type: ignore[attr-defined]

    def test_task_registration_start_stop(self) -> None:
        self.write_plugin(
            "worker",
            '''
            from queue import Queue
            from plugin_manager import PluginBase
            class P(PluginBase):
                def __init__(self, config, context):
                    self.config = config
                    self.context = context
                    context.queues["events"] = Queue()
                def run_once(self): pass
            PLUGIN_CLASS = P
            ''',
        )
        cfg = self.config(
            {
                "worker": {
                    "enabled": True,
                    "tasks": {
                        "periodic": {
                            "execution": "task",
                            "method": "run_once",
                            "period_us": 10,
                            "pool_id": "fast",
                            "count": 1,
                        }
                    },
                }
            },
            manager={"task_manager": FakeTaskManager()},
        )
        (self.tmp / "fake_backend.py").write_text(dedent("""
            class Scheduler:
                def __init__(self, default_pool_workers=1):
                    self.default_pool_workers = default_pool_workers
                    self.pools = {"default": default_pool_workers}
                    self.tasks = []
                    self.started = False
                def create_pool(self, pool_id, workers=1):
                    self.pools[pool_id] = workers
                def add_task(self, **kwargs):
                    if self.started:
                        raise RuntimeError("cannot add tasks while scheduler is running")
                    self.tasks.append(kwargs)
                    return str(kwargs.get("task_id"))
                def start_engine(self):
                    if not self.tasks:
                        raise RuntimeError("no tasks scheduled")
                    self.started = True
                def stop(self):
                    self.started = False
                def clear_tasks(self):
                    self.tasks.clear()
                def read_stats(self):
                    return {"running": self.started, "task_count": len(self.tasks)}
                def read_jobs(self):
                    return [{"id": t.get("task_id"), "task_error": ""} for t in self.tasks]
                def read_pool_stats(self, pool_id=None):
                    return self.pools if pool_id is None else {pool_id: self.pools[pool_id]}
            """), encoding="utf-8")
        manager = PluginManager(cfg, self.context())
        manager.start()
        task_manager = manager.config.task_manager
        self.assertEqual([task["task_id"] for task in task_manager.scheduler.tasks], ["worker:periodic"])
        task_manager.scheduler.start_engine()  # application-owned lifecycle
        manager.stop()
        self.assertTrue(task_manager.scheduler.started)
        self.assertEqual(task_manager.suspended, ["worker:periodic"])
        self.assertEqual(
            [task["task_id"] for task in task_manager.scheduler.tasks],
            ["worker:periodic"],
        )
        self.assertEqual(manager.diagnostics()["tasks"], ["worker:periodic"])

    def test_task_manager_instance_from_config(self) -> None:
        self.write_plugin(
            "p1",
            """
            from plugin_manager import PluginBase
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            """,
        )
        task_manager = NoOpTaskManager()
        cfg = self.config({"p1": {"enabled": True}}, manager={"task_manager": task_manager})
        manager = PluginManager(cfg, self.context())
        manager.start()
        self.assertIs(manager.config.task_manager, task_manager)
        self.assertEqual(manager.diagnostics()["tasks"], [])
        manager.stop()

    def test_task_manager_class_is_rejected(self) -> None:
        cfg = self.config(
            {},
            manager={"task_manager": NoOpTaskManager},
        )
        manager = PluginManager(cfg, self.context())
        with self.assertRaisesRegex(
            PluginConfigError,
            "must be a TaskManager instance",
        ):
            manager.start()

    def test_startup_failure_cleanup(self) -> None:
        self.write_plugin(
            "ok",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self): self.context.manager.order.append("start:ok")
                def stop(self): self.context.manager.order.append("stop:ok")
            PLUGIN_CLASS = P
            ''',
        )
        self.write_plugin(
            "fail",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self): raise RuntimeError("boom")
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(self.config({"ok": {"enabled": True}, "fail": {"enabled": True}}), self.context())
        manager.order = []  # type: ignore[attr-defined]
        with self.assertRaises(PluginLifecycleError):
            manager.start()
        self.assertEqual(manager.order, ["start:ok", "stop:ok"])  # type: ignore[attr-defined]

    def test_continue_on_error_policy(self) -> None:
        self.write_plugin("bad", "raise RuntimeError('import boom')")
        self.write_plugin(
            "ok",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"bad": {"enabled": True}, "ok": {"enabled": True}}, manager={"fail_policy": "continue-on-error"}),
            self.context(),
        )
        manager.load()
        self.assertEqual(manager.plugins["bad"].state.value, "failed")
        self.assertEqual(manager.plugins["ok"].state.value, "created")

    def test_manager_state_machine_restart_and_close(self) -> None:
        self.write_plugin(
            "cycle",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self): self.context.manager.events.append("start")
                def stop(self): self.context.manager.events.append("stop")
                def close(self): self.context.manager.events.append("close")
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(self.config({"cycle": {"enabled": True}}), self.context())
        manager.events = []  # type: ignore[attr-defined]

        self.assertEqual(manager.state, PluginManagerState.NEW)
        manager.load()
        self.assertEqual(manager.state, PluginManagerState.LOADED)
        manager.start()
        self.assertEqual(manager.state, PluginManagerState.STARTED)
        manager.stop()
        self.assertEqual(manager.state, PluginManagerState.STOPPED)
        self.assertEqual(manager.plugins["cycle"].state, PluginState.STOPPED)
        self.assertEqual(manager.events, ["start", "stop"])  # type: ignore[attr-defined]

        manager.start()
        self.assertEqual(manager.state, PluginManagerState.STARTED)
        manager.close()
        self.assertEqual(manager.state, PluginManagerState.CLOSED)
        self.assertEqual(manager.plugins["cycle"].state, PluginState.CLOSED)
        self.assertEqual(
            manager.events, ["start", "stop", "start", "stop", "close"]
        )  # type: ignore[attr-defined]

    def test_repeated_start_is_idempotent(self) -> None:
        self.write_plugin(
            "once",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self): self.context.manager.starts += 1
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(self.config({"once": {"enabled": True}}), self.context())
        manager.starts = 0  # type: ignore[attr-defined]
        manager.start()
        manager.start()
        self.assertEqual(manager.starts, 1)  # type: ignore[attr-defined]
        manager.close()

    def test_restart_resumes_tasks_without_reregistering(self) -> None:
        self.write_plugin(
            "worker_restart",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def run_once(self): pass
            PLUGIN_CLASS = P
            ''',
        )
        task_manager = NoOpTaskManager()
        cfg = self.config(
            {
                "worker_restart": {
                    "enabled": True,
                    "tasks": {
                        "periodic": {
                            "execution": "task",
                            "method": "run_once",
                            "period_us": 10,
                        }
                    },
                }
            },
            manager={"task_manager": task_manager},
        )
        manager = PluginManager(cfg, self.context())
        manager.start()
        self.assertEqual(list(task_manager.tasks), ["worker_restart:periodic"])
        manager.stop()
        manager.start()

        self.assertEqual(list(task_manager.tasks), ["worker_restart:periodic"])
        self.assertEqual(task_manager.suspended, ["worker_restart:periodic"])
        self.assertEqual(task_manager.resumed, ["worker_restart:periodic"])
        manager.close()

    def test_restart_with_scheduled_tasks_requires_resume_support(self) -> None:
        class SuspendOnlyTaskManager:
            def __init__(self) -> None:
                self.tasks: dict[str, dict[str, object]] = {}
            def add_task(self, **task_spec: object) -> str:
                task_id = str(task_spec["task_id"])
                self.tasks[task_id] = dict(task_spec)
                return task_id
            def suspend(self, task_id: str, for_: int = 0) -> None:
                return None

        self.write_plugin(
            "worker_no_resume",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def run_once(self): pass
            PLUGIN_CLASS = P
            ''',
        )
        cfg = self.config(
            {
                "worker_no_resume": {
                    "enabled": True,
                    "tasks": {"default": {"execution": "task", "method": "run_once"}},
                }
            },
            manager={"task_manager": SuspendOnlyTaskManager()},
        )
        manager = PluginManager(cfg, self.context())
        manager.start()
        manager.stop()
        with self.assertRaisesRegex(PluginLifecycleError, "must provide resume"):
            manager.start()
        self.assertEqual(manager.state, PluginManagerState.STOPPED)
        manager.close()

    def test_close_is_final_and_idempotent(self) -> None:
        self.write_plugin(
            "final",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def close(self): self.context.manager.closes += 1
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(self.config({"final": {"enabled": True}}), self.context())
        manager.closes = 0  # type: ignore[attr-defined]
        manager.load()
        manager.close()
        manager.close()
        self.assertEqual(manager.closes, 1)  # type: ignore[attr-defined]
        self.assertEqual(manager.state, PluginManagerState.CLOSED)
        with self.assertRaisesRegex(PluginLifecycleError, "closed"):
            manager.start()
        with self.assertRaisesRegex(PluginLifecycleError, "closed"):
            manager.load()
        with self.assertRaisesRegex(PluginLifecycleError, "after manager.close"):
            manager.get_plugin_attribute("final", "")

    def test_optional_plugin_startup_failure_does_not_fail_manager(self) -> None:
        self.write_plugin(
            "optional_bad",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self): raise RuntimeError("optional boom")
            PLUGIN_CLASS = P
            ''',
        )
        self.write_plugin(
            "required_good",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self): self.context.manager.events.append("good:start")
                def stop(self): self.context.manager.events.append("good:stop")
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config(
                {
                    "optional_bad": {"enabled": True, "required": False},
                    "required_good": {"enabled": True},
                }
            ),
            self.context(),
        )
        manager.events = []  # type: ignore[attr-defined]

        manager.start()

        self.assertEqual(manager.state, PluginManagerState.STARTED)
        self.assertEqual(manager.plugins["optional_bad"].state, PluginState.FAILED)
        self.assertIn("optional boom", manager.plugins["optional_bad"].error or "")
        self.assertEqual(manager.plugins["required_good"].state, PluginState.STARTED)
        self.assertEqual(manager.events, ["good:start"])  # type: ignore[attr-defined]
        diag = manager.diagnostics()["plugins"]
        self.assertFalse(diag["optional_bad"]["required"])
        self.assertTrue(diag["required_good"]["required"])
        manager.close()
        self.assertEqual(manager.events, ["good:start", "good:stop"])  # type: ignore[attr-defined]

    def test_optional_failed_plugin_tasks_are_not_registered(self) -> None:
        self.write_plugin(
            "optional_worker",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self): raise RuntimeError("cannot start")
                def run_once(self): pass
            PLUGIN_CLASS = P
            ''',
        )
        task_manager = NoOpTaskManager()
        manager = PluginManager(
            self.config(
                {
                    "optional_worker": {
                        "enabled": True,
                        "required": False,
                        "tasks": {
                            "periodic": {
                                "execution": "task",
                                "method": "run_once",
                            }
                        },
                    }
                },
                manager={"task_manager": task_manager},
            ),
            self.context(),
        )

        manager.start()

        self.assertEqual(manager.state, PluginManagerState.STARTED)
        self.assertEqual(manager.plugins["optional_worker"].state, PluginState.FAILED)
        self.assertEqual(task_manager.tasks, {})
        self.assertEqual(manager.diagnostics()["tasks"], [])
        manager.close()

    def test_optional_failed_plugin_is_not_retried_on_restart(self) -> None:
        self.write_plugin(
            "optional_once",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self):
                    self.context.manager.optional_starts += 1
                    raise RuntimeError("still broken")
            PLUGIN_CLASS = P
            ''',
        )
        self.write_plugin(
            "healthy_restart",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self): self.context.manager.healthy_starts += 1
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config(
                {
                    "optional_once": {"enabled": True, "required": False},
                    "healthy_restart": {"enabled": True},
                }
            ),
            self.context(),
        )
        manager.optional_starts = 0  # type: ignore[attr-defined]
        manager.healthy_starts = 0  # type: ignore[attr-defined]

        manager.start()
        manager.stop()
        manager.start()

        self.assertEqual(manager.optional_starts, 1)  # type: ignore[attr-defined]
        self.assertEqual(manager.healthy_starts, 2)  # type: ignore[attr-defined]
        self.assertEqual(manager.plugins["optional_once"].state, PluginState.FAILED)
        manager.close()

    def test_required_must_be_boolean_before_plugin_import(self) -> None:
        marker_path = self.tmp / "required-imported"
        self.write_plugin(
            "required_type",
            f'''
            from pathlib import Path
            from plugin_manager import PluginBase
            Path({str(marker_path)!r}).write_text("imported", encoding="utf-8")
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(
            self.config({"required_type": {"enabled": True, "required": "no"}}),
            self.context(),
        )

        with self.assertRaisesRegex(PluginConfigError, "'required' must be bool"):
            manager.load()

        self.assertFalse(marker_path.exists())
        self.assertNotIn("plugins.required_type", sys.modules)

    def test_startup_failure_moves_manager_to_failed(self) -> None:
        self.write_plugin(
            "fail_state",
            '''
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self): raise RuntimeError("boom")
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(self.config({"fail_state": {"enabled": True}}), self.context())
        with self.assertRaises(PluginLifecycleError):
            manager.start()
        self.assertEqual(manager.state, PluginManagerState.FAILED)
        with self.assertRaisesRegex(PluginLifecycleError, "state is 'failed'"):
            manager.start()
        manager.close()
        self.assertEqual(manager.state, PluginManagerState.CLOSED)

    def test_diagnostics_include_manager_state(self) -> None:
        manager = PluginManager(self.config({}), self.context())
        self.assertEqual(manager.diagnostics()["state"], "new")
        manager.start()
        self.assertEqual(manager.diagnostics()["state"], "started")
        manager.stop()
        self.assertEqual(manager.diagnostics()["state"], "stopped")
        manager.close()
        self.assertEqual(manager.diagnostics()["state"], "closed")

    def test_missing_module_constants_are_allowed(self) -> None:
        (self.plugins / "plain.py").write_text(
            "from plugin_manager import PluginBase\nclass P(PluginBase): pass\nPLUGIN_CLASS = P\n",
            encoding="utf-8",
        )
        manager = PluginManager(self.config({"plain": {"enabled": True}}), self.context())
        manager.load()
        self.assertEqual(manager.plugins["plain"].name, "plain")

    def test_module_constants_populate_matching_config_fields(self) -> None:
        self.write_plugin(
            "meta",
            """
            from dataclasses import dataclass
            from plugin_manager import PluginBaseConfig, PluginBase
            @dataclass(frozen=True)
            class Cfg(PluginBaseConfig):
                name: str = ""
                description: str = ""
                author: str | None = None
                license: str | None = None
            class P(PluginBase):
                Config = Cfg
            PLUGIN_CLASS = P
            """,
        )
        manager = PluginManager(self.config({"meta": {"enabled": True}}), self.context())
        manager.load()
        plugin = manager.plugins["meta"]
        self.assertEqual(plugin.instance.config.name, "meta")
        self.assertEqual(plugin.instance.config.version, "1.0.0")
        self.assertEqual(plugin.instance.config.description, "Test plugin.")
        self.assertEqual(plugin.instance.config.api_version, "1.0")
        metadata = manager.diagnostics()["plugins"]["meta"]["metadata"]
        self.assertEqual(metadata["description"], "Test plugin.")

    def test_module_constants_override_same_named_input_fields(self) -> None:
        self.write_plugin(
            "fixed",
            """
            from dataclasses import dataclass
            from plugin_manager import PluginBaseConfig, PluginBase
            @dataclass(frozen=True)
            class Cfg(PluginBaseConfig):
                name: str = ""
            class P(PluginBase):
                Config = Cfg
            PLUGIN_CLASS = P
            """,
        )
        config = self.config({
            "fixed": {
                "enabled": True,
                "name": "input-name",
                "version": "input-version",
                "api_version": "input-api",
            }
        })
        manager = PluginManager(config, self.context())
        manager.load()
        cfg = manager.plugins["fixed"].instance.config
        self.assertEqual(cfg.name, "fixed")
        self.assertEqual(cfg.version, "1.0.0")
        self.assertEqual(cfg.api_version, "1.0")

    def test_constant_validation_belongs_to_config_post_init(self) -> None:
        self.write_plugin(
            "validated",
            """
            PLUGIN_VERSION = "invalid"
            from dataclasses import dataclass
            from plugin_manager import PluginBaseConfig, PluginBase
            @dataclass(frozen=True)
            class Cfg(PluginBaseConfig):
                def __post_init__(self):
                    if self.version == "invalid":
                        raise ValueError("invalid plugin version")
            class P(PluginBase):
                Config = Cfg
            PLUGIN_CLASS = P
            """,
        )
        manager = PluginManager(self.config({"validated": {"enabled": True}}), self.context())
        with self.assertRaisesRegex(ValueError, "invalid plugin version"):
            manager.load()

    def test_pm_does_not_validate_constant_types(self) -> None:
        self.write_plugin(
            "opaque",
            """
            PLUGIN_VERSION = 123
            from dataclasses import dataclass
            from typing import Any
            from plugin_manager import PluginBaseConfig, PluginBase
            @dataclass(frozen=True)
            class Cfg(PluginBaseConfig):
                version: Any = None
            class P(PluginBase):
                Config = Cfg
            PLUGIN_CLASS = P
            """,
        )
        manager = PluginManager(self.config({"opaque": {"enabled": True}}), self.context())
        manager.load()
        self.assertEqual(manager.plugins["opaque"].instance.config.version, 123)



    def test_plugin_version_requirement_is_accepted(self) -> None:
        manager = PluginManager(
            self.config({
                "compatible": {
                    "enabled": True,
                    "req_version": ">=1.0,<2.0",
                    "req_api_version": ">=1.0",
                }
            }),
            self.context(),
        )
        self.write_plugin(
            "compatible",
            """
            from dataclasses import dataclass
            from plugin_manager import PluginBaseConfig, PluginBase
            PLUGIN_VERSION = "1.2.0"
            PLUGIN_API_VERSION = "1.0"
            @dataclass(frozen=True)
            class Cfg(PluginBaseConfig):
                pass
            class P(PluginBase):
                Config = Cfg
            PLUGIN_CLASS = P
            """,
        )
        manager.load()
        cfg = manager.plugins["compatible"].instance.config
        self.assertEqual(cfg.req_version, ">=1.0,<2.0")
        self.assertEqual(cfg.req_api_version, ">=1.0")

    def test_plugin_version_requirement_is_rejected(self) -> None:
        self.write_plugin(
            "incompatible",
            """
            from dataclasses import dataclass
            from plugin_manager import PluginBaseConfig, PluginBase
            PLUGIN_VERSION = "2.0.0"
            PLUGIN_API_VERSION = "1.0"
            @dataclass(frozen=True)
            class Cfg(PluginBaseConfig):
                pass
            class P(PluginBase):
                Config = Cfg
            PLUGIN_CLASS = P
            """,
        )
        manager = PluginManager(
            self.config({
                "incompatible": {
                    "enabled": True,
                    "req_version": "<2.0",
                }
            }),
            self.context(),
        )
        with self.assertRaisesRegex(ValueError, "does not satisfy req_version"):
            manager.load()

    def test_api_version_requirement_is_rejected(self) -> None:
        self.write_plugin(
            "api_incompatible",
            """
            from dataclasses import dataclass
            from plugin_manager import PluginBaseConfig, PluginBase
            PLUGIN_VERSION = "1.0.0"
            PLUGIN_API_VERSION = "1.0"
            @dataclass(frozen=True)
            class Cfg(PluginBaseConfig):
                pass
            class P(PluginBase):
                Config = Cfg
            PLUGIN_CLASS = P
            """,
        )
        manager = PluginManager(
            self.config({
                "api_incompatible": {
                    "enabled": True,
                    "req_api_version": ">=2.0",
                }
            }),
            self.context(),
        )
        with self.assertRaisesRegex(ValueError, "does not satisfy req_api_version"):
            manager.load()

    def test_none_requirements_skip_version_checks(self) -> None:
        from plugin_manager import PluginBaseConfig

        cfg = PluginBaseConfig(
            version="not-a-version",
            api_version="also-not-a-version",
        )
        self.assertEqual(cfg.version, "not-a-version")

    def test_dependencies_control_start_and_stop_order(self) -> None:
        for name in ("base", "middle", "top"):
            source = """
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self):
                    self.context.app.append(%r)
                def stop(self):
                    self.context.app.append(%r)
            PLUGIN_CLASS = P
            """ % ("start:" + name, "stop:" + name)
            self.write_plugin(name, source)

        events: list[str] = []
        config = self.config({
            "top": {"enabled": True, "requires": ["middle"]},
            "middle": {"enabled": True, "requires": ["base"]},
            "base": {"enabled": True},
        })
        manager = PluginManager(config, {"app_root": self.tmp, "app": events})

        manager.start()
        self.assertEqual(events, ["start:base", "start:middle", "start:top"])
        self.assertEqual(manager.started_order, ["base", "middle", "top"])

        manager.stop()
        self.assertEqual(
            events,
            [
                "start:base", "start:middle", "start:top",
                "stop:top", "stop:middle", "stop:base",
            ],
        )

    def test_dependency_validation_rejects_missing_or_disabled_dependencies(self) -> None:
        for name in ("consumer", "provider"):
            self.write_plugin(
                name,
                """
                from plugin_manager import PluginBase
                class P(PluginBase):
                    pass
                PLUGIN_CLASS = P
                """,
            )
        manager = PluginManager(
            self.config({
                "consumer": {"enabled": True, "requires": ["provider"]},
                "provider": {"enabled": False},
            }),
            self.context(),
        )

        with self.assertRaisesRegex(PluginConfigError, "dependencies must reference enabled plugins"):
            manager.validate()

    def test_dependency_validation_rejects_self_dependency_and_cycles(self) -> None:
        for name in ("a", "b"):
            self.write_plugin(
                name,
                """
                from plugin_manager import PluginBase
                class P(PluginBase):
                    pass
                PLUGIN_CLASS = P
                """,
            )

        self_dep = PluginManager(
            self.config({"a": {"enabled": True, "requires": ["a"]}}),
            self.context(),
        )
        with self.assertRaisesRegex(PluginConfigError, "cannot depend on itself"):
            self_dep.validate()

        cycle = PluginManager(
            self.config({
                "a": {"enabled": True, "requires": ["b"]},
                "b": {"enabled": True, "requires": ["a"]},
            }),
            self.context(),
        )
        with self.assertRaisesRegex(PluginConfigError, "plugin dependency cycle"):
            cycle.validate()

    def test_dependency_shape_is_rejected_before_plugin_import(self) -> None:
        marker = self.tmp / "dependency-imported"
        self.write_plugin(
            "consumer",
            """
            from pathlib import Path
            from plugin_manager import PluginBase
            Path(%r).write_text("imported", encoding="utf-8")
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            """ % str(marker),
        )
        manager = PluginManager(
            self.config({"consumer": {"enabled": True, "requires": "provider"}}),
            self.context(),
        )

        with self.assertRaisesRegex(PluginConfigError, "'requires' must be a list"):
            manager.load()

        self.assertFalse(marker.exists())

    def test_optional_dependency_failure_isolated_and_blocks_optional_dependents(self) -> None:
        self.write_plugin(
            "provider",
            """
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self):
                    raise RuntimeError("provider unavailable")
            PLUGIN_CLASS = P
            """,
        )
        self.write_plugin(
            "consumer",
            """
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self):
                    self.context.app.append("consumer-started")
            PLUGIN_CLASS = P
            """,
        )
        self.write_plugin(
            "independent",
            """
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self):
                    self.context.app.append("independent-started")
            PLUGIN_CLASS = P
            """,
        )
        events: list[str] = []
        manager = PluginManager(
            self.config({
                "consumer": {
                    "enabled": True,
                    "required": False,
                    "requires": ["provider"],
                },
                "provider": {"enabled": True, "required": False},
                "independent": {"enabled": True},
            }),
            {"app_root": self.tmp, "app": events},
        )

        manager.start()

        self.assertEqual(manager.state, PluginManagerState.STARTED)
        self.assertEqual(manager.plugins["provider"].state, PluginState.FAILED)
        self.assertEqual(manager.plugins["consumer"].state, PluginState.FAILED)
        self.assertIn("dependency:", manager.plugins["consumer"].error or "")
        self.assertEqual(events, ["independent-started"])

    def test_required_dependent_makes_unavailable_dependency_fatal(self) -> None:
        self.write_plugin(
            "provider",
            """
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self):
                    raise RuntimeError("provider unavailable")
            PLUGIN_CLASS = P
            """,
        )
        self.write_plugin(
            "consumer",
            """
            from plugin_manager import PluginBase
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            """,
        )
        manager = PluginManager(
            self.config({
                "consumer": {"enabled": True, "requires": ["provider"]},
                "provider": {"enabled": True, "required": False},
            }),
            self.context(),
        )

        with self.assertRaisesRegex(PluginLifecycleError, "dependencies are not started"):
            manager.start()

        self.assertEqual(manager.state, PluginManagerState.FAILED)
        self.assertEqual(manager.plugins["consumer"].state, PluginState.FAILED)

    def test_dependency_diagnostics_expose_requires(self) -> None:
        for name in ("base", "consumer"):
            self.write_plugin(
                name,
                """
                from plugin_manager import PluginBase
                class P(PluginBase):
                    pass
                PLUGIN_CLASS = P
                """,
            )
        manager = PluginManager(
            self.config({
                "consumer": {"enabled": True, "requires": ["base"]},
                "base": {"enabled": True},
            }),
            self.context(),
        )
        manager.load()

        diagnostics = manager.diagnostics()
        self.assertEqual(diagnostics["plugins"]["consumer"]["requires"], ["base"])

    def test_debug_config_logging_redacts_common_and_nested_secret_keys(self) -> None:
        self.write_plugin(
            "secure",
            """
            from dataclasses import dataclass, field
            from plugin_manager import PluginBaseConfig, PluginBase
            @dataclass(frozen=True)
            class Cfg(PluginBaseConfig):
                endpoint: str = "https://example.invalid"
                api_key: str = "default-api-key"
                options: dict = field(default_factory=dict)
            class P(PluginBase):
                Config = Cfg
            PLUGIN_CLASS = P
            """,
        )
        manager = PluginManager(
            self.config({
                "secure": {
                    "enabled": True,
                    "endpoint": "https://service.invalid",
                    "api_key": "TOP-SECRET-API-KEY",
                    "options": {
                        "token": "TOP-SECRET-TOKEN",
                        "region": "eu-west",
                    },
                }
            }),
            self.context(),
        )

        with self.assertLogs("plugin_manager", level="DEBUG") as captured:
            manager.load()

        output = "\n".join(captured.output)
        self.assertNotIn("TOP-SECRET-API-KEY", output)
        self.assertNotIn("TOP-SECRET-TOKEN", output)
        self.assertIn("'api_key': '***'", output)
        self.assertIn("'token': '***'", output)
        self.assertIn("https://service.invalid", output)
        self.assertIn("eu-west", output)

    def test_debug_config_logging_honors_secret_field_metadata(self) -> None:
        self.write_plugin(
            "secure_metadata",
            """
            from dataclasses import dataclass, field
            from plugin_manager import PluginBaseConfig, PluginBase
            @dataclass(frozen=True)
            class Cfg(PluginBaseConfig):
                client_pin: str = field(default="0000", metadata={"secret": True})
                label: str = "visible"
            class P(PluginBase):
                Config = Cfg
            PLUGIN_CLASS = P
            """,
        )
        manager = PluginManager(
            self.config({
                "secure_metadata": {
                    "enabled": True,
                    "client_pin": "987654",
                    "label": "public-label",
                }
            }),
            self.context(),
        )

        with self.assertLogs("plugin_manager", level="DEBUG") as captured:
            manager.load()

        output = "\n".join(captured.output)
        self.assertNotIn("987654", output)
        self.assertIn("'client_pin': '***'", output)
        self.assertIn("public-label", output)


    def test_stronger_diagnostics_include_lifecycle_compatibility_and_start_count(self) -> None:
        self.write_plugin(
            "diagnostic_plugin",
            """
            from plugin_manager import PluginBase
            class P(PluginBase):
                pass
            PLUGIN_CLASS = P
            """,
        )
        manager = PluginManager(
            self.config({
                "diagnostic_plugin": {
                    "enabled": True,
                    "req_version": ">=1.0",
                    "req_api_version": ">=1.0",
                }
            }),
            self.context(),
        )

        manager.start()
        diag = manager.diagnostics()
        plugin_diag = diag["plugins"]["diagnostic_plugin"]

        self.assertEqual(diag["state"], "started")
        self.assertTrue(diag["lifecycle"]["created_at"].endswith("Z"))
        self.assertTrue(diag["lifecycle"]["loaded_at"].endswith("Z"))
        self.assertTrue(diag["lifecycle"]["start_started_at"].endswith("Z"))
        self.assertTrue(diag["lifecycle"]["started_at"].endswith("Z"))
        self.assertGreaterEqual(diag["lifecycle"]["load_duration_ms"], 0)
        self.assertGreaterEqual(diag["lifecycle"]["start_duration_ms"], 0)
        self.assertTrue(plugin_diag["lifecycle"]["loaded_at"].endswith("Z"))
        self.assertTrue(plugin_diag["lifecycle"]["start_started_at"].endswith("Z"))
        self.assertTrue(plugin_diag["lifecycle"]["started_at"].endswith("Z"))
        self.assertEqual(plugin_diag["lifecycle"]["start_count"], 1)
        self.assertEqual(
            plugin_diag["compatibility"]["plugin_version"],
            {"version": "1.0.0", "requirement": ">=1.0", "status": "satisfied"},
        )
        self.assertEqual(
            plugin_diag["compatibility"]["api_version"],
            {"version": "1.0", "requirement": ">=1.0", "status": "satisfied"},
        )

        manager.stop()
        manager.start()
        restarted = manager.diagnostics()["plugins"]["diagnostic_plugin"]
        self.assertEqual(restarted["lifecycle"]["start_count"], 2)

    def test_diagnostics_map_registered_tasks_to_owning_plugin(self) -> None:
        self.write_plugin(
            "task_owner",
            """
            from plugin_manager import PluginBase
            class P(PluginBase):
                def periodic(self):
                    return None
            PLUGIN_CLASS = P
            """,
        )
        task_manager = NoOpTaskManager()
        manager = PluginManager(
            self.config(
                {
                    "task_owner": {
                        "enabled": True,
                        "tasks": {
                            "periodic": {"execution": "task", "task_id": "owned-task"}
                        },
                    }
                },
                {"task_manager": task_manager},
            ),
            self.context(),
        )

        manager.start()
        diag = manager.diagnostics()

        self.assertEqual(diag["tasks"], ["owned-task"])
        self.assertEqual(diag["tasks_by_plugin"], {"task_owner": ["owned-task"]})
        self.assertEqual(diag["plugins"]["task_owner"]["task_ids"], ["owned-task"])

    def test_diagnostics_expose_structured_plugin_error(self) -> None:
        self.write_plugin(
            "optional_broken",
            """
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self):
                    raise RuntimeError("service offline")
            PLUGIN_CLASS = P
            """,
        )
        manager = PluginManager(
            self.config({"optional_broken": {"enabled": True, "required": False}}),
            self.context(),
        )

        manager.start()
        diag = manager.diagnostics()["plugins"]["optional_broken"]

        self.assertEqual(diag["state"], "failed")
        self.assertEqual(diag["error"], "start: RuntimeError: service offline")
        self.assertEqual(
            diag["error_info"],
            {"phase": "start", "type": "RuntimeError", "message": "service offline"},
        )

    def test_diagnostics_expose_structured_manager_start_error(self) -> None:
        self.write_plugin(
            "required_broken",
            """
            from plugin_manager import PluginBase
            class P(PluginBase):
                def start(self):
                    raise ValueError("cannot initialize")
            PLUGIN_CLASS = P
            """,
        )
        manager = PluginManager(
            self.config({"required_broken": {"enabled": True}}),
            self.context(),
        )

        with self.assertRaises(PluginLifecycleError):
            manager.start()

        diag = manager.diagnostics()
        self.assertEqual(diag["state"], "failed")
        self.assertEqual(diag["error"]["phase"], "start")
        self.assertIn(diag["error"]["type"], {"ValueError", "PluginLifecycleError"})
        self.assertTrue(diag["error"]["message"])


if __name__ == "__main__":
    unittest.main()
