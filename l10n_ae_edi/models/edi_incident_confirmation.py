# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import base64
import binascii
import hashlib
import json
import re
from typing import ClassVar

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.binary import BinaryBytes


class UaeEdiIncidentConfirmation(models.Model):
    _name = "l10n_ae.edi.incident.confirmation"
    _description = "UAE Supplied FTA Registration Confirmation"
    _rec_name = "external_reference"
    _check_company_auto = True

    incident_id = fields.Many2one(
        "l10n_ae.edi.incident",
        required=True,
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    evidence_key = fields.Char(required=True, readonly=True)
    evidence_origin = fields.Selection(
        [("simulated", "Controlled Simulation"), ("external", "Externally Supplied")],
        required=True,
        readonly=True,
    )
    received_at = fields.Datetime(
        string="Declared Received At",
        required=True,
        readonly=True,
        help="Reviewed receipt time; this does not verify authorship or delivery.",
    )
    external_reference = fields.Text(required=True, readonly=True)
    source_reference = fields.Text(required=True, readonly=True)
    review_note = fields.Text(required=True, readonly=True)
    document_content = fields.Binary(
        required=True, readonly=True, attachment=False, groups="base.group_system"
    )
    document = fields.Binary(
        compute="_compute_document", compute_sudo=False, attachment=False
    )
    document_filename = fields.Char(required=True, readonly=True)
    document_sha256 = fields.Char(required=True, readonly=True)
    evidence_hash = fields.Char(required=True, readonly=True)
    recorded_by_id = fields.Many2one(
        "res.users", required=True, readonly=True, ondelete="restrict"
    )
    recorded_at = fields.Datetime(required=True, readonly=True)

    _incident_unique = models.UniqueIndex(
        "(incident_id)", "This incident already has an original FTA confirmation."
    )
    _required_names: ClassVar[set[str]] = {
        "evidence_key",
        "evidence_origin",
        "received_at",
        "external_reference",
        "source_reference",
        "review_note",
        "document_filename",
        "document",
    }
    _max_document_bytes = 10 * 1024 * 1024

    @api.depends_context("uid", "allowed_company_ids")
    @api.depends("document_content", "document_sha256", "incident_id")
    def _compute_document(self):
        for record in self:
            actual = record.with_user(self.env.uid)
            actual.check_access("read")
            actual.incident_id._check_actor("read")
            original = actual.sudo().document_content
            content = original.content if original else b""
            if hashlib.sha256(content).hexdigest() != actual.document_sha256:
                raise UserError(
                    self.env._("The stored confirmation failed integrity review.")
                )
            record.document = BinaryBytes(content)

    @api.model_create_multi
    def create(self, vals_list):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Use the incident FTA confirmation action."))

    def write(self, vals):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Supplied FTA confirmation evidence is immutable."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_confirmation_evidence(self):
        raise UserError(self.env._("Supplied FTA confirmation cannot be deleted."))

    @api.model
    def _normalize_confirmation(self, evidence):
        if not isinstance(evidence, dict) or set(evidence) != self._required_names:
            raise ValidationError(
                self.env._("Supply the complete FTA confirmation evidence.")
            )
        values = {}
        for name in self._required_names - {"document"}:
            value = evidence[name]
            if not isinstance(value, str) or not value.strip() or len(value) > 10000:
                raise ValidationError(
                    self.env._("Supply a valid confirmation value for %s.", name)
                )
            values[name] = value.strip()
        filename = values["document_filename"]
        if (
            values["evidence_origin"] not in {"simulated", "external"}
            or len(values["evidence_key"]) > 128
            or len(filename) > 255
            or filename in {".", ".."}
            or re.search(r"[/\\\x00-\x1f\x7f]", filename)
        ):
            raise ValidationError(
                self.env._("Review the origin, stable key and plain filename.")
            )
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", values["received_at"]
        ):
            raise ValidationError(self.env._("Use a declared UTC receipt timestamp."))
        try:
            received_at = fields.Datetime.to_datetime(values["received_at"])
        except (ValueError, TypeError) as exc:
            raise ValidationError(
                self.env._("Use a declared UTC receipt timestamp.")
            ) from exc
        if received_at > fields.Datetime.now():
            raise ValidationError(
                self.env._("Confirmation receipt cannot be in the future.")
            )
        encoded = evidence["document"]
        if (
            not isinstance(encoded, str)
            or len(encoded) > ((self._max_document_bytes + 2) // 3) * 4
        ):
            raise ValidationError(self.env._("Supply a Base64 document up to 10 MiB."))
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError(
                self.env._("Supply a valid Base64 document.")
            ) from exc
        if not content or len(content) > self._max_document_bytes:
            raise ValidationError(
                self.env._("Supply a nonempty document up to 10 MiB.")
            )
        values["document_sha256"] = hashlib.sha256(content).hexdigest()
        values["evidence_hash"] = hashlib.sha256(
            json.dumps(values, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        values["document_content"] = BinaryBytes(content)
        return values

    @api.model
    def _record(self, incident, evidence):
        incident._check_actor("write")
        if incident.incident_kind != "registration_change":
            raise UserError(self.env._("Select a registration amendment incident."))
        values = self._normalize_confirmation(evidence)
        existing = self.with_user(self.env.uid).search(
            [("incident_id", "=", incident.id)], limit=1
        )
        if existing:
            if existing.evidence_hash != values["evidence_hash"]:
                raise UserError(
                    self.env._("This incident has conflicting confirmation evidence.")
                )
            return existing
        if incident.state != "draft":
            raise UserError(
                self.env._("Capture the original confirmation before opening.")
            )
        # The parent action holds the incident row lock and checks the actual actor.
        return (
            super(UaeEdiIncidentConfirmation, self.sudo())
            .create(
                {
                    **values,
                    "incident_id": incident.id,
                    "company_id": incident.company_id.id,
                    "recorded_by_id": self.env.uid,
                    "recorded_at": fields.Datetime.now(),
                }
            )
            .sudo(False)
        )
