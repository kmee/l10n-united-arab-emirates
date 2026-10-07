# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).


def post_init_hook(env):
    """Complete missing native metadata after first installation on existing charts."""
    for company in env["res.company"].search([("chart_template", "=", "ae")]):
        env["account.chart.template"].with_company(
            company
        )._l10n_ae_fill_missing_pint_tax_categories(company)
