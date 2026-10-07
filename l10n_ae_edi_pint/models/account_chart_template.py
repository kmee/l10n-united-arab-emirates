# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import models
from odoo.addons.account.models.chart_template import template


class AccountChartTemplate(models.AbstractModel):
    _inherit = "account.chart.template"

    @template("ae", "account.tax")
    def _get_ae_kmee_pint_account_tax(self):
        # Public template API; metadata follows the LGPL-3 Odoo PR262200 table.
        return self._parse_csv("ae", "account.tax", module="l10n_ae_edi_pint")
