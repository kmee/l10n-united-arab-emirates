# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Public worker-to-event coverage; native TestCursor does not prove durability."""

import hashlib
import hmac
import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from odoo.tests import HttpCase, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_ae_edi.tests.common import (
    CONTROLLED_CAPABILITIES,
    review_controlled_binding,
)
from odoo.addons.l10n_ae_edi.tools.controlled_executor import ControlledExecutor

from .common import requires_normative_resources


@contextmanager
def _literal_controlled_http_server(observed):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.connection.settimeout(3)
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            document = json.loads(raw.decode("utf-8"))
            key = self.headers.get("Idempotency-Key")
            snapshot = json.loads(document["snapshot"])
            receipt = {
                "contract_version": "kmee-controlled-1",
                "simulation": True,
                "remote_id": "synthetic-native-http-accepted-1",
                "provider": "controlled",
                "tin": snapshot["company"]["tin"],
                "request_key": key,
                "snapshot_sha256": document["snapshot_sha256"],
                "xml_sha256": document["xml_sha256"],
                "submission_state": "accepted",
                "validation_c2_state": "pending",
                "delivery_c3_state": "pending",
                "reporting_c2_state": "pending",
                "reporting_c3_state": "pending",
            }
            observed.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "idempotency_key": key,
                    "content_type": self.headers.get("Content-Type"),
                    "body": document,
                }
            )
            content = json.dumps(receipt, sort_keys=True).encode("utf-8")
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, _format, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 3
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
                thread.join(timeout=5)


