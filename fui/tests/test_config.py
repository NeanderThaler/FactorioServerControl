import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.config import _load_instances


class InstanceConfigTests(TestCase):
    def test_loads_allowlisted_instances(self) -> None:
        instances = [
            {
                "id": "krastorio",
                "name": "Krastorio",
                "service_name": "factorio@krastorio.service",
                "reset_script_path": "/etc/factorio/krastorio/reset.sh",
                "mod_sync_script_path": "/etc/factorio/krastorio/sync-mods.sh",
                "mod_sync_enabled": False,
                "mods_dir": "/var/lib/factorio-krastorio/mods",
                "upload_slot": False,
                "saves_dir": "/var/lib/factorio-krastorio/saves",
                "server_settings_path": "/etc/factorio-krastorio/server-settings.json",
                "port": 34199,
                "reset_enabled": True,
            }
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "instances.json"
            path.write_text(json.dumps(instances), encoding="utf-8")
            with patch.dict(os.environ, {"FUI_INSTANCES_FILE": str(path)}):
                loaded = _load_instances()

        self.assertEqual(loaded[0].id, "krastorio")
        self.assertEqual(loaded[0].service_name, "factorio@krastorio.service")
        self.assertFalse(loaded[0].mod_sync_enabled)

    def test_rejects_unit_injection(self) -> None:
        instances = [
            {
                "id": "bad",
                "name": "Bad",
                "service_name": "factorio.service --all",
                "reset_script_path": "/reset",
                "mod_sync_script_path": "/sync",
                "mods_dir": "/mods",
                "upload_slot": False,
                "saves_dir": "/saves",
                "server_settings_path": "/settings.json",
                "port": 34199,
                "reset_enabled": True,
            }
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "instances.json"
            path.write_text(json.dumps(instances), encoding="utf-8")
            with (
                patch.dict(os.environ, {"FUI_INSTANCES_FILE": str(path)}),
                self.assertRaises(RuntimeError),
            ):
                _load_instances()