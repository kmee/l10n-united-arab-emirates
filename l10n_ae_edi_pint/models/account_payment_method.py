# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models

UAE_PAYMENT_MEANS = [
    ("1", "Instrument not defined"),
    ("10", "In cash"),
    ("20", "Cheque"),
    ("21", "Banker's draft"),
    ("30", "Credit transfer"),
    ("49", "Direct debit"),
    ("54", "Credit card"),
    ("55", "Debit card"),
    ("68", "Online payment service"),
]


class AccountPaymentMethodLine(models.Model):
    _inherit = "account.payment.method.line"

    l10n_ae_edi_payment_means_code = fields.Selection(
        UAE_PAYMENT_MEANS,
        string="UAE Payment Instrument",
        help="Reviewed PINT-AE classification for this journal method. Leave blank "
        "to use supported native cash/manual or bank/manual derivation. This "
        "does not change the native payment method or execute a payment.",
    )
