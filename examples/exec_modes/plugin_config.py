"""Example Python configuration module consumed by PluginManager."""

PLUGIN_MANAGER = {
    "fail_policy": "fail-fast",
    "plugin_dir": "plugins",
}

PLUGINS = {
    "example_direct": {
        "enabled": True,
        "greeting": "Ciao",
    },
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