@tagged("post_install", "-at_install")
class TestUaeNativeControlledEventHttp(AccountTestInvoicingCommon, HttpCase):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.product_a.l10n_ae_classification_code = "999999"
        cls.env.company.write(
            {
                "l10n_ae_tin": "1000000001",
                "l10n_ae_tin_confirmed": True,
                "l10n_ae_edi_auto_prepare": False,
            }
        )
        cls.env.company.partner_id.write(
            {
                "vat": "100000000100003",
                "street": "Synthetic supplier street",
                "city": "Dubai",
                "state_id": cls.env.ref("base.state_ae_du").id,
                "routing_scheme": "0235",
                "routing_endpoint": "1000000001",
                "l10n_ae_registration_type": "TL",
                "l10n_ae_registration_authority": "Synthetic UAE Authority",
                "additional_identifiers": {"AE_TL": "SYNTH-NATIVE-HTTP-0001"},
            }
        )
        cls.partner_a.write(
            {
                "country_id": cls.env.ref("base.ae").id,
                "vat": "100000000200003",
                "street": "Synthetic buyer street",
                "city": "Dubai",
                "state_id": cls.env.ref("base.state_ae_du").id,
                "routing_scheme": "0235",
                "routing_endpoint": "1000000002",
                "l10n_ae_registration_type": "TL",
                "l10n_ae_registration_authority": "Synthetic UAE Authority",
                "additional_identifiers": {"AE_TL": "SYNTH-NATIVE-HTTP-0002"},
            }
        )
        cls.binding = review_controlled_binding(
            cls.env["l10n_ae.edi.binding"].create(
                {
                    "name": "Synthetic native HTTP event sandbox",
                    "fiscal_uuid_policy": "controlled_local",
                }
            ),
            cls.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Reviewed local simulation contract for native event HTTP test.",
        )
        cls.env.company.l10n_ae_edi_binding_id = cls.binding

    def _accepted_invoice(self):
        manager = self.simple_accountman
        company = self.env.company
        move = (
            self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
            .with_user(manager)
            .with_context(allowed_company_ids=company.ids)
        )
        self.assertFalse(move.env.su)
        self.assertTrue(manager.has_group("account.group_account_manager"))
        move.l10n_ae_binding_id = self.binding
        move.action_post()
        self.assertEqual(move.state, "posted")
        self.assertEqual(
            (move.amount_untaxed, move.amount_tax, move.amount_total), (100, 5, 105)
        )
        ledger_fields = [
            "account_id",
            "partner_id",
            "currency_id",
            "date",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "tax_tag_ids",
            "tax_line_id",
            "tax_repartition_line_id",
            "analytic_distribution",
            "display_type",
            "quantity",
            "price_unit",
            "product_id",
            "write_uid",
            "write_date",
        ]
        self.env.flush_all()
        move.line_ids.invalidate_recordset()
        posted_ledger = move.line_ids.read(ledger_fields, load=None)

        move.action_l10n_ae_prepare()
        transmission = move.l10n_ae_transmission_id
        self.assertTrue(transmission)
        self.assertEqual(transmission.submission_state, "queued")
        self.assertEqual(
            (move.amount_untaxed, move.amount_tax, move.amount_total), (100, 5, 105)
        )
        frozen_fields = [
            "company_id",
            "move_id",
            "binding_id",
            "provider",
            "environment",
            "process",
            "exchange_mode",
            "origin_system",
            "origin_document_id",
            "revision",
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
        ]
        frozen_identity = transmission.read(frozen_fields, load=None)
        self.assertFalse(transmission.attempt_ids)
        self.assertFalse(transmission.event_ids)
        self.assertTrue(transmission.xml_content)
        self.env.flush_all()
        move.line_ids.invalidate_recordset()
        prepared_ledger = move.line_ids.read(ledger_fields, load=None)
        self.assertEqual(prepared_ledger, posted_ledger)
        posted_ledger = prepared_ledger

        observed = []
        token = "synthetic-native-http-token"
        with _literal_controlled_http_server(observed) as endpoint:
            outcome = ControlledExecutor(
                self.env.registry,
                manager.id,
                company.id,
                endpoint,
                token,
                retry_jitter=lambda: 0,
            ).run_once(transmission.id)
        self.assertEqual(outcome, "accepted")
        self.assertEqual(len(observed), 1)
        self.assertEqual(
            observed[0],
            {
                "method": "POST",
                "path": "/mock/v1/controlled/submissions",
                "authorization": "Bearer " + token,
                "idempotency_key": transmission.idempotency_key,
                "content_type": "application/json",
                "body": {
                    "contract_version": "kmee-controlled-1",
                    "snapshot": transmission.payload,
                    "snapshot_sha256": transmission.payload_hash,
                    "xml": transmission.xml_content,
                    "xml_sha256": transmission.xml_hash,
                },
            },
        )
        transmission.invalidate_recordset()
        self.assertEqual(transmission.submission_state, "accepted")
        self.assertEqual(transmission.remote_id, "synthetic-native-http-accepted-1")

        return (
            move,
            transmission,
            ledger_fields,
            posted_ledger,
            frozen_fields,
            frozen_identity,
        )

    def _assert_original_unchanged(self, fixture):
        (
            move,
            transmission,
            ledger_fields,
            posted_ledger,
            frozen_fields,
            frozen_identity,
        ) = fixture
        move.invalidate_recordset()
        transmission.invalidate_recordset()
        self.env.flush_all()
        move.line_ids.invalidate_recordset()
        self.assertEqual(move.state, "posted")
        self.assertEqual(
            (move.amount_untaxed, move.amount_tax, move.amount_total), (100, 5, 105)
        )
        self.assertEqual(move.line_ids.read(ledger_fields, load=None), posted_ledger)
        self.assertEqual(transmission.submission_state, "accepted")
        self.assertEqual(transmission.remote_id, "synthetic-native-http-accepted-1")
        self.assertEqual(len(transmission.attempt_ids), 1)
        self.assertEqual(
            transmission.read(frozen_fields, load=None),
            frozen_identity,
        )

    def _event_bytes(self, transmission, *, event_id, dimension, state):
        transmission.invalidate_recordset()
        return json.dumps(
            {
                "contract_version": "kmee-controlled-events-1",
                "event_id": event_id,
                "provider": transmission.provider,
                "tin": "1000000001",
                "request_key": transmission.idempotency_key,
                "remote_id": transmission.remote_id,
                "revision": transmission.revision,
                "snapshot_sha256": transmission.payload_hash,
                "xml_sha256": transmission.xml_hash,
                "dimension": dimension,
                "state": state,
                "sequence": 1,
                "occurred_at": "2026-10-10 00:00:00",
            },
            sort_keys=True,
        ).encode("utf-8")

    def _post_event(self, raw, expected_outcome):
        self.authenticate(None, None)
        prefix = f"KMEE_UAE_CONTROLLED_EVENTS_{self.binding.id}"
        env_keys = tuple(
            prefix + suffix
            for suffix in ("_DATABASE", "_USER_ID", "_COMPANY_ID", "_SECRET")
        )
        old_env = {key: os.environ.get(key) for key in env_keys}
        secret = b"synthetic-native-event-secret-2026"
        credentials = {
            env_keys[0]: self.env.cr.dbname,
            env_keys[1]: str(self.simple_accountman.id),
            env_keys[2]: str(self.env.company.id),
            env_keys[3]: secret.decode("ascii"),
        }
        try:
            os.environ.update(credentials)
            signature = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
            response = self.url_open(
                f"/l10n_ae/controlled/events/{self.binding.id}",
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-KMEE-UAE-Signature": signature,
                },
                timeout=15,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"outcome": expected_outcome})
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    @requires_normative_resources("1.0.4")
    def test_native_prepared_invoice_accepts_signed_public_http_event(self):
        fixture = self._accepted_invoice()
        transmission = fixture[1]
        raw = self._event_bytes(
            transmission,
            event_id="native-http-validation-event-1",
            dimension="validation_c2_state",
            state="passed",
        )
        self._post_event(raw, "applied")
        self._assert_original_unchanged(fixture)
        self.assertEqual(transmission.validation_c2_state, "passed")
        events = transmission.event_ids.sorted("id")
        self.assertEqual(len(events), 1)
        self.assertEqual(events.event_id, "native-http-validation-event-1")
        self.assertEqual(events.outcome, "applied")
        self.assertEqual(events.original_body, raw.decode("utf-8"))
        self.assertEqual(events.body_hash, hashlib.sha256(raw).hexdigest())

    @requires_normative_resources("1.0.4")
    def test_public_http_deduplicates_replay_and_retains_divergent_original(self):
        fixture = self._accepted_invoice()
        transmission = fixture[1]
        original = self._event_bytes(
            transmission,
            event_id="native-http-report-event-1",
            dimension="reporting_c2_state",
            state="confirmed",
        )
        self._post_event(original, "applied")
        self._post_event(original, "duplicate")
        self._assert_original_unchanged(fixture)
        self.assertEqual(transmission.reporting_c2_state, "confirmed")
        self.assertEqual(len(transmission.event_ids), 1)
        self.assertFalse(transmission.event_review_required)

        divergent = self._event_bytes(
            transmission,
            event_id="native-http-report-event-1",
            dimension="reporting_c2_state",
            state="rejected",
        )
        self.assertNotEqual(divergent, original)
        self._post_event(divergent, "replay_conflict")
        self._post_event(divergent, "duplicate")
        self._assert_original_unchanged(fixture)
        self.assertEqual(transmission.reporting_c2_state, "confirmed")
        self.assertTrue(transmission.event_review_required)
        events = transmission.event_ids.sorted("id")
        self.assertEqual(len(events), 2)
        self.assertEqual(events.mapped("event_id"), ["native-http-report-event-1"] * 2)
        self.assertEqual(events.mapped("outcome"), ["applied", "replay_conflict"])
        self.assertEqual(events.mapped("fact_state"), ["confirmed", "rejected"])
        self.assertEqual(
            events.mapped("original_body"), [original.decode(), divergent.decode()]
        )
        self.assertEqual(
            events.mapped("body_hash"),
            [
                hashlib.sha256(original).hexdigest(),
                hashlib.sha256(divergent).hexdigest(),
            ],
        )
        self.assertEqual(len(transmission.attempt_ids), 1)
