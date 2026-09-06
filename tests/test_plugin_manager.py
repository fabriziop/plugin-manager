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
    PluginConfigError,
    PluginLifecycleError,
    PluginLoadError,
    PluginManager,
)


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

    def add_task(self, **task_spec: object) -> str:
        task_id = str(task_spec["task_id"])
        self.tasks[task_id] = dict(task_spec)
        return task_id

    def suspend(self, task_id: str, for_: int = 0) -> str:
        self.suspended.append(task_id)
        return "forever" if for_ == 0 else str(for_)


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

    def test_missing_plugin_file(self) -> None:
        manager = PluginManager(self.config({"missing": {"enabled": True}}), self.context())
        with self.assertRaises(PluginLoadError):
            manager.load()

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
            from plugin_manager import PluginBase
            @dataclass(frozen=True)
            class Cfg:
                x: int = 1
            class P(PluginBase):
                Config = Cfg
            PLUGIN_CLASS = P
            ''',
        )
        manager = PluginManager(self.config({"p1": {"enabled": True, "y": 2}}), self.context())
        with self.assertRaises(PluginConfigError):
            manager.load()

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


if __name__ == "__main__":
    unittest.main()
