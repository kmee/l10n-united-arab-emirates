# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import uuid

from odoo import fields, models


class UaeEdiIncidentImpactWizard(models.TransientModel):
    _name = "l10n_ae.edi.incident.impact.wizard"
    _description = "Review UAE Affected Documents"
    _check_company_auto = True

    incident_id = fields.Many2one("l10n_ae.edi.incident", required=True, readonly=True)
    company_id = fields.Many2one(related="incident_id.company_id", readonly=True)
    evidence_key = fields.Char(
        required=True, readonly=True, copy=False, default=lambda self: uuid.uuid4().hex
    )
    transmission_ids = fields.Many2many(
        "l10n_ae.edi.transmission",
        string="Affected Documents",
        required=True,
        check_company=True,
    )
    source_reference = fields.Text(required=True)
    review_note = fields.Text(required=True)

    def action_confirm(self):
        self.ensure_one()
        self.with_user(self.env.uid).check_access("read")
        self.incident_id.record_affected_transmissions(
            {
                "evidence_key": self.evidence_key,
                "transmission_ids": self.transmission_ids.ids,
                "source_reference": self.source_reference,
                "review_note": self.review_note,
            }
        )
        return {"type": "ir.actions.act_window_close"}


class UaeEdiIncidentRestorationWizard(models.TransientModel):
    _name = "l10n_ae.edi.incident.restoration.wizard"
    _description = "Review UAE Service Restoration"

    incident_id = fields.Many2one("l10n_ae.edi.incident", required=True, readonly=True)
    evidence_key = fields.Char(
        required=True, readonly=True, copy=False, default=lambda self: uuid.uuid4().hex
    )
    evidence_origin = fields.Selection(
        [("simulated", "Controlled Simulation"), ("external", "Externally Supplied")],
        required=True,
    )
    restored_at = fields.Datetime(string="Declared Restored At", required=True)
    source_reference = fields.Text(required=True)
    review_note = fields.Text(required=True)

    def action_confirm(self):
        self.ensure_one()
        self.with_user(self.env.uid).check_access("read")
        self.incident_id.record_restoration(
            {
                "evidence_key": self.evidence_key,
                "evidence_origin": self.evidence_origin,
                "restored_at": fields.Datetime.to_string(self.restored_at),
                "source_reference": self.source_reference,
                "review_note": self.review_note,
            }
        )
        return {"type": "ir.actions.act_window_close"}
