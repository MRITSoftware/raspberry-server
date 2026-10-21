import subprocess
import unittest
from unittest.mock import Mock, patch

from wifi_recovery import WifiRecovery


def result(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


class WifiRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("wifi_recovery.time.monotonic", return_value=1000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.station = patch("wifi_recovery.subprocess.run", return_value=result())
        self.run = self.station.start()
        self.addCleanup(self.station.stop)
        self.nmcli = Mock(side_effect=[
            result("saved-uuid:802-11-wireless\n"),
            result("wlan0\ninfrastructure\n"),
            result(),
        ])
        self.internet = Mock(return_value=True)
        self.stop = Mock()
        self.start = Mock()
        self.recovery = WifiRecovery(self.nmcli, self.internet, self.stop, self.start, Mock())
        self.now.return_value = 1300

    def test_waits_five_minutes_without_querying_or_stopping(self):
        self.now.return_value = 1299
        self.assertFalse(self.recovery.try_reconnect({}))
        self.run.assert_not_called()
        self.stop.assert_not_called()

    def test_associated_client_preserves_hotspot_until_disconnected(self):
        self.run.return_value = result("Station aa:bb:cc:dd:ee:ff (on wlan0)\n")
        self.assertFalse(self.recovery.try_reconnect({}))
        self.stop.assert_not_called()
        self.nmcli.assert_not_called()
        self.run.return_value = result()
        self.assertTrue(self.recovery.try_reconnect({}))
        self.stop.assert_called_once()

    def test_client_check_errors_preserve_hotspot(self):
        for failure in (result(code=1), FileNotFoundError(), subprocess.TimeoutExpired("iw", 10)):
            with self.subTest(failure=failure):
                self.run.side_effect = failure if isinstance(failure, Exception) else None
                self.run.return_value = failure
                self.assertFalse(self.recovery.try_reconnect({}))
                self.stop.assert_not_called()

    def test_client_arriving_during_profile_lookup_preserves_hotspot(self):
        self.run.side_effect = [result(), result("Station aa:bb:cc:dd:ee:ff\n")]
        self.assertFalse(self.recovery.try_reconnect({}))
        self.stop.assert_not_called()

    def test_success_uses_saved_profile_without_deleting_credentials(self):
        self.assertTrue(self.recovery.try_reconnect({}))
        self.nmcli.assert_called_with(
            "connection", "up", "uuid", "saved-uuid", "ifname", "wlan0", sudo=True, timeout=40,
        )
        self.internet.assert_called_once()
        self.start.assert_not_called()

    def test_connected_wifi_without_internet_restores_hotspot_and_waits(self):
        self.internet.return_value = False
        self.assertFalse(self.recovery.try_reconnect({}))
        self.start.assert_called_once()
        self.now.return_value = 1599
        self.assertFalse(self.recovery.try_reconnect({}))
        self.stop.assert_called_once()

    def test_profile_timeout_continues_to_next_saved_network(self):
        self.nmcli.side_effect = [
            result("first:802-11-wireless\nsecond:802-11-wireless\n"),
            result("\ninfrastructure\n"), result("wlan0\ninfrastructure\n"),
            subprocess.TimeoutExpired("nmcli", 40), result(),
        ]
        self.assertTrue(self.recovery.try_reconnect({}))
        self.nmcli.assert_called_with(
            "connection", "up", "uuid", "second", "ifname", "wlan0", sudo=True, timeout=40,
        )

    def test_backup_used_after_saved_network_has_no_internet(self):
        self.nmcli.side_effect = [
            result("saved-uuid:802-11-wireless\n"), result("wlan0\ninfrastructure\n"),
            result(), result(),
        ]
        self.internet.side_effect = [False, True]
        self.assertTrue(self.recovery.try_reconnect({"ssid": "reserve", "password": "test-password"}))
        self.nmcli.assert_called_with(
            "device", "wifi", "connect", "reserve", "ifname", "wlan0",
            "password", "test-password", sudo=True, timeout=40,
        )
        self.start.assert_not_called()

    def test_backup_timeout_restores_hotspot(self):
        self.nmcli.side_effect = [result(), subprocess.TimeoutExpired("nmcli", 40)]
        self.assertFalse(self.recovery.try_reconnect({"ssid": "reserve"}))
        self.start.assert_called_once()

    def test_stop_failure_still_attempts_to_restore_hotspot(self):
        self.stop.side_effect = RuntimeError("stop failed")
        self.assertFalse(self.recovery.try_reconnect({}))
        self.start.assert_called_once()

    def test_profile_lookup_failure_keeps_hotspot(self):
        self.nmcli.side_effect = subprocess.TimeoutExpired("nmcli", 10)
        self.assertFalse(self.recovery.try_reconnect({}))
        self.stop.assert_not_called()

    def test_no_saved_networks_keeps_hotspot(self):
        self.nmcli.side_effect = [result()]
        self.assertFalse(self.recovery.try_reconnect({}))
        self.stop.assert_not_called()

    def test_profiles_exclude_ethernet_hotspot_and_other_interface(self):
        self.nmcli.side_effect = [
            result("wired:802-3-ethernet\nap:802-11-wireless\nother:802-11-wireless\nnormal:802-11-wireless\n"),
            result("wlan0\nap\n"), result("wlan1\ninfrastructure\n"),
            result("\ninfrastructure\n"), result(),
        ]
        self.assertTrue(self.recovery.try_reconnect({}))
        self.nmcli.assert_called_with(
            "connection", "up", "uuid", "normal", "ifname", "wlan0", sudo=True, timeout=40,
        )

    def test_new_hotspot_gets_full_five_minute_grace_period(self):
        self.recovery.hotspot_started()
        self.now.return_value = 1599
        self.assertFalse(self.recovery.try_reconnect({}))
        self.stop.assert_not_called()

    def test_accepts_wifi_type_alias_and_unset_mode(self):
        self.nmcli.side_effect = [result("saved-uuid:wifi\n"), result("\n\n"), result()]
        self.assertTrue(self.recovery.try_reconnect({}))


if __name__ == "__main__":
    unittest.main()
