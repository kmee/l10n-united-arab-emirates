# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
import os

from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install")
class TestControlledEventBadSignatureHttp(HttpCase):
    def test_bad_hmac_is_refused_by_registered_public_route(self):
        # The ingress route requires valid process configuration before attempting
        # authentication. It does not query the binding until after HMAC succeeds.
        # A synthetic positive ID exercises route matching without requiring an
        # accepted transmission or fabricating one.
        self.authenticate(None, None)
        binding_id = 2147483000
        prefix = f"KMEE_UAE_CONTROLLED_EVENTS_{binding_id}"
        keys = tuple(
            prefix + suffix
            for suffix in ("_DATABASE", "_USER_ID", "_COMPANY_ID", "_SECRET")
        )
        previous = {key: os.environ.get(key) for key in keys}
        secret = "synthetic-httpcase-secret-29"
        self.assertGreaterEqual(len(secret.encode("utf-8")), 16)
        credentials = {
            keys[0]: self.env.cr.dbname,
            keys[1]: str(self.env.ref("base.user_admin").id),
            keys[2]: str(self.env.company.id),
            keys[3]: secret,
        }
        body = b"{}"
        # Correct signature syntax but deliberately incorrect HMAC for the body.
        headers = {
            "Content-Type": "application/json",
            "X-KMEE-UAE-Signature": "sha256=" + "0" * 64,
        }

        try:
            os.environ.update(credentials)
            # HttpCase drives the actual registered HTTP route. The test database
            # is selected by the native public session, not the process variable.
            response = self.url_open(
                f"/l10n_ae/controlled/events/{binding_id}",
                data=body,
                headers=headers,
                timeout=15,
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(
                json.loads(response.text), {"error": "receiving_forbidden"}
            )
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_public_http_route_rejects_body_over_application_limit(self):
        # Select this HttpCase database through Odoo's native public session.
        self.authenticate(None, None)
        binding_id = 2147483000
        body = b"x" * 65537
        self.assertEqual(len(body), 65537)

        response = self.url_open(
            f"/l10n_ae/controlled/events/{binding_id}",
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(json.loads(response.text), {"error": "body_too_large"})
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
