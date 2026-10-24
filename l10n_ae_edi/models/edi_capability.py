# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models


class UaeEdiCapability(models.Model):
    _name = "l10n_ae.edi.capability"
    _description = "Controlled UAE Provider Contract Capability"
    _order = "code"

    name = fields.Char(required=True, translate=True)
    code = fields.Selection(
        [
            ("submit", "Submit an Electronic Document"),
            ("lookup", "Reconcile an Existing Submission"),
            ("report", "Retrieve Separate Fiscal Reporting Facts"),
            ("receive", "Retrieve Incoming Documents"),
            ("billing_invoice", "Issue Billing Invoices"),
            ("billing_credit", "Issue Billing Credit Notes"),
            ("selfbilling_invoice", "Issue Self-Billed Invoices"),
            ("selfbilling_credit", "Issue Self-Billed Credit Notes"),
        ],
        required=True,
    )
    _unique_code = models.Constraint(
        "UNIQUE(code)", "Contract capability codes are unique."
    )
