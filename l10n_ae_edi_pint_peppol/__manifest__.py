# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
{
    "name": "UAE PINT Native Peppol Coexistence",
    "summary": "Keep UAE native Send accessible when Odoo Peppol is inapplicable",
    "version": "20.0.1.0.0",
    "development_status": "Alpha",
    "category": "Accounting/Localizations",
    "author": "KMEE, Odoo Community Association (OCA)",
    "maintainers": ["mileo"],
    "website": "https://github.com/kmee/l10n-united-arab-emirates",
    "license": "AGPL-3",
    "depends": ["l10n_ae_edi_pint", "account_peppol"],
    "data": ["views/res_partner_views.xml"],
    "auto_install": True,
    "installable": True,
}
