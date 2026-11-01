from unittest import TestCase
from unittest.mock import patch

from app.players import read_players


class PlayerResetRetentionTests(TestCase):
    def test_sessions_and_time_continue_across_map_reset(self) -> None:
        entries = [
            {
                "__REALTIME_TIMESTAMP": 1_000_000_000,
                "MESSAGE": "2026-08-02 10:00:00 [JOIN] Alice joined the game",
            },
            {
                "__REALTIME_TIMESTAMP": 1_600_000_000,
                "MESSAGE": " 600.000 Goodbye",
            },
            {
                "__REALTIME_TIMESTAMP": 2_000_000_000,
                "MESSAGE": "   0.000 2026-08-02 10:20:00; Factorio 2.0.0",
            },
            {
                "__REALTIME_TIMESTAMP": 2_100_000_000,
                "MESSAGE": "2026-08-02 10:21:40 [JOIN] Alice joined the game",
            },
        ]

        with (
            patch("app.players.read_journal_json", return_value=(entries, 20_000)),
            patch("app.players.read_status", return_value={"running": True}),
            patch("app.players.time.time", return_value=2_400.0),
        ):
            result = read_players(object())

        self.assertEqual(result["online_count"], 1)
        self.assertEqual(result["last_join"], {"name": "Alice", "at": 2_100.0})
        self.assertEqual(
            result["players"][0],
            {
                "name": "Alice",
                "online": True,
                "sessions": 2,
                "total_seconds": 900.0,
                "current_seconds": 300.0,
                "online_since": 2_100.0,
                "last_seen": 2_400.0,
            },
        )


class PlayerDroppedConnectionTests(TestCase):
    def test_peer_removal_without_leave_ends_session(self) -> None:
        entries = [
            {
                "__REALTIME_TIMESTAMP": 1_000_000_000,
                "MESSAGE": "49080.797 Info InputActionHandler.cpp:5065: UpdateTick (2546840) "
                "processed PlayerJoinGame peerID(18) playerIndex(16) mode(create)",
            },
            {
                "__REALTIME_TIMESTAMP": 1_000_000_000,
                "MESSAGE": "2026-11-01 09:07:27 [JOIN] vectoroc joined the game",
            },
            {
                "__REALTIME_TIMESTAMP": 1_100_000_000,
                "MESSAGE": "49100.000 Info InputActionHandler.cpp:5065: UpdateTick (2546900) "
                "processed PlayerJoinGame peerID(19) playerIndex(8) mode(connect)",
            },
            {
                "__REALTIME_TIMESTAMP": 1_100_000_000,
                "MESSAGE": "2026-11-01 09:09:07 [JOIN] Alice joined the game",
            },
            {
                "__REALTIME_TIMESTAMP": 1_138_000_000,
                "MESSAGE": "49218.913 Info ServerMultiplayerManager.cpp:1085: "
                "Disconnect notification for peer (18)",
            },
            {
                "__REALTIME_TIMESTAMP": 1_138_000_000,
                "MESSAGE": "49218.913 Info ServerSynchronizer.cpp:613: "
                "nextHeartbeatSequenceNumber(2951408) removing peer(18).",
            },
        ]

        with (
            patch("app.players.read_journal_json", return_value=(entries, 20_000)),
            patch("app.players.read_status", return_value={"running": True}),
            patch("app.players.time.time", return_value=1_200.0),
        ):
            result = read_players(object())

        self.assertEqual(result["online_count"], 1)
        players = {player["name"]: player for player in result["players"]}
        self.assertTrue(players["Alice"]["online"])
        self.assertFalse(players["vectoroc"]["online"])
        self.assertEqual(players["vectoroc"]["total_seconds"], 138.0)
        self.assertEqual(players["vectoroc"]["last_seen"], 1_138.0)