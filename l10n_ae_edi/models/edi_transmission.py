# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
import uuid
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.fields import Domain
from odoo.tools.binary import BinaryBytes

from ..tools.controlled_events import DIMENSION_STATES
from ..tools.errors import UaeControlledEndpointMismatch


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
    local_correction_id = fields.Many2one(
        "l10n_ae.edi.correction",
        readonly=True,
        copy=False,
        ondelete="restrict",
        check_company=True,
    )
    payload = fields.Text(required=True, readonly=True)
    payload_hash = fields.Char(required=True, readonly=True)
    xml_content = fields.Text(readonly=True)
    xml_hash = fields.Char(readonly=True)
    xml_download = fields.Binary(
        string="Original PINT XML",
        compute="_compute_xml_download",
        compute_sudo=False,
        attachment=False,
    )
    xml_download_filename = fields.Char(
        compute="_compute_xml_download_filename", compute_sudo=False
    )
    conventional_html_content = fields.Text(readonly=True, copy=False)
    conventional_html_hash = fields.Char(readonly=True, copy=False)
    conventional_report_id = fields.Many2one(
        "ir.actions.report", readonly=True, copy=False, ondelete="restrict"
    )
    conventional_filename = fields.Char(readonly=True, copy=False)
    conventional_pdf_original_content = fields.Binary(
        readonly=True, copy=False, attachment=True, groups="base.group_system"
    )
    conventional_pdf_content = fields.Binary(
        compute="_compute_conventional_pdf_content",
        compute_sudo=False,
        attachment=False,
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
    next_attempt_at = fields.Datetime(readonly=True, copy=False, index=True)
    consecutive_transport_failures = fields.Integer(readonly=True, copy=False)
    claim_token = fields.Char(readonly=True, copy=False, groups="base.group_system")
    dispatch_review_required = fields.Boolean(readonly=True, copy=False)
    dispatch_error_kind = fields.Selection(
        [
            ("local_review", "Local Document Validation"),
            ("conformance_policy", "Conformance Policy Review Required"),
            ("xml_integrity", "Frozen XML Integrity Review Required"),
            ("package_mismatch", "Pinned PINT Package Review Required"),
            ("endpoint_mismatch", "Original Controlled Endpoint Required"),
            ("provider_mismatch", "Original Provider Binding Required"),
        ],
        readonly=True,
        copy=False,
    )
    dispatch_review_at = fields.Datetime(readonly=True, copy=False)
    dispatch_validation_receipt = fields.Text(
        string="Local Validation Rejection",
        readonly=True,
        copy=False,
        help="The first rejected formal report for this frozen XML. "
        "This is local evidence, not an ASP or FTA confirmation.",
    )
    dispatch_policy_receipt = fields.Text(
        string="Pending Conformance Policy",
        readonly=True,
        copy=False,
        help="The first local policy hold and its actual valid formal report. "
        "This is not a validation rejection, fiscal approval or ASP response.",
    )
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

    @api.depends("origin_document_id", "move_id.name", "revision")
    @api.depends_context(
        "uid", "allowed_company_ids", "l10n_ae_incident_impact_selection"
    )
    def _compute_display_name(self):
        if not self.env.context.get("l10n_ae_incident_impact_selection"):
            return super()._compute_display_name()
        # Native name_search elevates label reads. Preserve the caller's access.
        originals = self.with_user(self.env.uid)
        originals.check_access("read")
        originals.move_id.check_access("read")
        for record in originals:
            self.browse(record.id).display_name = self.env._(
                "%(invoice)s (Revision %(revision)s)",
                invoice=record.move_id.name,
                revision=record.revision,
            )

    @api.model
    def _search_display_name(self, operator, value):
        origin = super()._search_display_name(operator, value)
        if not self.env.context.get("l10n_ae_incident_impact_selection"):
            return origin
        caller = self.with_user(self.env.uid)
        caller.check_access("read")
        caller.env["account.move"].check_access("read")
        invoice = Domain("move_id.name", operator, value)
        identities = (
            origin & invoice
            if operator in Domain.NEGATIVE_OPERATORS
            else origin | invoice
        )
        return identities & Domain("company_id", "in", caller.env.companies.ids)

    @api.depends("revision")
    def _compute_xml_download_filename(self):
        for record in self:
            record.xml_download_filename = f"pint-ae-{record.id}-r{record.revision}.xml"

    @api.depends("xml_content", "xml_hash")
    def _compute_xml_download(self):
        for record in self:
            if not record.xml_content:
                record.xml_download = False
                continue
            original = record.xml_content.encode("utf-8")
            if hashlib.sha256(original).hexdigest() != record.xml_hash:
                raise UserError(
                    self.env._(
                        "The preserved UAE XML does not match its recorded hash."
                    )
                )
            record.xml_download = BinaryBytes(original)

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

    @api.depends_context("uid", "allowed_company_ids")
    @api.depends(
        "conventional_pdf_original_content",
        "conventional_pdf_hash",
        "conventional_filename",
        "company_id",
    )
    def _compute_conventional_pdf_content(self):
        for record in self:
            actual = record.with_user(self.env.uid)
            actual.check_access("read")
            principal = actual.env.user
            if (
                not principal.active
                or actual.company_id.id not in principal.company_ids.ids
                or actual.company_id.id not in actual.env.companies.ids
            ):
                raise AccessError(self.env._("This UAE PDF company is not accessible."))
            original = actual.sudo().conventional_pdf_original_content
            content = original.content if original else b""
            if (content or actual.conventional_pdf_hash) and (
                not content
                or hashlib.sha256(content).hexdigest() != actual.conventional_pdf_hash
            ):
                # Lost/unmigrated originals must not be silently regenerated.
                raise UserError(self.env._("The preserved invoice PDF is corrupt."))
            record.conventional_pdf_content = (
                BinaryBytes(content, filename=actual.conventional_filename)
                if content
                else False
            )

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
        super(UaeEdiTransmission, self.with_company(self.company_id)).write(
            {
                "conventional_pdf_original_content": BinaryBytes(
                    content, filename=self.conventional_filename
                ),
                "conventional_pdf_hash": hashlib.sha256(content).hexdigest(),
                "conventional_pdf_generated_at": fields.Datetime.now(),
            }
        )

    def _apply_controlled_event(self, event):
        self.ensure_one()
        dimension = event.dimension
        state = event.fact_state
        if state not in DIMENSION_STATES.get(dimension, set()):
            raise ValidationError(
                self.env._("The controlled event fact is not supported.")
            )
        return super().write({dimension: state})

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

    def _mark_controlled_dispatch_review(
        self, now, report=None, *, error_kind="local_review"
    ):
        """Record a local validation stop without inventing a remote rejection."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_transmission WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()
        if error_kind == "conformance_policy" and self.submission_state != "queued":
            # A queued validation can race with another worker's claim. A policy
            # hold must never exclude an existing obligation from lookup.
            return self.submission_state
        if self.submission_state == "accepted" or (
            self.submission_state == "sending"
            and self.lease_until
            and self.lease_until > now
        ):
            return self.submission_state
        values = {
            "dispatch_review_required": True,
            "dispatch_error_kind": error_kind,
            "dispatch_review_at": now,
        }
        if error_kind == "conformance_policy":
            if not report or report["valid"] is not True:
                raise UserError(
                    self.env._("A conformance policy hold requires its valid report.")
                )
            if not self.dispatch_policy_receipt:
                values["dispatch_policy_receipt"] = json.dumps(
                    {
                        "xml_sha256": self.xml_hash,
                        "report": report,
                        "checked_at": fields.Datetime.to_string(now),
                        "error_kind": error_kind,
                        "decision": "pending",
                    },
                    sort_keys=True,
                )
        if (
            report is not None
            and report["valid"] is False
            and not self.dispatch_validation_receipt
        ):
            values["dispatch_validation_receipt"] = json.dumps(
                {
                    "xml_sha256": self.xml_hash,
                    "report": report,
                    "checked_at": fields.Datetime.to_string(now),
                },
                sort_keys=True,
            )
        super().write(values)
        return "blocked"

    def _mark_controlled_endpoint_review(self, endpoint, now):
        """Retain an ambiguous original route without blocking later work."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_transmission WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()
        if (
            self.submission_state == "accepted"
            or (
                self.submission_state == "sending"
                and self.lease_until
                and self.lease_until > now
            )
            or not self.transport_endpoint
            or self.transport_endpoint == endpoint
        ):
            return self.submission_state
        super().write(
            {
                "dispatch_review_required": True,
                "dispatch_error_kind": "endpoint_mismatch",
                "dispatch_review_at": now,
            }
        )
        return "blocked"

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
        if self.local_correction_id:
            return None
        if (
            self.submission_state == "accepted"
            or (self.submission_state == "sending" and self.lease_until > now)
            or (
                self.submission_state == "unknown"
                and self.next_attempt_at
                and self.next_attempt_at > now
            )
        ):
            return None
        if self.transport_endpoint and self.transport_endpoint != endpoint:
            raise UaeControlledEndpointMismatch(
                self.env._("Reconcile using the original controlled endpoint.")
            )
        operation = "submit" if self.submission_state == "queued" else "lookup"
        token = str(uuid.uuid4())
        proof = (
            self.validation_receipt
            if operation == "lookup"
            else json.dumps(
                {
                    "xml_sha256": self.xml_hash,
                    "report": validation,
                    "checked_at": fields.Datetime.to_string(now),
                },
                sort_keys=True,
            )
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

    def _finish_controlled(
        self, token, receipt, error_kind, now, evidence=None, retry_jitter=0
    ):
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
            values["next_attempt_at"] = False
            values["consecutive_transport_failures"] = 0
        else:
            failures = self.consecutive_transport_failures + 1
            delay = min(60 * 2 ** min(failures - 1, 6) + retry_jitter, 3600)
            values["consecutive_transport_failures"] = failures
            values["next_attempt_at"] = now + timedelta(seconds=delay)
        super().write(values)
        return state

    _business_revision_unique = models.UniqueIndex(
        "(company_id, environment, process, origin_system, origin_document_id, "
        "revision)",
        "This UAE invoice revision already exists, independently of its ASP.",
    )

    def write(self, vals):
        immutable = {
            "local_correction_id",
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
            "conventional_pdf_original_content",
            "conventional_pdf_content",
            "conventional_pdf_hash",
            "conventional_pdf_generated_at",
            "submission_state",
            "remote_id",
            "transport_endpoint",
            "lease_until",
            "next_attempt_at",
            "consecutive_transport_failures",
            "claim_token",
            "dispatch_review_required",
            "dispatch_error_kind",
            "dispatch_review_at",
            "dispatch_validation_receipt",
            "dispatch_policy_receipt",
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

    def _set_local_correction(self, correction):
        self.ensure_one()
        return super().write({"local_correction_id": correction.id})

    @api.ondelete(at_uninstall=False)
    def _unlink_except_frozen_evidence(self):
        raise UserError(
            self.env._(
                "Frozen UAE invoice evidence is immutable and cannot be deleted."
            )
        )
