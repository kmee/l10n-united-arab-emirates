# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import models


class BaseDocumentLayout(models.TransientModel):
    _inherit = "base.document.layout"

    def _extract_company_layout_vals(self, vals):
        companies = (
            self.env["res.company"].browse(vals["company_id"])
            if vals.get("company_id")
            else self.company_id
        )
        result = super()._extract_company_layout_vals(vals)
        # Native invoice onboarding resubmits the current VAT. Rewriting it
        # recomputes routing from the full TRN, discarding a reviewed UAE TIN.
        # Keep genuine VAT edits and the core's company/access checks intact.
        if (
            "vat" in result
            and companies
            and all(
                company.account_fiscal_country_id.code == "AE"
                and company.vat == result["vat"]
                for company in companies
            )
        ):
            result.pop("vat")
        return result
