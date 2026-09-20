"""RX9 H2 proof that the production GatewayRunner shutdown reaches definitive slot cleanup."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml

import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import PluginManager
from tests.gateway.restart_test_helpers import make_restart_runner


def test_gateway_runner_stop_definitively_cleans_runtime_slot(tmp_path, monkeypatch):
    asyncio.run(_run_gateway_shutdown_test(tmp_path, monkeypatch))


async def _run_gateway_shutdown_test(tmp_path, monkeypatch):
    home = tmp_path / "gateway-home"
    plugin = home / "plugins" / "shutdown-fixture"
    plugin.mkdir(parents=True)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (plugin / "plugin.yaml").write_text("name: shutdown-fixture\nversion: 0.1.0\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "\n"
        "def register(ctx):\n"
        "    slot = ctx.runtime_slot('lease')\n"
        "    lease = slot.get_or_create(lambda: object())\n"
        "    cleanup_path = Path(ctx._manager.home_path) / 'cleanup.json'\n"
        "    def cleanup():\n"
        "        rows = json.loads(cleanup_path.read_text()) if cleanup_path.exists() else []\n"
        "        rows.append({'lease_id': id(lease)})\n"
        "        cleanup_path.write_text(json.dumps(rows))\n"
        "    slot.clear_on_unload(cleanup=cleanup)\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["shutdown-fixture"]}}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))

    manager = PluginManager()
    manager.discover_and_load()
    assert manager._runtime_slots
    old_manager = plugins_mod._plugin_manager
    plugins_mod._plugin_manager = manager
    try:
        runner, _adapter = make_restart_runner()
        with monkeypatch.context() as patcher:
            patcher.setattr("gateway.status.remove_pid_file", lambda: None)
            patcher.setattr("gateway.status.write_runtime_status", lambda *args, **kwargs: None)
            await runner.stop()
            await runner.stop()
    finally:
        plugins_mod._plugin_manager = old_manager

    rows = json.loads((home / "cleanup.json").read_text())
    assert len(rows) == 1
    assert manager._runtime_slots == {}
