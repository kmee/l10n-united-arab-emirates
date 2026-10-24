# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models


class UaeEdiCorrectionWizard(models.TransientModel):
    _name = "l10n_ae.edi.correction.wizard"
    _description = "Correct UAE Invoice Before Sending"

    move_id = fields.Many2one("account.move", required=True, readonly=True)
    source_transmission_id = fields.Many2one(
        "l10n_ae.edi.transmission",
        required=True,
        readonly=True,
    )
    reason = fields.Text(
        required=True, help="Explain the correction; retained in the invoice history."
    )

    def action_confirm(self):
        self.ensure_one()
        return self.move_id._start_l10n_ae_local_correction(
            self.source_transmission_id, self.reason
        )
