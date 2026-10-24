# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
from typing import ClassVar

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class UaeEdiIncidentImpact(models.Model):
    _name = "l10n_ae.edi.incident.impact"
    _description = "UAE Reviewed Incident Impact Batch"
    _rec_name = "evidence_key"
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
    source_reference = fields.Text(required=True, readonly=True)
    review_note = fields.Text(required=True, readonly=True)
    transmission_ids = fields.Many2many(
        "l10n_ae.edi.transmission",
        readonly=True,
        check_company=True,
    )
    identity_snapshot = fields.Json(required=True, readonly=True)
    input_hash = fields.Char(required=True, readonly=True)
    recorded_by_id = fields.Many2one(
        "res.users",
        required=True,
        readonly=True,
        ondelete="restrict",
    )
    recorded_at = fields.Datetime(required=True, readonly=True)

    _impact_key_unique = models.UniqueIndex(
        "(incident_id, evidence_key)",
        "This incident impact key already exists.",
    )
    _input_names: ClassVar[set[str]] = {
        "evidence_key",
        "transmission_ids",
        "source_reference",
        "review_note",
    }

    @api.model_create_multi
    def create(self, vals_list):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Use the reviewed incident impact action."))

    def write(self, vals):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Reviewed incident impact history is immutable."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_reviewed_impact(self):
        raise UserError(self.env._("Reviewed incident impacts cannot be deleted."))

    @api.model
    def _record(self, incident, evidence):
        if incident.state != "open" or incident.incident_kind != "system_failure":
            raise UserError(self.env._("Select an open reviewed system failure."))
        if not isinstance(evidence, dict) or set(evidence) != self._input_names:
            raise ValidationError(self.env._("Supply the complete impact review."))
        values = {}
        for name in self._input_names - {"transmission_ids"}:
            value = evidence[name]
            limit = 128 if name == "evidence_key" else 10000
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValidationError(self.env._("Supply valid impact review values."))
            values[name] = value.strip()
        identifiers = evidence["transmission_ids"]
        if (
            not isinstance(identifiers, list)
            or not 1 <= len(identifiers) <= 1000
            or any(type(value) is not int or value <= 0 for value in identifiers)
            or len(set(identifiers)) != len(identifiers)
        ):
            raise ValidationError(self.env._("Select explicit affected transmissions."))
        identifiers = sorted(identifiers)
        issues = (
            self.env["l10n_ae.edi.transmission"]
            .with_user(self.env.uid)
            .browse(identifiers)
        )
        if len(issues.exists()) != len(identifiers):
            raise AccessError(self.env._("Affected transmissions are not accessible."))
        issues.check_access("read")
        if any(issue.company_id != incident.company_id for issue in issues):
            raise AccessError(self.env._("Affected transmissions are not accessible."))
        issues.mapped("move_id").check_access("read")
        normalized = {**values, "transmission_ids": identifiers}
        input_hash = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        existing = self.with_user(self.env.uid).search(
            [
                ("incident_id", "=", incident.id),
                ("evidence_key", "=", values["evidence_key"]),
            ],
            limit=1,
        )
        if existing:
            if existing.input_hash != input_hash:
                raise ValidationError(
                    self.env._("This impact key has conflicting review.")
                )
            return existing
        snapshot = [
            {
                "transmission_id": issue.id,
                "move_id": issue.move_id.id,
                "binding_id": issue.binding_id.id,
                "process": issue.process,
                "revision": issue.revision,
                "idempotency_key": issue.idempotency_key,
                "payload_hash": issue.payload_hash,
                "xml_hash": issue.xml_hash,
            }
            for issue in issues
        ]
        # Parent action checked actual actor/company and holds the incident row lock.
        # Elevation persists only append-only local review, never dispatch/accounting.
        return (
            super(UaeEdiIncidentImpact, self.sudo())
            .create(
                {
                    **values,
                    "incident_id": incident.id,
                    "company_id": incident.company_id.id,
                    "transmission_ids": [Command.set(identifiers)],
                    "identity_snapshot": snapshot,
                    "input_hash": input_hash,
                    "recorded_by_id": self.env.uid,
                    "recorded_at": fields.Datetime.now(),
                }
            )
            .sudo(False)
        )
