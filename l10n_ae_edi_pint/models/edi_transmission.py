# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import models


class UaeEdiTransmission(models.Model):
    _inherit = "l10n_ae.edi.transmission"

    def _validate_controlled_document(self):
        self.ensure_one()
        report = self.move_id.validate_l10n_ae_pint()
        self.move_id._l10n_ae_check_pint_report(report)
        return report
