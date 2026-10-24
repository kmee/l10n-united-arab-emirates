# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
import re
from typing import ClassVar

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


class UaeEdiIncidentRestoration(models.Model):
    _name = "l10n_ae.edi.incident.restoration"
    _description = "UAE Reviewed Service Restoration"
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
    evidence_origin = fields.Selection(
        [("simulated", "Controlled Simulation"), ("external", "Externally Supplied")],
        required=True,
        readonly=True,
    )
    restored_at = fields.Datetime(
        string="Declared Restored At",
        required=True,
        readonly=True,
    )
    source_reference = fields.Text(required=True, readonly=True)
    review_note = fields.Text(required=True, readonly=True)
    input_hash = fields.Char(required=True, readonly=True)
    recorded_by_id = fields.Many2one(
        "res.users",
        required=True,
        readonly=True,
        ondelete="restrict",
    )
    recorded_at = fields.Datetime(required=True, readonly=True)

    _restoration_key_unique = models.UniqueIndex(
        "(incident_id, evidence_key)",
        "This restoration key already exists.",
    )
    _input_names: ClassVar[set[str]] = {
        "evidence_key",
        "evidence_origin",
        "restored_at",
        "source_reference",
        "review_note",
    }

    @api.model_create_multi
    def create(self, vals_list):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Use the incident restoration review action."))

    def write(self, vals):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Service restoration review is immutable."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_restoration_review(self):
        raise UserError(self.env._("Service restoration history cannot be deleted."))

    @api.model
    def _record(self, incident, evidence):
        if incident.state != "open" or incident.incident_kind != "system_failure":
            raise UserError(self.env._("Select an open reviewed system failure."))
        if not isinstance(evidence, dict) or set(evidence) != self._input_names:
            raise ValidationError(self.env._("Supply the complete restoration review."))
        values = {}
        for name in self._input_names:
            value = evidence[name]
            limit = 128 if name == "evidence_key" else 10000
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValidationError(self.env._("Supply valid restoration values."))
            values[name] = value.strip()
        if values["evidence_origin"] not in {"simulated", "external"}:
            raise ValidationError(self.env._("Review the restoration evidence origin."))
        timestamp = values["restored_at"]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", timestamp):
            raise ValidationError(
                self.env._("Use a declared UTC restoration timestamp.")
            )
        try:
            restored_at = fields.Datetime.to_datetime(timestamp)
        except (ValueError, TypeError) as exc:
            raise ValidationError(
                self.env._("Use a declared UTC restoration timestamp.")
            ) from exc
        if not incident.occurrence_at <= restored_at <= fields.Datetime.now():
            raise ValidationError(self.env._("Review restoration against the failure."))
        input_hash = hashlib.sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
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
                raise ValidationError(self.env._("This restoration key conflicts."))
            return existing
        # A checked, serialized local declaration; no service or fiscal success.
        return (
            super(UaeEdiIncidentRestoration, self.sudo())
            .create(
                {
                    **values,
                    "incident_id": incident.id,
                    "company_id": incident.company_id.id,
                    "input_hash": input_hash,
                    "recorded_by_id": self.env.uid,
                    "recorded_at": fields.Datetime.now(),
                }
            )
            .sudo(False)
        )
