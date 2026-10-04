# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import models


class AccountMoveSend(models.AbstractModel):
    _inherit = "account.move.send"

    def _hook_invoice_document_after_pdf_report_render(self, invoice, invoice_data):
        # Native UBL/CII postprocessing embeds a freshly rebuilt Factur-X document.
        # The preserved conventional PDF must remain the original issue's bytes.
        if invoice.l10n_ae_transmission_id.conventional_html_content:
            return
        return super()._hook_invoice_document_after_pdf_report_render(
            invoice, invoice_data
        )
