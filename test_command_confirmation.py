"""Isola o fluxo real de comandos, sem carregar configuracao ou acessar hardware."""
import ast
from pathlib import Path
import threading
import time
import unittest
from typing import Any, Dict, Optional
from unittest.mock import Mock


def command_functions():
    source = Path(__file__).with_name("tuya_server.py")
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    names = {
        "schedule_command_heartbeat", "execute_tuya_command_request",
        "execute_remote_command_action", "process_remote_command_record",
    }
    isolated = ast.Module(
        body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names],
        type_ignores=[],
    )
    env = {
        "Any": Any, "Dict": Dict, "Optional": Optional,
        "threading": threading, "time": time, "SITE_NAME": "test-site",
        "log": Mock(), "current_timestamp_iso": Mock(return_value="2026-10-26T22:44:00+00:00"),
        "normalize_version": float,
        "send_tuya_command": Mock(return_value={"command_response": {"dps": {"1": True}}}),
        "save_device_to_cache": Mock(), "set_last_active_device": Mock(),
        "get_device_site_id_from_db": Mock(return_value="test-site"),
        "insert_heartbeat_ok_log": Mock(), "claim_remote_command": Mock(return_value=True),
        "update_remote_command_status": Mock(return_value=True),
        "insert_tuya_server_event": Mock(), "get_app_trace_result": Mock(return_value={}),
    }
    exec(compile(isolated, str(source), "exec"), env)
    return env


class CommandConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.env = command_functions()
        self.release = threading.Event()
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.addCleanup(self.release.set)
        self.payload = {
            "action": "on", "tuya_device_id": "test-device",
            "local_key": "test-key", "lan_ip": "192.0.2.1", "version": 3.5,
        }

    def block_monitoring(self, **kwargs):
        self.entered.set()
        try:
            if not self.release.wait(3):
                raise RuntimeError("Test did not release monitoring")
        finally:
            self.finished.set()

    def test_http_response_does_not_wait_for_blocked_monitoring(self):
        self.env["insert_heartbeat_ok_log"].side_effect = self.block_monitoring
        result = self.env["execute_tuya_command_request"](self.payload)
        self.assertTrue(result["ok"])
        self.assertTrue(self.entered.wait(1))
        self.assertFalse(self.finished.is_set())
        self.env["send_tuya_command"].assert_called_once()
        self.release.set()
        self.assertTrue(self.finished.wait(1))

    def test_remote_result_is_recorded_while_site_lookup_is_blocked(self):
        def blocked_lookup(device):
            self.entered.set()
            self.release.wait(3)
            return "test-site"
        self.env["get_device_site_id_from_db"].side_effect = blocked_lookup
        self.env["insert_heartbeat_ok_log"].side_effect = lambda **kw: self.finished.set()
        record = {**self.payload, "id": 123, "site_id": "test-site", "status": "pending"}
        self.env["process_remote_command_record"](record)
        self.assertTrue(self.entered.wait(1))
        args, kwargs = self.env["update_remote_command_status"].call_args
        self.assertEqual(args, (123, "done"))
        self.assertTrue(kwargs["result"]["ok"])
        self.assertFalse(self.finished.is_set())
        self.env["send_tuya_command"].assert_called_once()
        self.release.set()
        self.assertTrue(self.finished.wait(1))

    def test_failed_actuation_never_publishes_success_heartbeat(self):
        self.env["send_tuya_command"].side_effect = RuntimeError("placa indisponivel")
        self.env["process_remote_command_record"]({
            **self.payload, "id": 124, "site_id": "test-site", "status": "pending",
        })
        self.env["update_remote_command_status"].assert_called_once_with(
            124, "error", error_message="placa indisponivel",
        )
        self.env["insert_heartbeat_ok_log"].assert_not_called()

    def test_monitoring_preserves_original_timestamp_and_device(self):
        self.env["insert_heartbeat_ok_log"].side_effect = lambda **kw: self.finished.set()
        stamp = "2026-10-26T22:44:00+00:00"
        self.env["schedule_command_heartbeat"]("test-device", stamp)
        self.assertTrue(self.finished.wait(1))
        self.env["insert_heartbeat_ok_log"].assert_called_once_with(
            site_id="test-site", status="ok", tuya_device_id="test-device", event_time_iso=stamp,
        )

    def test_monitoring_worker_failure_does_not_fail_successful_actuation(self):
        self.env["get_device_site_id_from_db"].side_effect = RuntimeError("banco indisponivel")
        result = self.env["execute_tuya_command_request"](self.payload)
        self.assertTrue(result["ok"])
        self.env["send_tuya_command"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
