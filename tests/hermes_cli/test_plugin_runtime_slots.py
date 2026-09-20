"""RX9 H2 runtime-slot contract tests through real PluginManager discovery."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import yaml

from hermes_cli.plugins import PluginManager


def _write_slot_plugin(home: Path, *, fail_marker: bool = False, ordinary: bool = False) -> Path:
    plugin = home / "plugins" / "slot-fixture"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "plugin.yaml").write_text(
        "name: slot-fixture\nversion: 0.1.0\ndescription: runtime slot fixture\n",
        encoding="utf-8",
    )
    if ordinary:
        source = (
            "import json\n"
            "from pathlib import Path\n"
            "\n"
            "def register(ctx):\n"
            "    path = Path(ctx._manager.home_path) / 'slot-observations.json'\n"
            "    rows = json.loads(path.read_text()) if path.exists() else []\n"
            "    rows.append({'ordinary': True})\n"
            "    path.write_text(json.dumps(rows))\n"
            "    ctx.register_hook('on_session_start', lambda **kw: {'ordinary': True})\n"
        )
    else:
        marker = repr(str(home / "fail-replacement"))
        source = (
            "import json\n"
            "from pathlib import Path\n"
            "\n"
            "def register(ctx):\n"
            "    slot = ctx.runtime_slot('service')\n"
            "    service = slot.get_or_create(lambda: object())\n"
            "    path = Path(ctx._manager.home_path) / 'slot-observations.json'\n"
            "    rows = json.loads(path.read_text()) if path.exists() else []\n"
            "    rows.append({'object_id': id(service), 'event': 'register'})\n"
            "    path.write_text(json.dumps(rows))\n"
            "    cleanup_path = Path(ctx._manager.home_path) / 'slot-cleanups.json'\n"
            "    def cleanup():\n"
            "        cleanups = json.loads(cleanup_path.read_text()) if cleanup_path.exists() else []\n"
            "        cleanups.append({'object_id': id(service)})\n"
            "        cleanup_path.write_text(json.dumps(cleanups))\n"
            "    slot.clear_on_unload(cleanup=cleanup)\n"
            "    ctx.register_hook('on_session_start', lambda **kw: {'object_id': id(service)})\n"
            f"    if Path({marker}).exists():\n"
            "        raise RuntimeError('fixture replacement failure')\n"
        )
    (plugin / "__init__.py").write_text(source, encoding="utf-8")
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["slot-fixture"]}}),
        encoding="utf-8",
    )
    return plugin


def _home(tmp_path: Path, monkeypatch, name: str = "home") -> Path:
    home = tmp_path / name
    home.mkdir()
    bundled = tmp_path / f"bundled-{name}"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    return home


def _rows(home: Path, filename: str):
    path = home / filename
    return json.loads(path.read_text()) if path.exists() else []


def test_h2_red_force_reload_loses_service_identity_on_h1_baseline(tmp_path, monkeypatch):
    """H2-R1: baseline discovery cannot preserve a fixture service across reload."""
    home = _home(tmp_path, monkeypatch)
    _write_slot_plugin(home)

    manager = PluginManager()
    manager.discover_and_load()
    manager.discover_and_load(force=True)

    observations = _rows(home, "slot-observations.json")
    assert len(observations) == 2
    assert observations[0]["object_id"] == observations[1]["object_id"]


def test_runtime_slot_survives_replacement_and_new_handler_reads_it(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _write_slot_plugin(home)
    manager = PluginManager()

    manager.discover_and_load()
    first = _rows(home, "slot-observations.json")[0]["object_id"]
    assert manager.invoke_hook("on_session_start") == [{"object_id": first}]
    manager.discover_and_load(force=True)

    observations = _rows(home, "slot-observations.json")
    assert observations[0]["object_id"] == observations[1]["object_id"]
    assert manager.invoke_hook("on_session_start") == [{"object_id": first}]


def test_runtime_slots_are_profile_isolated_a_b_a(tmp_path, monkeypatch):
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    for home in (home_a, home_b):
        home.mkdir()
        (tmp_path / f"bundled-{home.name}").mkdir()
        _write_slot_plugin(home)
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "bundled-home-a"))

    manager_a = PluginManager(scope_key=str(home_a.resolve()))
    manager_b = PluginManager(scope_key=str(home_b.resolve()))
    manager_a.discover_and_load()
    manager_b.discover_and_load()
    slot_a = next(iter(manager_a._runtime_slots.values()))
    slot_b = next(iter(manager_b._runtime_slots.values()))
    value_a = slot_a.get_or_create(lambda: object())
    value_b = slot_b.get_or_create(lambda: object())
    manager_a.discover_and_load(force=True)

    assert slot_a is not slot_b
    assert value_a is not value_b
    assert next(iter(manager_a._runtime_slots.values())) is slot_a
    assert slot_a.get_or_create(lambda: object()) is value_a
    assert _rows(home_a, "slot-observations.json")[0]["object_id"] == _rows(home_a, "slot-observations.json")[1]["object_id"]
    assert _rows(home_b, "slot-observations.json")[0]["object_id"] != 0


def test_runtime_slot_concurrent_factory_runs_once(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    manager = PluginManager(scope_key=str(home.resolve()))
    slot = manager._runtime_slot("slot-fixture", "concurrent")
    calls = []
    values = []

    def factory():
        calls.append(1)
        time.sleep(0.03)
        return object()

    threads = [threading.Thread(target=lambda: values.append(slot.get_or_create(factory))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert values[0] is values[1]


def test_runtime_slot_factory_failure_is_not_cached(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    manager = PluginManager(scope_key=str(home.resolve()))
    slot = manager._runtime_slot("slot-fixture", "retry")
    attempts = []

    def failing_factory():
        attempts.append(1)
        raise RuntimeError("not ready")

    try:
        slot.get_or_create(failing_factory)
    except RuntimeError:
        pass
    healthy = slot.get_or_create(lambda: object())

    assert len(attempts) == 1
    assert slot.get_or_create(lambda: object()) is healthy


def test_failed_replacement_preserves_slot_but_removes_partial_surface(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _write_slot_plugin(home, fail_marker=True)
    manager = PluginManager()
    manager.discover_and_load()
    old_slot = next(iter(manager._runtime_slots.values()))
    old_object_id = _rows(home, "slot-observations.json")[0]["object_id"]

    (home / "fail-replacement").touch()
    manager.discover_and_load(force=True)
    assert manager._plugins["slot-fixture"].enabled is False
    assert next(iter(manager._runtime_slots.values())) is old_slot
    assert manager.invoke_hook("on_session_start") == []

    (home / "fail-replacement").unlink()
    manager.discover_and_load(force=True)
    assert manager._plugins["slot-fixture"].enabled is True
    assert next(iter(manager._runtime_slots.values())) is old_slot
    assert manager.invoke_hook("on_session_start") == [{"object_id": old_object_id}]


def test_definitive_unload_cleans_once_then_fresh_load_gets_new_slot(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _write_slot_plugin(home)
    manager = PluginManager()
    manager.discover_and_load()
    old_slot = next(iter(manager._runtime_slots.values()))
    old_value = old_slot.get_or_create(lambda: object())

    assert manager.unload() is True
    assert _rows(home, "slot-cleanups.json") == [{"object_id": id(old_value)}]
    assert manager.unload() is False

    manager.discover_and_load()
    new_slot = next(iter(manager._runtime_slots.values()))
    new_value = new_slot.get_or_create(lambda: object())
    assert new_slot is not old_slot
    assert new_value is not old_value
    assert len(_rows(home, "slot-cleanups.json")) == 1


def test_plugin_without_runtime_slot_keeps_reload_behavior(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _write_slot_plugin(home, ordinary=True)
    manager = PluginManager()

    manager.discover_and_load()
    assert manager.invoke_hook("on_session_start") == [{"ordinary": True}]
    manager.discover_and_load(force=True)

    assert manager._plugins["slot-fixture"].enabled is True
    assert manager.invoke_hook("on_session_start") == [{"ordinary": True}]
    assert len(_rows(home, "slot-observations.json")) == 2
