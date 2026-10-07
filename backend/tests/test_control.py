"""Graceful-shutdown endpoint used by the desktop shell."""

from __future__ import annotations

import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.collectors.hub import SensorHub
from app.config import Settings
from tests.test_hub import FakeCpu, FakeSystem
from tests.test_storage import ServerThread


def request(srv: ServerThread, method: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str]]:
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}


def rig(token: str | None) -> tuple[tempfile.TemporaryDirectory[str], ServerThread]:
    d = tempfile.TemporaryDirectory()
    settings = Settings(poll_interval_s=0.05, data_dir=Path(d.name), flush_interval_s=0.5, control_token=token)
    hub = SensorHub(settings, lhm=FakeCpu(), system=FakeSystem(), auto_detect=False)
    return d, ServerThread(settings, hub)


class ControlTests(unittest.TestCase):
    def test_endpoint_does_not_exist_without_a_token(self) -> None:
        d, srv = rig(None)
        with d, srv:
            self.assertEqual(request(srv, "POST", "/api/control/shutdown")[0], 404)
            self.assertEqual(request(srv, "POST", "/api/control/shutdown", {"X-ThermalSense-Token": "anything"})[0], 404)
            self.assertTrue(srv.thread.is_alive())

    def test_wrong_or_missing_token_is_rejected_and_the_server_keeps_running(self) -> None:
        d, srv = rig("s3cret-token")
        with d, srv:
            self.assertEqual(request(srv, "POST", "/api/control/shutdown")[0], 403)
            self.assertEqual(request(srv, "POST", "/api/control/shutdown", {"X-ThermalSense-Token": "nope"})[0], 403)
            self.assertEqual(request(srv, "POST", "/api/control/shutdown", {"X-ThermalSense-Token": "s3cret-tokeN"})[0], 403)
            self.assertTrue(srv.thread.is_alive())
            self.assertEqual(request(srv, "GET", "/api/health")[0], 200)

    def test_get_is_not_allowed(self) -> None:
        d, srv = rig("t")
        with d, srv:
            self.assertEqual(request(srv, "GET", "/api/control/shutdown", {"X-ThermalSense-Token": "t"})[0], 405)

    def test_correct_token_stops_the_server_gracefully(self) -> None:
        d, srv = rig("s3cret-token")
        with d:
            srv.__enter__()
            time.sleep(1.0)
            code, _ = request(srv, "POST", "/api/control/shutdown", {"X-ThermalSense-Token": "s3cret-token"})
            self.assertEqual(code, 200)
            srv.thread.join(10)
            self.assertFalse(srv.thread.is_alive(), "server must exit after a valid shutdown request")
            # lifespan shutdown ran: history was flushed to disk
            self.assertTrue((Path(d.name) / "thermalsense.db").exists())

    def test_a_foreign_web_origin_cannot_even_preflight_the_control_endpoint(self) -> None:
        d, srv = rig("t")
        with d, srv:
            code, headers = request(
                srv, "OPTIONS", "/api/control/shutdown",
                {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "x-thermalsense-token"},
            )
            self.assertEqual(code, 400)
            self.assertNotIn("access-control-allow-origin", headers)

    def test_the_desktop_apps_origin_is_allowed_to_read_the_api(self) -> None:
        d, srv = rig(None)
        with d, srv:
            code, headers = request(srv, "GET", "/api/health", {"Origin": "app://thermalsense"})
            self.assertEqual(code, 200)
            self.assertEqual(headers.get("access-control-allow-origin"), "app://thermalsense")


if __name__ == "__main__":
    unittest.main()
