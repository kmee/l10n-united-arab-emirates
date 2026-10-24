# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Transport qualification of KMEE's simulation SDK, without fiscal fixtures."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from odoo.tests import TransactionCase, tagged

from odoo.addons.l10n_ae_edi.tools.controlled_client import ControlledAspClient


@contextmanager
def _local_submit_http_server(observed):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.connection.settimeout(2)
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            observed.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "idempotency_key": self.headers.get("Idempotency-Key"),
                    "content_type": self.headers.get("Content-Type"),
                    "body": json.loads(raw.decode("utf-8")),
                }
            )
            content = (
                b'{"contract_version":"kmee-controlled-1",'
                b'"simulation":true,"remote_id":"synthetic-remote-201"}'
            )
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, _format, *_args):
            # Do not emit request headers or body in test-server logs.
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        try:
            server.shutdown()
        finally:
            try:
                server.server_close()
            finally:
                thread.join(timeout=3)


@tagged("post_install", "-at_install")
class TestControlledAspClientSubmitHttp(TransactionCase):
    def test_submit_sends_envelope_and_idempotency_over_loopback_http(self):
        observed = []
        document = {
            "contract_version": "kmee-controlled-1",
            "snapshot": "{}",
            "snapshot_sha256": (
                "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
            ),
            "xml": None,
            "xml_sha256": None,
        }
        idempotency_key = "synthetic-sdk-submit-key-1"
        token = "synthetic-local-only-token"
        expected_result = {
            "contract_version": "kmee-controlled-1",
            "simulation": True,
            "remote_id": "synthetic-remote-201",
        }

        with _local_submit_http_server(observed) as endpoint:
            client = ControlledAspClient(endpoint, token, "controlled", timeout=3)
            result = client.submit(idempotency_key, document)

        self.assertEqual(result, expected_result)
        self.assertEqual(len(observed), 1)
        self.assertEqual(
            observed[0],
            {
                "method": "POST",
                "path": "/mock/v1/controlled/submissions",
                "authorization": "Bearer " + token,
                "idempotency_key": idempotency_key,
                "content_type": "application/json",
                "body": document,
            },
        )
