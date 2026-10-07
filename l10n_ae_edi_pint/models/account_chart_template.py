# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import models
from odoo.addons.account.models.chart_template import template


class AccountChartTemplate(models.AbstractModel):
    _inherit = "account.chart.template"

    @template("ae", "account.tax")
    def _get_ae_kmee_pint_account_tax(self):
        # Public template API; metadata follows the LGPL-3 Odoo PR262200 table.
        data = self._parse_csv("ae", "account.tax", module="l10n_ae_edi_pint")
        for xmlid in list(data):
            tax = self.env.ref(
                f"account.{self.env.company.id}_{xmlid}", raise_if_not_found=False
            )
            # The native module-install loader forces updates. Omit populated
            # fields here so it cannot overwrite an existing reviewed choice.
            if tax and (
                tax.company_id != self.env.company or tax.ubl_cii_tax_category_code
            ):
                del data[xmlid]
        return data

    def _post_load_data(self, template_code, company, template_data):
        result = super()._post_load_data(template_code, company, template_data)
        if template_code == "ae":
            self._l10n_ae_fill_missing_pint_tax_categories(company)
        return result

    def _l10n_ae_fill_missing_pint_tax_categories(self, company):
        data = self.with_company(company)._get_ae_kmee_pint_account_tax()
        for xmlid, values in data.items():
            # Exact company XMLIDs: do not fall back to a parent's shared taxes.
            tax = self.env.ref(f"account.{company.id}_{xmlid}", raise_if_not_found=False)
            if tax and tax.company_id == company and not tax.ubl_cii_tax_category_code:
                tax.ubl_cii_tax_category_code = values["ubl_cii_tax_category_code"]
