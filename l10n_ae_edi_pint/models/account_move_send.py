# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import hashlib
import re

from odoo import api, models
from odoo.exceptions import AccessError, MissingError, UserError


class AccountMoveSend(models.AbstractModel):
    _inherit = "account.move.send"

    @api.model
    def _get_alerts(self, moves, moves_data):
        alerts = super()._get_alerts(moves, moves_data)
        moves.check_access("read")
        groups = {}
        for move in moves.filtered(
            lambda invoice: (
                invoice.company_id.country_id.code == "AE"
                and not invoice.l10n_ae_transmission_id
                and not invoice.l10n_ae_received_document
                and (
                    invoice.move_type in ("out_invoice", "out_refund")
                    or invoice.is_self_billing
                    and invoice.move_type in ("in_invoice", "in_refund")
                )
            )
        ):
            try:
                move.action_l10n_ae_validate()
            except (AccessError, MissingError):
                raise
            except UserError as error:
                message = str(error)
                rule = re.search(r"\bIBR-\d{3}-AE\b", message, re.IGNORECASE)
                key = (
                    rule.group().lower().replace("-", "_")
                    if rule
                    else hashlib.sha256(message.encode("utf-8")).hexdigest()
                )
                group = groups.setdefault(
                    key, {"moves": self.env["account.move"], "messages": []}
                )
                group["moves"] |= move
                if message not in group["messages"]:
                    group["messages"].append(message)
        for key, group in groups.items():
            alerts["l10n_ae_edi_" + key] = {
                # PDF-only Generate is still available. The formatter retains
                # its mandatory checks if PINT-AE is selected for generation.
                "level": "warning",
                "message": "\n".join(group["messages"]),
                "action_text": self.env._("View Invoices"),
                "action": group["moves"]._get_records_action(),
            }
        return alerts

    @api.model
    def _get_default_sending_settings(self, move, from_cron=False, **custom_settings):
        settings = super()._get_default_sending_settings(
            move, from_cron=from_cron, **custom_settings
        )
        # Check effective explicit/default/cron settings before generation starts.
        # An already frozen fiscal issue cannot silently change Send's format.
        if move.l10n_ae_transmission_id and settings.get("invoice_edi_format") not in (
            None,
            False,
            "kmee_ae_pint",
        ):
            raise UserError(
                self.env._(
                    "The selected eInvoice format conflicts with "
                    "the frozen UAE PINT-AE issue."
                )
            )
        if (
            not move.l10n_ae_transmission_id
            and settings.get("invoice_edi_format") == "kmee_ae_pint"
        ):
            # The warning does not certify eligibility. Native generation must
            # retain the same local refusals as the explicit Check/Prepare path.
            move.action_l10n_ae_validate()
        return settings

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
