# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models


class AccountMoveSendWizard(models.TransientModel):
    _inherit = "account.move.send.wizard"

    l10n_ae_send_format = fields.Selection(
        selection=[
            ("default", "Customer default"),
            ("pint", "PINT-AE"),
            ("pdf_only", "PDF only"),
        ],
        string="Format for this Send",
        default="default",
        required=True,
        help="This choice applies only to this Send. It does not change the customer "
        "default. A conventional PDF does not replace a required electronic invoice.",
    )
    l10n_ae_can_choose_send_format = fields.Boolean(
        compute="_compute_l10n_ae_can_choose_send_format",
    )

    @api.depends(
        "move_id.country_code",
        "move_id.company_id.account_fiscal_country_id",
        "move_id.move_type",
        "move_id.is_self_billing",
        "move_id.l10n_ae_received_document",
        "move_id.l10n_ae_transmission_id",
    )
    def _compute_l10n_ae_can_choose_send_format(self):
        for wizard in self:
            move = wizard.move_id
            wizard.l10n_ae_can_choose_send_format = bool(
                move.country_code == "AE"
                and not move.l10n_ae_transmission_id
                and not move.l10n_ae_received_document
                and (
                    move.move_type in ("out_invoice", "out_refund")
                    or move.is_self_billing
                    and move.move_type in ("in_invoice", "in_refund")
                )
            )

    @api.depends(
        "move_id",
        "sending_methods",
        "l10n_ae_send_format",
        "l10n_ae_can_choose_send_format",
    )
    def _compute_invoice_edi_format(self):
        result = super()._compute_invoice_edi_format()
        for wizard in self:
            if wizard.l10n_ae_can_choose_send_format:
                if wizard.l10n_ae_send_format == "pint":
                    wizard.invoice_edi_format = "kmee_ae_pint"
                elif wizard.l10n_ae_send_format == "pdf_only":
                    wizard.invoice_edi_format = False
        return result

    @api.depends(
        "sending_methods", "extra_edis", "mail_partner_ids", "invoice_edi_format"
    )
    def _compute_alerts(self):
        return super()._compute_alerts()
