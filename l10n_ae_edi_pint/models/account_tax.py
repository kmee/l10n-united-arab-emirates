# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models

# Official PINT-AE AE-Exempt list; these are UAE legal references, not EU VATEX.
# https://docs.peppol.eu/poac/ae/pint-ae/trn-invoice/codelist/Aligned-TaxExemptionCodes/
UAE_EXEMPTION_REASONS = [
    ("DL8.46.1", "Certain financial services"),
    ("DL8.46.2", "Supply of residential units (lease or sale)"),
    ("DL8.46.3", "Bare land"),
    ("DL8.46.4", "Local passenger transport"),
]


class AccountTax(models.Model):
    _inherit = "account.tax"

    ubl_cii_tax_exemption_reason_code = fields.Selection(
        selection_add=UAE_EXEMPTION_REASONS,
        ondelete={code: "set null" for code, _label in UAE_EXEMPTION_REASONS},
    )
