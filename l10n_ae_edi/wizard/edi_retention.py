# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models


class UaeEdiRetentionHoldWizard(models.TransientModel):
    _name = "l10n_ae.edi.retention.hold.wizard"
    _description = "Place Reviewed UAE Retention Hold"

    retention_id = fields.Many2one(
        "l10n_ae.edi.retention", required=True, readonly=True
    )
    reason = fields.Text(required=True)
    source_reference = fields.Text(required=True)

    def action_confirm(self):
        self.ensure_one()
        self.env["l10n_ae.edi.retention"]._check_current_editor()
        self.with_user(self.env.uid).check_access("read")
        self.env["l10n_ae.edi.retention.hold"].create(
            {
                "retention_id": self.retention_id.id,
                "reason": self.reason,
                "source_reference": self.source_reference,
            }
        )
        return {"type": "ir.actions.act_window_close"}


class UaeEdiRetentionReleaseWizard(models.TransientModel):
    _name = "l10n_ae.edi.retention.release.wizard"
    _description = "Release Reviewed UAE Retention Hold"

    hold_id = fields.Many2one(
        "l10n_ae.edi.retention.hold", required=True, readonly=True
    )
    reason = fields.Text(required=True)

    def action_confirm(self):
        self.ensure_one()
        self.env["l10n_ae.edi.retention"]._check_current_editor()
        self.with_user(self.env.uid).check_access("read")
        self.hold_id.action_release(self.reason)
        return {"type": "ir.actions.act_window_close"}
