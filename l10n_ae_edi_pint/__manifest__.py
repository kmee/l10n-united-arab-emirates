# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
{
    "name": "UAE PINT Electronic Invoicing",
    "summary": "Extend the public Odoo PINT builder for UAE invoices",
    "version": "20.0.1.3.0",
    "development_status": "Alpha",
    "category": "Accounting/Localizations",
    "author": "KMEE",
    "website": "https://github.com/kmee/l10n-eua",
    "license": "AGPL-3",
    "depends": ["l10n_ae_edi", "account_edi_ubl_cii"],
    "data": [
        "security/inbox_groups.xml",
        "security/ir.access.csv",
        "security/inbox_security.xml",
        "views/account_move_views.xml",
        "views/account_move_send_wizard_views.xml",
        "views/edi_inbox_views.xml",
        "views/edi_audit_inbox_views.xml",
    ],
    "external_dependencies": {
        "python": ["packaging", "lxml>=5.2,<7", "saxonche>=13.0,<14"]
    },
    "installable": True,
}
