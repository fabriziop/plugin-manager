# Execution modes with system Conductor

This example uses a system-installed `conductor` package as the real task
scheduler.

Run it from the project root:

```bash
python examples/exec_modes_conductor/exec_modes_conductor.py
```

The application creates, configures, starts, stops, clears, and inspects the
Conductor scheduler. It supplies Plugin Manager with an already-created
adapter implementing:

```python
add_task(**task_spec)
suspend(task_id, for_=0)
```

Plugin Manager registers only tasks requested by plugin configuration. During
`PluginManager.stop()`, it suspends the task IDs that it registered, but it
does not resume, remove, clear, start, or stop scheduler tasks.

Conductor rejects suspension of finite tasks that have already completed. The
example adapter treats that specific condition as a successful no-op, making
Plugin Manager shutdown idempotent. Other scheduler errors still propagate.

After Plugin Manager has stopped its plugins, the application explicitly
stops and clears the scheduler according to its own lifecycle policy.
