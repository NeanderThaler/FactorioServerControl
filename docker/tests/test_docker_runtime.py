import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).parents[2]


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def check_shell(path: Path) -> None:
    if os.name == "nt":
        drive, remainder = os.path.splitdrive(str(path.resolve()))
        linux_path = f"/mnt/{drive[0].lower()}/{remainder.lstrip('\\').replace('\\', '/')}"
        subprocess.run(["wsl", "sh", "-n", linux_path], check=True)
    else:
        subprocess.run(["sh", "-n", str(path)], check=True)


class DockerAdapterTests(TestCase):
    def setUp(self) -> None:
        self.adapter = load_module("docker_adapter", "docker/docker-adapter.py")

    def test_rejects_unconfigured_service_names(self) -> None:
        with self.assertRaises(SystemExit):
            self.adapter.container_for("factorio@default.service;docker-rm")

    def test_translates_running_container_status(self) -> None:
        inspected = {
            "Id": "abc123",
            "RestartCount": 2,
            "State": {
                "Running": True,
                "Pid": 42,
                "StartedAt": "2026-08-09T10:00:00Z",
            },
        }
        output = io.StringIO()
        with (
            patch.object(self.adapter, "inspect", return_value=inspected),
            patch.object(self.adapter.dt, "datetime", wraps=self.adapter.dt.datetime) as clock,
            contextlib.redirect_stdout(output),
        ):
            clock.now.return_value = self.adapter.dt.datetime(
                2026, 8, 9, 10, 1, tzinfo=self.adapter.dt.timezone.utc
            )
            result = self.adapter.systemctl(
                [
                    "show",
                    "factorio@default.service",
                    "--property=ActiveState,SubState,MainPID,NRestarts",
                ]
            )

        self.assertEqual(result, 0)
        self.assertIn("ActiveState=active", output.getvalue())
        self.assertIn("SubState=running", output.getvalue())
        self.assertIn("MainPID=42", output.getvalue())
        self.assertIn("NRestarts=2", output.getvalue())

    def test_emits_journal_json_expected_by_fui(self) -> None:
        records = [("2026-08-09T10:00:00Z", "Player [JOIN] joined the game")]
        output = io.StringIO()
        with (
            patch.object(self.adapter, "timestamped_logs", return_value=records),
            contextlib.redirect_stdout(output),
        ):
            result = self.adapter.journalctl(
                [
                    "--unit",
                    "factorio@default.service",
                    "--lines",
                    "20",
                    "--output=json",
                    "--grep",
                    "JOIN",
                ]
            )

        self.assertEqual(result, 0)
        record = json.loads(output.getvalue())
        self.assertEqual(record["MESSAGE"], records[0][1])
        self.assertGreater(int(record["__REALTIME_TIMESTAMP"]), 0)

    def test_filters_persistent_logs_before_applying_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "instances" / "default" / "state"
            state.mkdir(parents=True)
            (state / "factorio-history.log").write_text(
                "2026-08-09 10:00:00 [JOIN] Ada joined the game\n"
                + "routine engine line\n" * 100,
                encoding="utf-8",
            )
            with patch.object(self.adapter, "DATA_ROOT", Path(temp)):
                records = self.adapter.timestamped_logs(
                    "default", 1, False, self.adapter.re.compile("JOIN")
                )

        self.assertEqual(len(records), 1)
        self.assertIn("Ada joined", records[0][1])

    def test_derives_shutdown_timestamp_from_run_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "instances" / "default" / "state"
            state.mkdir(parents=True)
            (state / "factorio-history.log").write_text(
                "   0.000 2026-08-09 10:00:00; Factorio 2.0.77\n"
                " 600.000 Goodbye\n",
                encoding="utf-8",
            )
            pattern = self.adapter.re.compile(r"; Factorio |Goodbye")
            with patch.object(self.adapter, "DATA_ROOT", Path(temp)):
                records = self.adapter.timestamped_logs("default", 20, False, pattern)

        self.assertEqual(records[1][0], "2026-08-09T10:10:00Z")


class FuiEntrypointTests(TestCase):
    def test_generates_registry_and_valid_shell_helpers(self) -> None:
        entrypoint = load_module("fui_entrypoint", "docker/fui-entrypoint.py")
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            for instance_id, _, _, _, _ in entrypoint.INSTANCES:
                config = data / "instances" / instance_id / "config"
                config.mkdir(parents=True)
                (config / "server-settings.json").write_text("{}", encoding="utf-8")
            upload_save = data / "instances" / "upload1" / "state" / "saves" / "world.zip"
            upload_save.parent.mkdir(parents=True)
            upload_save.write_bytes(b"save")

            with (
                patch.object(entrypoint, "DATA", data),
                patch.object(entrypoint, "FUI", data / "fui"),
                patch.object(entrypoint, "HELPERS", data / "runtime-helpers"),
                patch.object(entrypoint.os, "execvp", side_effect=RuntimeError("stop")),
                patch.object(entrypoint.subprocess, "run") as run,
                patch.dict(
                    os.environ,
                    {"FUI_PASSWORD": "docker-test-password", "FUI_SECRET_KEY": "x" * 64},
                    clear=False,
                ),
                self.assertRaisesRegex(RuntimeError, "stop"),
            ):
                entrypoint.main()

            registry = json.loads((data / "fui" / "instances.json").read_text())
            self.assertEqual([item["id"] for item in registry], ["default", "upload1", "upload2"])
            run.assert_called_once_with(
                [entrypoint.SYSTEMCTL, "start", "factorio@upload1.service"],
                check=False,
            )
            for item in registry:
                helper_dir = data / "runtime-helpers" / item["id"]
                self.assertEqual(Path(item["mod_sync_script_path"]).parent, helper_dir)
                for name in ("docker-sync-mods.sh", "factorio-delete-upload.sh", "docker-reset-map.sh"):
                    helper = helper_dir / name
                    if os.name != "nt":
                        self.assertTrue(helper.stat().st_mode & 0o100)
                    check_shell(helper)
