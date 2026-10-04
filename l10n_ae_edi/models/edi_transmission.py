# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
import uuid
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.binary import BinaryBytes


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
    exchange_mode = fields.Selection(
        [("exchange", "Invoice Exchange"), ("reporting_only", "Reporting Only")],
        default="exchange",
        required=True,
        readonly=True,
    )
    origin_system = fields.Char(required=True)
    origin_document_id = fields.Char(required=True)
    revision = fields.Integer(default=1, required=True)
    payload = fields.Text(required=True, readonly=True)
    payload_hash = fields.Char(required=True, readonly=True)
    xml_content = fields.Text(readonly=True)
    xml_hash = fields.Char(readonly=True)
    conventional_html_content = fields.Text(readonly=True, copy=False)
    conventional_html_hash = fields.Char(readonly=True, copy=False)
    conventional_report_id = fields.Many2one(
        "ir.actions.report", readonly=True, copy=False, ondelete="restrict"
    )
    conventional_filename = fields.Char(readonly=True, copy=False)
    conventional_pdf_content = fields.Binary(
        readonly=True, copy=False, attachment=False
    )
    conventional_pdf_hash = fields.Char(readonly=True, copy=False)
    conventional_pdf_generated_at = fields.Datetime(readonly=True, copy=False)
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
        [
            ("unknown", "Unknown"),
            ("pending", "Pending"),
            ("passed", "Passed"),
            ("rejected", "Rejected"),
        ],
        default="unknown",
        readonly=True,
        required=True,
    )
    delivery_c3_state = fields.Selection(
        [
            ("not_applicable", "Not Applicable"),
            ("unknown", "Unknown"),
            ("pending", "Pending"),
            ("delivered", "Delivered"),
            ("rejected", "Rejected"),
        ],
        default="unknown",
        readonly=True,
        required=True,
    )
    reporting_c2_state = fields.Selection(
        [
            ("unknown", "Unknown"),
            ("pending", "Pending"),
            ("confirmed", "Confirmed"),
            ("rejected", "Rejected"),
        ],
        default="unknown",
        readonly=True,
        required=True,
    )
    reporting_c3_state = fields.Selection(
        [
            ("not_applicable", "Not Applicable"),
            ("unknown", "Unknown"),
            ("pending", "Pending"),
            ("confirmed", "Confirmed"),
            ("rejected", "Rejected"),
        ],
        default="unknown",
        readonly=True,
        required=True,
    )

    event_ids = fields.One2many("l10n_ae.edi.event", "transmission_id", readonly=True)

    event_review_required = fields.Boolean(readonly=True, copy=False)

    confirmation_state = fields.Selection(
        [
            ("pending", "Pending Confirmations"),
            ("confirmed", "Confirmations Complete"),
            ("review", "Requires Review"),
        ],
        compute="_compute_confirmation_state",
    )

    @api.depends(
        "exchange_mode",
        "submission_state",
        "validation_c2_state",
        "delivery_c3_state",
        "reporting_c2_state",
        "reporting_c3_state",
        "event_review_required",
    )
    def _compute_confirmation_state(self):
        for record in self:
            expected = {
                "validation_c2_state": "passed",
                "reporting_c2_state": "confirmed",
            }
            if record.exchange_mode == "reporting_only":
                expected.update(
                    delivery_c3_state="not_applicable",
                    reporting_c3_state="not_applicable",
                )
            else:
                expected.update(
                    delivery_c3_state="delivered", reporting_c3_state="confirmed"
                )
            if record.event_review_required or any(
                record[name] == "rejected" for name in expected
            ):
                record.confirmation_state = "review"
            elif record.submission_state == "accepted" and all(
                record[name] == state for name, state in expected.items()
            ):
                record.confirmation_state = "confirmed"
            else:
                record.confirmation_state = "pending"

    def _mark_controlled_event_review(self):
        return super().write({"event_review_required": True})

    def _ensure_conventional_pdf(self):
        """Render preserved content once; no invoice recalculation or delivery."""
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_transmission WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()
        if not self.conventional_html_content or not self.conventional_report_id:
            raise UserError(
                self.env._("No preserved conventional invoice is available.")
            )
        html = self.conventional_html_content.encode()
        if hashlib.sha256(html).hexdigest() != self.conventional_html_hash:
            raise UserError(
                self.env._("The preserved invoice presentation is corrupt.")
            )
        if self.conventional_pdf_content:
            content = self.conventional_pdf_content.content
            if hashlib.sha256(content).hexdigest() != self.conventional_pdf_hash:
                raise UserError(self.env._("The preserved invoice PDF is corrupt."))
            return
        report = self.conventional_report_id.with_company(self.company_id)
        engine = report._get_pdf_engine(report)
        content, _record_ids = report._run_pdf_engine(engine, html, report_ref=report)
        if not content.startswith(b"%PDF-"):
            raise UserError(self.env._("The report engine did not return a PDF."))
        # Only this checked invoice action may append the first PDF artifact.
        super().write(
            {
                "conventional_pdf_content": BinaryBytes(
                    content, filename=self.conventional_filename
                ),
                "conventional_pdf_hash": hashlib.sha256(content).hexdigest(),
                "conventional_pdf_generated_at": fields.Datetime.now(),
            }
        )

    def _apply_controlled_event(self, event):
        self.ensure_one()
        return super().write({event.dimension: event.fact_state})

    def _validate_controlled_document(self):
        raise UserError(
            self.env._("Install the UAE PINT format for controlled sending.")
        )

    def _controlled_pending_dimensions(self):
        self.ensure_one()
        c3_state = (
            "not_applicable" if self.exchange_mode == "reporting_only" else "pending"
        )
        return {
            "validation_c2_state": "pending",
            "delivery_c3_state": c3_state,
            "reporting_c2_state": "pending",
            "reporting_c3_state": c3_state,
        }

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
            values.update(self._controlled_pending_dimensions())
            values["remote_id"] = receipt["remote_id"]
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
            "exchange_mode",
            "origin_system",
            "origin_document_id",
            "revision",
            "payload",
            "payload_hash",
            "idempotency_key",
            "xml_content",
            "xml_hash",
            "conventional_html_content",
            "conventional_html_hash",
            "conventional_report_id",
            "conventional_filename",
            "conventional_pdf_content",
            "conventional_pdf_hash",
            "conventional_pdf_generated_at",
            "submission_state",
            "remote_id",
            "transport_endpoint",
            "lease_until",
            "claim_token",
            "attempt_ids",
            "event_ids",
            "event_review_required",
            "confirmation_state",
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
