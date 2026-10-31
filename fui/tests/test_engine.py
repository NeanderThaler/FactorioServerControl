import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from app import engine
from app.engine import EngineError, engine_status, previous_link, prune_versions, switch_to


def _fake_install(path: Path, version: str) -> Path:
    (path / "data" / "base").mkdir(parents=True)
    (path / "data" / "base" / "info.json").write_text(
        json.dumps({"name": "base", "version": version}), encoding="utf-8"
    )
    (path / "bin" / "x64").mkdir(parents=True)
    (path / "bin" / "x64" / "factorio").write_text("", encoding="utf-8")
    return path


class EngineLayoutTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.install = self.root / "factorio"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_migrates_a_plain_directory_and_keeps_it_for_rollback(self) -> None:
        _fake_install(self.install, "2.0.77")
        new = _fake_install(self.root / "factorio-2.1.23", "2.1.23")

        switch_to(new, self.install)

        self.assertTrue(self.install.is_symlink())
        self.assertEqual(engine_status(self.install), {"installed": "2.1.23", "previous": "2.0.77"})
        self.assertTrue((self.root / "factorio-2.0.77" / "bin" / "x64" / "factorio").exists())

    def test_rollback_swaps_current_and_previous(self) -> None:
        old = _fake_install(self.root / "factorio-2.0.77", "2.0.77")
        new = _fake_install(self.root / "factorio-2.1.23", "2.1.23")
        switch_to(old, self.install)
        switch_to(new, self.install)

        switch_to(previous_link(self.install).resolve(), self.install)

        self.assertEqual(engine_status(self.install), {"installed": "2.0.77", "previous": "2.1.23"})

    def test_prunes_versions_other_than_current_and_previous(self) -> None:
        versions = ["2.0.76", "2.0.77", "2.1.23"]
        for version in versions:
            _fake_install(self.root / f"factorio-{version}", version)
            switch_to(self.root / f"factorio-{version}", self.install)
        (self.root / "factorio-notes").mkdir()

        self.assertEqual(prune_versions(self.install), ["2.0.76"])
        self.assertTrue((self.root / "factorio-notes").exists())
        self.assertEqual(engine_status(self.install), {"installed": "2.1.23", "previous": "2.0.77"})

    def test_status_without_installation(self) -> None:
        self.assertEqual(engine_status(self.install), {"installed": None, "previous": None})


class ChangeEngineTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.install = self.root / "factorio"
        _fake_install(self.root / "factorio-2.0.77", "2.0.77")
        switch_to(self.root / "factorio-2.0.77", self.install)
        self.mods_dir = self.root / "state" / "mods"
        self.mods_dir.mkdir(parents=True)
        (self.mods_dir / "mod-list.json").write_text('{"mods":[{"name":"base","enabled":true}]}')
        self.instances = [{
            "id": "default",
            "service_name": "factorio@default.service",
            "mods_dir": str(self.mods_dir),
            "server_settings_path": str(self.root / "server-settings.json"),
            "mod_sync_enabled": True,
        }]
        self.calls: list[tuple[str, ...]] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _systemctl(self, active: bool):
        def run(systemctl, *arguments):
            self.calls.append(arguments)
            code = 0 if arguments[0] != "is-active" or active else 3
            return mock.Mock(returncode=code, stderr="")
        return run

    def _change(self, action: str, latest: str = "2.1.23", active: bool = True, update=None):
        def install(version, install_dir, cache_dir, timeout=120):
            return _fake_install(self.root / f"factorio-{version}", version)

        with mock.patch.object(engine, "latest_stable", return_value=latest), \
                mock.patch.object(engine, "install_version", side_effect=install), \
                mock.patch.object(engine, "_systemctl", side_effect=self._systemctl(active)), \
                mock.patch.object(engine, "_chown_mods"), \
                mock.patch.object(engine, "update_mods", side_effect=update or (lambda *a, **k: {"updated": []})):
            return engine.change_engine(
                action, self.instances, self.install, self.root / "cache", "systemctl", "f:f"
            )

    def test_upgrade_stops_switches_updates_and_restarts(self) -> None:
        result = self._change("upgrade")

        self.assertEqual((result["from"], result["to"]), ("2.0.77", "2.1.23"))
        self.assertEqual(engine_status(self.install)["installed"], "2.1.23")
        self.assertIn(("stop", "factorio@default.service"), self.calls)
        self.assertEqual(self.calls[-1], ("start", "factorio@default.service"))
        self.assertTrue(result["instances"][0]["started"])

    def test_upgrade_is_a_no_op_when_current(self) -> None:
        result = self._change("upgrade", latest="2.0.77")
        self.assertFalse(result["changed"])
        self.assertEqual(self.calls, [])

    def test_upgrade_refuses_to_downgrade(self) -> None:
        with self.assertRaises(EngineError):
            self._change("upgrade", latest="2.0.70")

    def test_failed_mod_update_leaves_that_server_stopped(self) -> None:
        def fail(*args, **kwargs):
            raise engine.ModSyncError("No Factorio 2.1 release on the mod portal for: x")

        result = self._change("upgrade", update=fail)

        entry = result["instances"][0]
        self.assertFalse(entry["started"])
        self.assertIn("x", entry["error"])
        self.assertNotIn(("start", "factorio@default.service"), self.calls)

    def test_rollback_requires_a_previous_version(self) -> None:
        with self.assertRaises(EngineError):
            self._change("rollback")

    def test_rollback_returns_to_previous(self) -> None:
        self._change("upgrade")
        result = self._change("rollback")
        self.assertEqual(result["to"], "2.0.77")
        self.assertEqual(engine_status(self.install), {"installed": "2.0.77", "previous": "2.1.23"})
