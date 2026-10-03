# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
{
    "name": "UAE Electronic Invoicing",
    "summary": "Validate UAE identity and freeze electronic invoice evidence",
    "version": "20.0.1.0.0",
    "development_status": "Alpha",
    "category": "Accounting/Localizations",
    "author": "KMEE",
    "website": "https://github.com/kmee/l10n-eua",
    "license": "AGPL-3",
    "depends": ["l10n_ae", "account_edi"],
    "data": [
        "security/ir.access.csv",
        "security/edi_security.xml",
        "views/res_company_views.xml",
        "views/account_move_views.xml",
    ],
    "installable": True,
}
