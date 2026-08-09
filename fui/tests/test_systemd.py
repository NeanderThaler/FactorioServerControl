import subprocess
from unittest import TestCase
from unittest.mock import patch

from app.systemd import ServiceError, delete_upload


class UploadDeletionTests(TestCase):
    @patch("app.systemd._run")
    def test_uses_fixed_noninteractive_helper(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")

        delete_upload("/srv/factorio/upload1/delete.sh", "/usr/bin/sudo", 180)

        run.assert_called_once_with(
            [
                "/usr/bin/sudo",
                "--non-interactive",
                "/srv/factorio/upload1/delete.sh",
            ],
            180,
        )

    @patch("app.systemd._run")
    def test_reports_helper_failure(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 1, "", "stop failed")

        with self.assertRaisesRegex(ServiceError, "stop failed"):
            delete_upload("/srv/factorio/upload1/delete.sh", "/usr/bin/sudo", 180)