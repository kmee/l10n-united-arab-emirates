# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json
import uuid
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError


class UaeEdiTransmission(models.Model):
    _name = "l10n_ae.edi.transmission"
    _description = "UAE Frozen Invoice Transmission"
    _check_company_auto = True
    _rec_name = "origin_document_id"

    move_id = fields.Many2one(
        "account.move", required=True, ondelete="restrict", index=True
    )
    company_id = fields.Many2one("res.company", required=True, index=True)
    binding_id = fields.Many2one(
        "l10n_ae.edi.binding", required=True, check_company=True, ondelete="restrict"
    )
    provider = fields.Char(required=True, readonly=True)
    environment = fields.Selection(
        [("sandbox", "Sandbox"), ("production", "Production")], required=True
    )
    process = fields.Selection(
        [("billing", "Billing"), ("selfbilling", "Self-Billing")], required=True
    )
    origin_system = fields.Char(required=True)
    origin_document_id = fields.Char(required=True)
    revision = fields.Integer(default=1, required=True)
    payload = fields.Text(required=True, readonly=True)
    payload_hash = fields.Char(required=True, readonly=True)
    xml_content = fields.Text(readonly=True)
    xml_hash = fields.Char(readonly=True)
    idempotency_key = fields.Char(
        required=True, readonly=True, default=lambda self: str(uuid.uuid4()), copy=False
    )
    submission_state = fields.Selection(
        [
            ("queued", "Queued"),
            ("sending", "Sending"),
            ("accepted", "Accepted by API"),
            ("unknown", "Indeterminate"),
        ],
        default="queued",
        required=True,
        readonly=True,
    )
    remote_id = fields.Char(readonly=True, copy=False)
    validation_receipt = fields.Text(readonly=True, copy=False)
    transport_endpoint = fields.Char(readonly=True, copy=False)
    lease_until = fields.Datetime(readonly=True, copy=False)
    claim_token = fields.Char(readonly=True, copy=False, groups="base.group_system")
    attempt_ids = fields.One2many(
        "l10n_ae.edi.attempt", "transmission_id", readonly=True
    )
    validation_c2_state = fields.Selection(
        [("unknown", "Unknown"), ("pending", "Pending")],
        default="unknown",
        readonly=True,
        required=True,
    )
    delivery_c3_state = fields.Selection(
        [("unknown", "Unknown"), ("pending", "Pending")],
        default="unknown",
        readonly=True,
        required=True,
    )
    reporting_c2_state = fields.Selection(
        [("unknown", "Unknown"), ("pending", "Pending")],
        default="unknown",
        readonly=True,
        required=True,
    )
    reporting_c3_state = fields.Selection(
        [("unknown", "Unknown"), ("pending", "Pending")],
        default="unknown",
        readonly=True,
        required=True,
    )

    def _validate_controlled_document(self):
        raise UserError(
            self.env._("Install the UAE PINT format for controlled sending.")
        )

    def _claim_controlled(self, endpoint, now, validation):
        """Called only by the executor's short, dedicated transaction."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_transmission WHERE id = %s "
            "FOR UPDATE SKIP LOCKED",
            [self.id],
        )
        if not self.env.cr.fetchone():
            return None
        self.invalidate_recordset()
        if self.submission_state == "accepted" or (
            self.submission_state == "sending" and self.lease_until > now
        ):
            return None
        if self.transport_endpoint and self.transport_endpoint != endpoint:
            raise UserError(
                self.env._("Reconcile using the original controlled endpoint.")
            )
        operation = "submit" if self.submission_state == "queued" else "lookup"
        token = str(uuid.uuid4())
        proof = json.dumps(
            {
                "xml_sha256": self.xml_hash,
                "report": validation,
                "checked_at": fields.Datetime.to_string(now),
            },
            sort_keys=True,
        )
        self.env["l10n_ae.edi.attempt"].sudo().create(
            {
                "transmission_id": self.id,
                "company_id": self.company_id.id,
                "operation": operation,
                "claim_token": token,
                "started_at": now,
                "validation_receipt": proof,
            }
        )
        super().write(
            {
                "submission_state": "sending",
                "claim_token": token,
                "lease_until": now + timedelta(minutes=5),
                "transport_endpoint": endpoint,
                "validation_receipt": self.validation_receipt or proof,
            }
        )
        return {"token": token, "operation": operation}

    def _finish_controlled(self, token, receipt, error_kind, now, evidence=None):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_transmission WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()
        attempt = (
            self.env["l10n_ae.edi.attempt"]
            .sudo()
            .search([("transmission_id", "=", self.id), ("claim_token", "=", token)])
        )
        if self.claim_token != token or self.submission_state != "sending":
            if attempt and attempt.state == "started":
                attempt._complete_controlled(
                    {
                        "state": "superseded",
                        "completed_at": now,
                        "error_kind": "lease_superseded",
                        "receipt": json.dumps(evidence, sort_keys=True)
                        if evidence
                        else False,
                    }
                )
            return "stale"
        accepted = bool(receipt)
        state = "accepted" if accepted else "unknown"
        attempt._complete_controlled(
            {
                "state": state,
                "completed_at": now,
                "error_kind": error_kind,
                "receipt": json.dumps(evidence, sort_keys=True) if evidence else False,
            }
        )
        values = {"submission_state": state, "lease_until": False}
        if accepted:
            values.update(
                {
                    "remote_id": receipt["remote_id"],
                    "validation_c2_state": "pending",
                    "delivery_c3_state": "pending",
                    "reporting_c2_state": "pending",
                    "reporting_c3_state": "pending",
                }
            )
        super().write(values)
        return state

    _business_revision_unique = models.UniqueIndex(
        "(company_id, environment, process, origin_system, origin_document_id, "
        "revision)",
        "This UAE invoice revision already exists, independently of its ASP.",
    )

    def write(self, vals):
        immutable = {
            "move_id",
            "company_id",
            "binding_id",
            "provider",
            "environment",
            "process",
            "origin_system",
            "origin_document_id",
            "revision",
            "payload",
            "payload_hash",
            "idempotency_key",
            "xml_content",
            "xml_hash",
            "submission_state",
            "remote_id",
            "transport_endpoint",
            "lease_until",
            "claim_token",
            "attempt_ids",
            "validation_c2_state",
            "delivery_c3_state",
            "reporting_c2_state",
            "reporting_c3_state",
            "validation_receipt",
        }
        if immutable.intersection(vals):
            raise UserError(self.env._("Frozen UAE invoice evidence is immutable."))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_frozen_evidence(self):
        raise UserError(
            self.env._(
                "Frozen UAE invoice evidence is immutable and cannot be deleted."
            )
        )
