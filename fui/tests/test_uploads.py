import io
import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.uploads import SaveUploadError, install_save, update_server_settings


class SaveUploadTests(TestCase):
    @patch("app.uploads.read_save_mods", return_value=[])
    def test_installs_validated_save(self, read_save_mods) -> None:
        with tempfile.TemporaryDirectory() as temp:
            saves_dir = Path(temp)
            target = install_save(io.BytesIO(b"factorio-save"), saves_dir)

            self.assertEqual(target.name, "uploaded.zip")
            self.assertEqual(target.read_bytes(), b"factorio-save")
            read_save_mods.assert_called_once()

    @patch("app.uploads.read_save_mods", return_value=[])
    def test_rejects_used_slot(self, _read_save_mods) -> None:
        with tempfile.TemporaryDirectory() as temp:
            saves_dir = Path(temp)
            (saves_dir / "existing.zip").write_bytes(b"save")

            with self.assertRaisesRegex(SaveUploadError, "already in use"):
                install_save(io.BytesIO(b"new"), saves_dir)

    def test_updates_name_and_public_visibility_without_losing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "server-settings.json"
            path.write_text(
                json.dumps(
                    {
                        "name": "Slot",
                        "username": "portal-user",
                        "token": "portal-token",
                        "visibility": {"public": False, "lan": False},
                    }
                ),
                encoding="utf-8",
            )

            original = update_server_settings(
                path, "Uploaded World", "A friendly rail world", True
            )
            updated = json.loads(path.read_text(encoding="utf-8"))

        self.assertIn(b'"name": "Slot"', original)
        self.assertEqual(updated["name"], "Uploaded World")
        self.assertEqual(updated["description"], "A friendly rail world")
        self.assertEqual(updated["username"], "portal-user")
        self.assertEqual(updated["token"], "portal-token")
        self.assertEqual(updated["visibility"], {"public": True, "lan": True})

    def test_rejects_empty_server_description(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "server-settings.json"
            original = '{"name": "Slot"}\n'
            path.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(SaveUploadError, "description"):
                update_server_settings(path, "Uploaded World", "   ", True)

            self.assertEqual(path.read_text(encoding="utf-8"), original)