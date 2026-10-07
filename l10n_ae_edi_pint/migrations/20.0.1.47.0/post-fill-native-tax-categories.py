# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Fill absent native categories when upgrading an existing UAE PINT installation."""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for company in env["res.company"].search([("chart_template", "=", "ae")]):
        env["account.chart.template"].with_company(
            company
        )._l10n_ae_fill_missing_pint_tax_categories(company)
