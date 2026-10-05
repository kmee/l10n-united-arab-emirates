# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
{
    "name": "UAE Electronic Invoicing",
    "summary": "Validate UAE identity and freeze electronic invoice evidence",
    "version": "20.0.1.10.0",
    "development_status": "Alpha",
    "category": "Accounting/Localizations",
    "author": "KMEE",
    "website": "https://github.com/kmee/l10n-eua",
    "license": "AGPL-3",
    "depends": ["l10n_ae", "account_edi"],
    "data": [
        "security/edi_groups.xml",
        "security/ir.access.csv",
        "security/edi_security.xml",
        "data/ir_cron.xml",
        "views/edi_binding_views.xml",
        "views/edi_audit_views.xml",
        "views/edi_retention_views.xml",
        "views/selfbilling_agreement_views.xml",
        "views/res_company_views.xml",
        "views/account_move_views.xml",
        "views/account_move_reversal_views.xml",
        "wizard/edi_correction_views.xml",
        "wizard/edi_retention_views.xml",
    ],
    "installable": True,
}
