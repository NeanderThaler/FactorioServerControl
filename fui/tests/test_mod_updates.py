import hashlib
import io
import json
import tempfile
import zipfile
from pathlib import Path
from unittest import TestCase, mock

from app import mods
from app.mods import ModSyncError, ensure_builtin_entries, game_series, update_mods


def _mod_zip(name: str, version: str, game: str, dependencies: list[str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"{name}_{version}/info.json",
            json.dumps(
                {
                    "name": name,
                    "version": version,
                    "factorio_version": game,
                    "dependencies": dependencies or ["base >= " + game],
                }
            ),
        )
    return buffer.getvalue()


class FakePortal:
    """Serves /api/mods/<name>/full metadata and downloads from memory."""

    def __init__(self, releases: dict[str, list[tuple[str, str, list[str]]]]) -> None:
        self.files: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, object]] = {}
        for name, versions in releases.items():
            entries = []
            for version, game, dependencies in versions:
                data = _mod_zip(name, version, game, dependencies)
                path = f"/download/{name}/{version}"
                self.files[path] = data
                entries.append(
                    {
                        "version": version,
                        "file_name": f"{name}_{version}.zip",
                        "download_url": path,
                        "sha1": hashlib.sha1(data).hexdigest(),
                        "info_json": {"factorio_version": game, "dependencies": dependencies},
                    }
                )
            self.metadata[name] = {"name": name, "releases": entries}
        self.downloads: list[str] = []

    def urlopen(self, request, timeout=None):
        url = request.full_url
        if url.startswith("https://mods.factorio.com/api/mods/"):
            name = url.split("/api/mods/")[1].rsplit("/full", 1)[0]
            if name not in self.metadata:
                raise OSError("404")
            return io.BytesIO(json.dumps(self.metadata[name]).encode())
        path = url.split("mods.factorio.com", 1)[1].split("?", 1)[0]
        self.downloads.append(path)
        return io.BytesIO(self.files[path])


class UpdateModsTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.mods_dir = self.state / "mods"
        self.mods_dir.mkdir()
        self.settings = self.state / "server-settings.json"
        self.settings.write_text('{"username": "u", "token": "t"}', encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _install(self, name: str, version: str, game: str, dependencies=None) -> None:
        (self.mods_dir / f"{name}_{version}.zip").write_bytes(
            _mod_zip(name, version, game, dependencies)
        )

    def _mod_list(self, entries: list[tuple[str, bool]]) -> None:
        (self.mods_dir / "mod-list.json").write_text(
            json.dumps({"mods": [{"name": n, "enabled": e} for n, e in entries]}),
            encoding="utf-8",
        )

    def _run(self, portal: FakePortal, **kwargs):
        with mock.patch.object(mods.urllib.request, "urlopen", portal.urlopen):
            return update_mods(self.mods_dir, self.settings, "2.1.23", **kwargs)

    def test_game_series(self) -> None:
        self.assertEqual(game_series("2.1.23"), "2.1")
        with self.assertRaises(ModSyncError):
            game_series("stable")

    def test_updates_to_newest_release_for_the_engine_series(self) -> None:
        self._install("flib", "0.16.5", "2.0")
        self._mod_list([("base", True), ("flib", True)])
        portal = FakePortal({
            "flib": [("0.16.5", "2.0", []), ("0.17.2", "2.1", []), ("0.18.0", "2.2", [])],
        })

        result = self._run(portal)

        self.assertEqual(result["updated"], [{"name": "flib", "from": "0.16.5", "to": "0.17.2"}])
        self.assertTrue((self.mods_dir / "flib_0.17.2.zip").exists())
        self.assertFalse((self.mods_dir / "flib_0.16.5.zip").exists())
        backup = Path(result["backup"])
        self.assertEqual(backup.parent, self.state / "mods-backup")
        self.assertTrue((backup / "flib_0.16.5.zip").exists())
        self.assertTrue((backup / "mod-list.json").exists())
        listed = json.loads((self.mods_dir / "mod-list.json").read_text(encoding="utf-8"))["mods"]
        self.assertIn({"name": "recycler", "enabled": False}, listed)

    def test_adds_missing_required_dependencies_and_skips_optional(self) -> None:
        self._install("K2", "2.0.19", "2.0")
        self._mod_list([("base", True), ("K2", True)])
        portal = FakePortal({
            "K2": [("2.1.4", "2.1", ["base >= 2.1.22", "+ Lane >= 1.3.0", "? Optional", "(?) Hidden", "! Bad"])],
            "Lane": [("1.3.0", "2.1", ["base >= 2.1.0"])],
        })

        result = self._run(portal)

        self.assertEqual(result["added"], ["Lane"])
        self.assertEqual(sorted(portal.downloads), ["/download/K2/2.1.4", "/download/Lane/1.3.0"])
        listed = json.loads((self.mods_dir / "mod-list.json").read_text(encoding="utf-8"))["mods"]
        self.assertIn({"name": "Lane", "enabled": True}, listed)

    def test_only_incompatible_keeps_mods_for_the_same_series(self) -> None:
        self._install("flib", "0.17.0", "2.1")
        self._install("old", "1.0.0", "2.0")
        self._mod_list([("base", True), ("flib", True), ("old", True)])
        portal = FakePortal({
            "flib": [("0.17.2", "2.1", [])],
            "old": [("1.0.0", "2.0", []), ("1.1.0", "2.1", [])],
        })

        result = self._run(portal, only_incompatible=True)

        self.assertEqual(result["updated"], [{"name": "old", "from": "1.0.0", "to": "1.1.0"}])
        self.assertEqual(result["unchanged"], [{"name": "flib", "version": "0.17.0"}])

    def test_refuses_when_a_mod_has_no_release_for_the_series(self) -> None:
        self._install("abandoned", "1.0.0", "2.0")
        self._mod_list([("base", True), ("abandoned", True)])
        before = (self.mods_dir / "mod-list.json").read_bytes()
        portal = FakePortal({"abandoned": [("1.0.0", "2.0", [])]})

        with self.assertRaisesRegex(ModSyncError, "abandoned"):
            self._run(portal)

        self.assertEqual((self.mods_dir / "mod-list.json").read_bytes(), before)
        self.assertTrue((self.mods_dir / "abandoned_1.0.0.zip").exists())

    def test_up_to_date_mods_need_no_credentials_or_downloads(self) -> None:
        self._install("flib", "0.17.2", "2.1")
        self._mod_list([
            ("base", True), ("elevated-rails", False), ("quality", False),
            ("recycler", False), ("space-age", False), ("flib", True),
        ])
        self.settings.write_text("{}", encoding="utf-8")
        portal = FakePortal({"flib": [("0.17.2", "2.1", [])]})

        result = self._run(portal)

        self.assertEqual(result["updated"], [])
        self.assertIsNone(result["backup"])
        self.assertEqual(portal.downloads, [])


class BuiltinEntryTests(TestCase):
    def test_lists_unlisted_builtins_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            mods_dir = Path(temp)
            (mods_dir / "mod-list.json").write_text(
                '{"mods":[{"name":"base","enabled":true},{"name":"quality","enabled":false},'
                '{"name":"K2","enabled":true}]}',
                encoding="utf-8",
            )
            self.assertTrue(ensure_builtin_entries(mods_dir))
            self.assertFalse(ensure_builtin_entries(mods_dir))
            listed = json.loads((mods_dir / "mod-list.json").read_text(encoding="utf-8"))["mods"]

        self.assertEqual(
            listed,
            [
                {"name": "base", "enabled": True},
                {"name": "elevated-rails", "enabled": False},
                {"name": "quality", "enabled": False},
                {"name": "recycler", "enabled": False},
                {"name": "space-age", "enabled": False},
                {"name": "K2", "enabled": True},
            ],
        )
