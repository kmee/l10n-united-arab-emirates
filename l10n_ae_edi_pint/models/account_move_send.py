# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import models


class AccountMoveSend(models.AbstractModel):
    _inherit = "account.move.send"

    def _hook_invoice_document_before_pdf_report_render(self, invoice, invoice_data):
        result = super()._hook_invoice_document_before_pdf_report_render(
            invoice, invoice_data
        )
        transmission = invoice.l10n_ae_transmission_id.with_context(bin_size=False)
        if transmission.conventional_html_content and invoice.invoice_pdf_report_id:
            invoice.action_l10n_ae_generate_conventional_pdf()
            content = transmission.conventional_pdf_content.content
            if invoice.invoice_pdf_report_id.raw.content != content:
                invoice_data["pdf_attachment_values"] = {
                    "name": transmission.conventional_filename,
                    "raw": content,
                    "mimetype": "application/pdf",
                    "res_model": invoice._name,
                    "res_id": invoice.id,
                    "res_field": "invoice_pdf_report_file",
                }
        return result

    def _link_invoice_documents(self, invoices_data):
        for invoice, invoice_data in invoices_data.items():
            if (
                invoice.l10n_ae_transmission_id.conventional_html_content
                and invoice_data.get("pdf_attachment_values")
                and invoice.invoice_pdf_report_id
            ):
                # Keep the earlier file as history, outside the native binary slot.
                invoice.check_access("write")
                invoice.invoice_pdf_report_id.write({"res_field": False})
        return super()._link_invoice_documents(invoices_data)

    def _hook_invoice_document_after_pdf_report_render(self, invoice, invoice_data):
        # Native UBL/CII postprocessing embeds a freshly rebuilt Factur-X document.
        # The preserved conventional PDF must remain the original issue's bytes.
        if invoice.l10n_ae_transmission_id.conventional_html_content:
            return
        return super()._hook_invoice_document_after_pdf_report_render(
            invoice, invoice_data
        )
