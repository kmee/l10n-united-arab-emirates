# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import hashlib

from odoo import api, models


class AccountMoveSend(models.AbstractModel):
    _inherit = "account.move.send"

    @api.model
    def _get_default_invoice_edi_format(self, move, **kwargs):
        if move.l10n_ae_transmission_id:
            return "kmee_ae_pint"
        return super()._get_default_invoice_edi_format(move, **kwargs)

    @api.model
    def _get_invoice_extra_attachments(self, move):
        attachments = super()._get_invoice_extra_attachments(move)
        transmission = move.l10n_ae_transmission_id
        if transmission:
            xml = move.preview_l10n_ae_pint().encode("utf-8")
            if move.ubl_cii_xml_id and move.ubl_cii_xml_id.raw.content != xml:
                attachments -= move.ubl_cii_xml_id
            if transmission.conventional_html_content and move.invoice_pdf_report_id:
                content = move.invoice_pdf_report_id.raw.content
                if (
                    hashlib.sha256(content).hexdigest()
                    != transmission.conventional_pdf_hash
                ):
                    attachments -= move.invoice_pdf_report_id
        return attachments

    @api.model
    def _get_placeholder_mail_attachments_data(
        self, move, invoice_edi_format=None, extra_edis=None, pdf_report=None
    ):
        result = super()._get_placeholder_mail_attachments_data(
            move,
            invoice_edi_format=invoice_edi_format,
            extra_edis=extra_edis,
            pdf_report=pdf_report,
        )
        transmission = move.l10n_ae_transmission_id
        if transmission:
            attachments = self._get_invoice_extra_attachments(move)
            expected = []
            if transmission.conventional_html_content and (
                not move.invoice_pdf_report_id
                or move.invoice_pdf_report_id not in attachments
            ):
                expected.append((transmission.conventional_filename, "application/pdf"))
            if not move.ubl_cii_xml_id or move.ubl_cii_xml_id not in attachments:
                expected.append(
                    (
                        move._get_invoice_report_filename(extension="xml"),
                        "application/xml",
                    )
                )
            for filename, mimetype in expected:
                if not any(item["mimetype"] == mimetype for item in result):
                    result.append(
                        {
                            "id": f"placeholder_{filename}",
                            "name": filename,
                            "mimetype": mimetype,
                            "placeholder": True,
                        }
                    )
        return result

    def _hook_invoice_document_before_pdf_report_render(self, invoice, invoice_data):
        transmission = invoice.l10n_ae_transmission_id.with_context(bin_size=False)
        frozen_xml = (
            invoice.preview_l10n_ae_pint().encode("utf-8") if transmission else None
        )
        # Keep the native hooks, without rebuilding an already preserved XML.
        core_data = (
            {**invoice_data, "invoice_edi_format": False}
            if transmission
            else invoice_data
        )
        result = super()._hook_invoice_document_before_pdf_report_render(
            invoice, core_data
        )
        if transmission:
            invoice_data.update(core_data)
            invoice_data["invoice_edi_format"] = "kmee_ae_pint"
            if (
                not invoice.ubl_cii_xml_id
                or invoice.ubl_cii_xml_id.raw.content != frozen_xml
            ):
                invoice_data["ubl_cii_xml_attachment_values"] = {
                    "name": invoice._get_invoice_report_filename(extension="xml"),
                    "raw": frozen_xml,
                    "mimetype": "application/xml",
                    "res_model": invoice._name,
                    "res_id": invoice.id,
                    "res_field": "ubl_cii_xml_file",
                }
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
                invoice.l10n_ae_transmission_id
                and invoice_data.get("ubl_cii_xml_attachment_values")
                and invoice.ubl_cii_xml_id
            ):
                invoice.check_access("write")
                invoice.ubl_cii_xml_id.write({"res_field": False})
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
        if invoice.l10n_ae_transmission_id:
            return
        return super()._hook_invoice_document_after_pdf_report_render(
            invoice, invoice_data
        )
