# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import models
from odoo.exceptions import UserError

from odoo.addons.l10n_ae_edi.tools.errors import UaeDocumentValidationError


class UaeEdiTransmission(models.Model):
    _inherit = "l10n_ae.edi.transmission"

    def _validate_controlled_document(self):
        self.ensure_one()
        # Resource/configuration and access failures remain outside this mapping.
        report = self.move_id.validate_l10n_ae_pint()
        try:
            self.move_id._l10n_ae_check_pint_report(report)
        except UserError as error:
            if report["valid"] or error.__class__ is not UserError:
                raise
            raise UaeDocumentValidationError(str(error)) from error
        return report
