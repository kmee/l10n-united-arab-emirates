# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json
from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_ae_advance_reference_summary = fields.Text(
        string="Taxed Advance References",
        compute="_compute_l10n_ae_advance_reference_summary",
    )

    def _l10n_ae_advance_references(self):
        self.ensure_one()
        if self.l10n_ae_transmission_id:
            return json.loads(self.l10n_ae_transmission_id.payload).get(
                "taxed_advances", []
            )
        if (
            self.company_id.country_code != "AE"
            or self.move_type != "out_invoice"
            or self.l10n_ae_received_document
        ):
            return []
        references = {}
        for line in self.invoice_line_ids.filtered(
            lambda item: item.is_downpayment and item.quantity < 0
        ):
            sources = (
                line._get_downpayment_lines()
                .filtered(
                    lambda item: (
                        item.move_id.move_type == "out_invoice" and item.quantity > 0
                    )
                )
                .move_id
            )
            if len(sources) != 1:
                raise UserError(
                    self.env._(
                        "Review the original taxed advance linked to this deduction."
                    )
                )
            source = sources
            if source.state != "posted":
                raise UserError(
                    self.env._(
                        "The original taxed advance must be posted "
                        "before this deduction."
                    )
                )
            if source.commercial_partner_id != self.commercial_partner_id:
                raise UserError(
                    self.env._(
                        "The taxed advance must belong to the same commercial customer."
                    )
                )
            identity = (
                json.loads(source.l10n_ae_transmission_id.payload)
                if source.l10n_ae_transmission_id
                else {
                    "invoice_number": source.name,
                    "issue_date": fields.Date.to_string(source.invoice_date),
                }
            )
            reference = references.setdefault(
                source.id,
                {
                    "source_move_id": source.id,
                    "number": identity["invoice_number"],
                    "issue_date": identity["issue_date"],
                    "consumed_net": Decimal(0),
                    "consumed_vat": Decimal(0),
                },
            )
            reference["consumed_net"] -= Decimal(str(line.price_subtotal))
            reference["consumed_vat"] -= Decimal(str(line.price_total)) - Decimal(
                str(line.price_subtotal)
            )
        scale = Decimal(1).scaleb(-self.currency_id.decimal_places)
        for reference in references.values():
            source = self.env["account.move"].browse(reference["source_move_id"])
            source_totals = (
                json.loads(source.l10n_ae_transmission_id.payload)["totals"]
                if source.l10n_ae_transmission_id
                else {"net": source.amount_untaxed, "vat": source.amount_tax}
            )
            if reference["consumed_net"] > Decimal(
                str(source_totals["net"])
            ) or reference["consumed_vat"] > Decimal(str(source_totals["vat"])):
                raise UserError(
                    self.env._(
                        "The deduction exceeds the original taxed advance. "
                        "Review the invoice lines."
                    )
                )
            for name in ("consumed_net", "consumed_vat"):
                reference[name] = str(reference[name].quantize(scale))
        return list(references.values())

    def action_l10n_ae_validate(self):
        result = super().action_l10n_ae_validate()
        for move in self:
            move._l10n_ae_advance_references()
        return result

    def _l10n_ae_snapshot(self):
        snapshot = super()._l10n_ae_snapshot()
        snapshot["taxed_advances"] = self._l10n_ae_advance_references()
        return snapshot

    @api.depends(
        "l10n_ae_transmission_id",
        "invoice_line_ids.sale_line_ids",
        "invoice_line_ids.quantity",
        "invoice_line_ids.price_subtotal",
        "invoice_line_ids.price_total",
    )
    def _compute_l10n_ae_advance_reference_summary(self):
        for move in self:
            try:
                references = move._l10n_ae_advance_references()
                move.l10n_ae_advance_reference_summary = "\n".join(
                    f"{ref['number']} | {ref['issue_date']} | "
                    f"{self.env._('Net')}: {ref['consumed_net']} | "
                    f"{self.env._('VAT')}: {ref['consumed_vat']} "
                    f"{move.currency_id.name}"
                    for ref in references
                )
            except UserError as error:
                move.l10n_ae_advance_reference_summary = str(error)
