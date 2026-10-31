from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from app.logs import clean_log_line, read_clean_logs


class CleanLogTests(TestCase):
    def test_formats_supported_events(self) -> None:
        cases = {
            "   0.000 2026-08-09 09:57:08; Factorio 2.0.77 (build 84539, linux64, headless, space-age)":
                "2026-08-09 09:57:08  Server started (Factorio 2.0.77)",
            "2026-08-09 10:00:00 [JOIN] Alice joined the game":
                "2026-08-09 10:00:00  Alice joined",
            "2026-08-09 10:03:00 [LEAVE] Alice left the game":
                "2026-08-09 10:03:00  Alice left",
            "2026-08-09 10:01:00 [CHAT] Alice: Hello world":
                "2026-08-09 10:01:00  Alice: Hello world",
            "2026-08-09 10:02:00 [CHAT] <server>: Alice was killed by Small biter.":
                "2026-08-09 10:02:00  Alice died: was killed by Small biter",
        }

        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(clean_log_line(raw), expected)

    def test_uses_journal_time_for_shutdown(self) -> None:
        timestamp = 1_786_263_845_000_000
        expected = datetime.fromtimestamp(timestamp / 1_000_000).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(
            clean_log_line(" 638.177 Goodbye", timestamp),
            f"{expected}  Server stopped",
        )

    def test_ignores_engine_noise_and_non_death_server_chat(self) -> None:
        self.assertIsNone(clean_log_line(" 0.686 Info ServerMultiplayerManager.cpp: InGame"))
        self.assertIsNone(
            clean_log_line("2026-08-09 10:02:00 [CHAT] <server>: Saving map")
        )

    def test_player_chat_cannot_impersonate_an_event(self) -> None:
        self.assertEqual(
            clean_log_line("2026-08-09 10:01:00 [CHAT] Alice: [JOIN] Bob joined the game"),
            "2026-08-09 10:01:00  Alice: [JOIN] Bob joined the game",
        )

    @patch("app.logs.read_journal_json")
    def test_reads_only_clean_events_from_requested_run(self, read_journal_json) -> None:
        read_journal_json.return_value = (
            [
                {"MESSAGE": "2026-08-09 10:00:00 [JOIN] Alice joined the game"},
                {"MESSAGE": " 1.000 Info ServerMultiplayerManager.cpp: InGame"},
            ],
            100,
        )

        settings = object()
        result = read_clean_logs(settings, 100, current_run_only=False)

        self.assertEqual(result, ["2026-08-09 10:00:00  Alice joined"])
        read_journal_json.assert_called_once_with(
            settings,
            r"\[JOIN\]|\[LEAVE\]|\[CHAT\]|; Factorio |Goodbye",
            100,
            current_run_only=False,
        )