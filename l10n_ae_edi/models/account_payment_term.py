# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models

UAE_BILLING_FREQUENCIES = [
    ("DLY", "Daily"),
    ("WKY", "Weekly"),
    ("Q15", "Once in 15 days"),
    ("MTH", "Monthly"),
    ("Q45", "Once in 45 days"),
    ("Q60", "Once in 60 days"),
    ("QTR", "Quarterly"),
    ("YRL", "Yearly"),
    ("HYR", "Half-yearly"),
    ("OTH", "Other"),
]


class AccountPaymentTerm(models.Model):
    _inherit = "account.payment.term"

    l10n_ae_billing_frequency_code = fields.Selection(
        UAE_BILLING_FREQUENCIES,
        string="UAE Billing Frequency",
        help="Reviewed billing cadence for UAE documents. This does not change "
        "native installments or payment due dates.",
    )
