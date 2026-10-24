# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from io import BytesIO

from odoo import models


class IrActionsReport(models.Model):
    _inherit = "ir.actions.report"

    def _render_qweb_pdf_prepare_streams(self, report_ref, data, res_ids=None):
        report = self._get_report(report_ref)
        if (
            report.model == "account.move"
            and res_ids
            and len(res_ids) == 1
            and not data.get("proforma")
            and not self.env.context.get("proforma_invoice")
        ):
            invoice = self.env["account.move"].browse(res_ids)
            transmission = invoice.l10n_ae_transmission_id.with_context(bin_size=False)
            if (
                transmission.conventional_html_content
                and transmission.conventional_report_id == report
            ):
                invoice.action_l10n_ae_generate_conventional_pdf()
                return {
                    invoice.id: {
                        "stream": BytesIO(
                            transmission.conventional_pdf_content.content
                        ),
                        "attachment": None,
                    }
                }
        return super()._render_qweb_pdf_prepare_streams(
            report_ref, data, res_ids=res_ids
        )
