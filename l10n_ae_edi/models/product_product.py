# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models


class ProductProduct(models.Model):
    _inherit = "product.product"

    l10n_ae_classification_code = fields.Char(
        string="UAE HS Code",
        copy=False,
        help="Reviewed HS identifier for this goods variant.",
    )
    l10n_ae_service_accounting_code = fields.Char(
        string="UAE Service Accounting Code",
        copy=False,
        help="Reviewed SAC identifier; not inferred from the product name or tax.",
    )
    l10n_ae_service_accounting_version = fields.Char(
        string="UAE SAC Scheme Version",
        copy=False,
        help="Version of the reviewed SAC scheme; no version is supplied by default.",
    )

    def _l10n_ae_item_classification(self):
        """Effective variant facts retained when the invoice is prepared.

        G/S derivation follows Odoo's public LGPL-3 draft PR262200; identifier
        retention and the explicit reviewed SAC version are KMEE extensions.
        """
        self.ensure_one()
        item_type = "S" if self.type == "service" else "G"
        return {
            "type": item_type,
            "hs_code": self.l10n_ae_classification_code or None
            if item_type == "G"
            else None,
            "sac_code": self.l10n_ae_service_accounting_code or None
            if item_type == "S"
            else None,
            "sac_scheme_version": self.l10n_ae_service_accounting_version or None
            if item_type == "S"
            else None,
        }
