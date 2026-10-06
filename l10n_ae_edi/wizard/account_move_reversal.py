# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models

from ..models.account_move import UAE_TRANSACTION_FLAG_FIELDS


class AccountMoveReversal(models.TransientModel):
    _inherit = "account.move.reversal"

    l10n_ae_credit_reason = fields.Selection(
        selection=lambda self: self.env["account.move"].fields_get(
            ["l10n_ae_credit_reason"]
        )["l10n_ae_credit_reason"]["selection"],
        string="UAE Credit Note Reason",
        help="Official UAE reason to preserve on the resulting credit note.",
    )
    l10n_ae_credit_reason_applicable = fields.Boolean(
        compute="_compute_l10n_ae_credit_reason_applicable"
    )

    def _l10n_ae_credit_reason_applies(self, move):
        return (
            move.company_id.country_id.code == "AE"
            and not move.l10n_ae_received_document
            and (
                move.move_type == "out_invoice"
                or move.move_type == "in_invoice"
                and move.is_self_billing
            )
        )

    @api.depends(
        "move_ids.move_type",
        "move_ids.company_id.country_id",
        "move_ids.is_self_billing",
        "move_ids.l10n_ae_received_document",
    )
    def _compute_l10n_ae_credit_reason_applicable(self):
        for wizard in self:
            wizard.l10n_ae_credit_reason_applicable = any(
                wizard._l10n_ae_credit_reason_applies(move) for move in wizard.move_ids
            )

    def _prepare_default_reversal(self, move):
        values = super()._prepare_default_reversal(move)
        if self._l10n_ae_credit_reason_applies(move):
            values["l10n_ae_credit_reason"] = self.l10n_ae_credit_reason
            for name in (
                *UAE_TRANSACTION_FLAG_FIELDS,
                "l10n_ae_beneficiary_id",
                "l10n_ae_principal_id",
            ):
                values[name] = move[name]
        return values
