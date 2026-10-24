# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    invoice_edi_format = fields.Selection(
        selection_add=[("kmee_ae_pint", "UAE PINT-AE")],
    )

    @api.model
    def _get_ubl_cii_formats_info(self):
        return {
            **super()._get_ubl_cii_formats_info(),
            "kmee_ae_pint": {
                "countries": [],
                "on_peppol": False,
                "embed_attachments": False,
            },
        }

    @api.model
    def _get_edi_builder(self, invoice_edi_format):
        if invoice_edi_format == "kmee_ae_pint":
            return self.env["account.edi.ubl.kmee_ae"]
        return super()._get_edi_builder(invoice_edi_format)
