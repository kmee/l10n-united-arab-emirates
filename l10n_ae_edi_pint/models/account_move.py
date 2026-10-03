# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import models
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = "account.move"

    def preview_l10n_ae_pint(self):
        """Preview XML without submitting or claiming formal conformance."""
        self.ensure_one()
        self.action_l10n_ae_validate()
        content, errors = self.env["account.edi.ubl.kmee_ae"]._export_invoice(self)
        if errors:
            raise UserError("\n".join(sorted(errors)))
        return content.decode("utf-8")

    def _l10n_ae_prepare_xml(self):
        return self.preview_l10n_ae_pint()
