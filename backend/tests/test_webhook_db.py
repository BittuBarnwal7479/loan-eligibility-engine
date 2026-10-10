"""Outbound n8n trigger, tested against a real local HTTP server (no n8n needed)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.config import get_settings
from tests.helpers import HEADER, import_csv

pytestmark = pytest.mark.db


class _Recorder(BaseHTTPRequestHandler):
    status = 200
    received: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((dict(self.headers), json.loads(body)))
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def webhook_server():
    _Recorder.received = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def _configure(monkeypatch, server):
    monkeypatch.setenv("N8N_MATCHING_WEBHOOK_URL", f"http://127.0.0.1:{server.server_port}/webhook/loan-matching")
    monkeypatch.setenv("N8N_WEBHOOK_TOKEN", "hook-secret")
    get_settings.cache_clear()
    return get_settings()


def test_successful_import_triggers_authenticated_webhook(db, monkeypatch, webhook_server):
    settings = _configure(monkeypatch, webhook_server)
    batch_id = import_csv(settings, HEADER + "U1,a@example.com,1000,700,salaried,30\n")
    headers, payload = _Recorder.received[0]
    assert headers["X-Webhook-Token"] == "hook-secret"
    assert payload == {"event": "upload.completed", "upload_batch_id": batch_id}
    assert "a@example.com" not in json.dumps(payload)  # no personal data in the trigger
    row = db.execute("SELECT matching_status FROM upload_batches WHERE id = %s", (batch_id,)).fetchone()
    assert row["matching_status"] == "TRIGGERED"


def test_rejected_webhook_is_recorded_and_import_kept(db, monkeypatch, webhook_server):
    _Recorder.status = 401
    try:
        settings = _configure(monkeypatch, webhook_server)
        batch_id = import_csv(settings, HEADER + "U1,a@example.com,1000,700,salaried,30\n")
    finally:
        _Recorder.status = 200
    row = db.execute(
        "SELECT status, matching_status, matching_error FROM upload_batches WHERE id = %s", (batch_id,)
    ).fetchone()
    assert row["status"] == "COMPLETED"
    assert row["matching_status"] == "TRIGGER_FAILED" and row["matching_error"] == "HTTP 401"
    assert len(_Recorder.received) == 1  # 4xx is not retried
