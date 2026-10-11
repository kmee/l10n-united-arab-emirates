# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import models


class ResPartner(models.Model):
    _inherit = "res.partner"

    def button_account_peppol_check_partner_endpoint(self, company=None):
        self.ensure_one()
        partner = self.with_company(company or self.env.company)
        # Resolve the authorized company context before any early return. Merely
        # supplying an arbitrary company to this public action grants no access.
        company = partner.env.company
        company.check_access("read")
        partner.check_access("read")
        if company.account_fiscal_country_id.code == "AE" and not partner.env[
            "account.move.send"
        ]._is_applicable_to_company("peppol", company):
            return False
        return super().button_account_peppol_check_partner_endpoint(company=company)
