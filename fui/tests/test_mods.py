import io
import struct
import tempfile
import zipfile
from pathlib import Path
from unittest import TestCase

from app.mods import (
    ModVersion,
    SaveMod,
    _desired_mod_list,
    read_mod_inventory,
    read_save_mods,
)


def _optimized(value: int, size: int) -> bytes:
    return bytes([value]) if value < 0xFF else b"\xff" + value.to_bytes(size, "little")


def _string(value: str) -> bytes:
    encoded = value.encode()
    return _optimized(len(encoded), 4) + encoded


class SaveModParserTests(TestCase):
    def test_reads_factorio_2_mod_names_and_exact_versions(self) -> None:
        game = (2, 0, 66, 0)
        header = io.BytesIO()
        header.write(struct.pack("<4H", *game))
        header.write(b"\x00")
        header.write(_string("freeplay"))
        header.write(_string("level"))
        header.write(_string("base"))
        header.write(b"\x00\x00\x00")
        header.write(_string(""))
        header.write(b"\x00\x00\x00\x01")
        header.write(_optimized(2, 2) + _optimized(0, 2) + _optimized(66, 2))
        header.write(struct.pack("<HB", 12345, 1))
        header.write(b"\x00\x01\x00\x00\xa0\x00")
        header.write(_optimized(2, 4))
        for name, version in (("base", (2, 0, 66)), ("Krastorio2", (1, 3, 25))):
            header.write(_string(name))
            for part in version:
                header.write(_optimized(part, 2))
            header.write(struct.pack("<I", 0))

        with tempfile.TemporaryDirectory() as temp:
            save = Path(temp) / "save.zip"
            with zipfile.ZipFile(save, "w") as archive:
                archive.writestr("save/level-init.dat", b"not the saved world")
                archive.writestr("save/level.dat", header.getvalue())

            self.assertEqual(
                read_save_mods(save),
                [
                    SaveMod("base", ModVersion(2, 0, 66)),
                    SaveMod("Krastorio2", ModVersion(1, 3, 25)),
                ],
            )

    def test_mod_list_disables_dlc_mods_not_required_by_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            mods_dir = Path(temp)
            (mods_dir / "mod-list.json").write_text(
                '{"mods":[{"name":"base","enabled":false},'
                '{"name":"quality","enabled":true},'
                '{"name":"space-age","enabled":true}]}',
                encoding="utf-8",
            )
            result = _desired_mod_list(
                [SaveMod("nullius", ModVersion(1, 9, 6))], mods_dir
            )

        self.assertEqual(
            result,
            {
                "mods": [
                    {"name": "base", "enabled": True},
                    {"name": "elevated-rails", "enabled": False},
                    {"name": "quality", "enabled": False},
                    {"name": "recycler", "enabled": False},
                    {"name": "space-age", "enabled": False},
                    {"name": "nullius", "enabled": True},
                ]
            },
        )

    def test_mod_list_enables_recycler_with_quality(self) -> None:
        result = _desired_mod_list(
            [SaveMod("quality", ModVersion(2, 0, 77))], Path(".")
        )
        enabled = {entry["name"]: entry["enabled"] for entry in result["mods"]}
        self.assertTrue(enabled["recycler"])
        self.assertFalse(enabled["space-age"])

    def test_reads_configured_mods_and_archive_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            mods_dir = Path(temp)
            (mods_dir / "mod-list.json").write_text(
                '{"mods":['
                '{"name":"base","enabled":true},'
                '{"name":"Krastorio2","enabled":true},'
                '{"name":"optional-mod","enabled":false}'
                ']}',
                encoding="utf-8",
            )
            with zipfile.ZipFile(mods_dir / "Krastorio2_1.3.25.zip", "w") as archive:
                archive.writestr(
                    "Krastorio2_1.3.25/info.json",
                    '{"name":"Krastorio2","version":"1.3.25"}',
                )

            result = read_mod_inventory(mods_dir)

        self.assertEqual(
            result,
            [
                {"name": "base", "enabled": True, "version": "Built-in", "installed": True},
                {"name": "Krastorio2", "enabled": True, "version": "1.3.25", "installed": True},
                {"name": "optional-mod", "enabled": False, "version": None, "installed": False},
            ],
        )