# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import uuid

from odoo import fields, models
from odoo.exceptions import ValidationError


class UaeEdiIncidentConfirmationWizard(models.TransientModel):
    _name = "l10n_ae.edi.incident.confirmation.wizard"
    _description = "Record Original UAE FTA Confirmation"

    incident_id = fields.Many2one("l10n_ae.edi.incident", required=True, readonly=True)
    evidence_key = fields.Char(
        required=True, readonly=True, copy=False, default=lambda self: uuid.uuid4().hex
    )
    evidence_origin = fields.Selection(
        [("simulated", "Controlled Simulation"), ("external", "Externally Supplied")],
        required=True,
    )
    received_at = fields.Datetime(string="Declared Received At", required=True)
    external_reference = fields.Text(required=True)
    source_reference = fields.Text(required=True)
    review_note = fields.Text(required=True)
    document = fields.Binary(required=True, attachment=False)
    document_filename = fields.Char(required=True)

    def action_confirm(self):
        self.ensure_one()
        self.with_user(self.env.uid).check_access("read")
        if not self.document:
            raise ValidationError(self.env._("Supply the original FTA confirmation."))
        evidence = {
            name: self[name]
            for name in (
                "evidence_key",
                "evidence_origin",
                "external_reference",
                "source_reference",
                "review_note",
                "document_filename",
            )
        }
        evidence.update(
            received_at=fields.Datetime.to_string(self.received_at),
            document=self.document.to_base64(),
        )
        self.incident_id.record_fta_confirmation(evidence)
        return {"type": "ir.actions.act_window_close"}
