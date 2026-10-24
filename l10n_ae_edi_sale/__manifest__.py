# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
{
    "name": "UAE Electronic Invoicing Sales",
    "summary": "Preserve taxed advance references on final sales invoices",
    "version": "20.0.1.0.0",
    "development_status": "Alpha",
    "category": "Accounting/Localizations",
    "author": "KMEE, Odoo Community Association (OCA)",
    "maintainers": ["mileo"],
    "website": "https://github.com/kmee/l10n-united-arab-emirates",
    "license": "AGPL-3",
    "depends": ["l10n_ae_edi_pint", "sale"],
    "auto_install": True,
    "data": ["views/account_move_views.xml"],
    "installable": True,
}
