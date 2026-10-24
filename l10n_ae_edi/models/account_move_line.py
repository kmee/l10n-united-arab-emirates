# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import UserError

from .product_product import UAE_RCM_NATURE_CODES


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    l10n_ae_edi_rcm_nature_code = fields.Selection(
        selection=UAE_RCM_NATURE_CODES,
        string="UAE RCM Nature Override",
        help="Reviewed AE item nature for this line; overrides the product's nature. "
        "No goods or service classification is inferred for a line without a product.",
    )
    l10n_ae_edi_rcm_standard_id = fields.Char(
        string="UAE RCM Standard ID",
        help="Reviewed scheme 0160 identifier for this AE item; overrides the product "
        "barcode. No identifier is generated from its description.",
    )

    def _l10n_ae_item_classification(self):
        self.ensure_one()
        classification = (
            self.product_id._l10n_ae_item_classification() if self.product_id else {}
        )
        if self.l10n_ae_edi_rcm_nature_code:
            classification["rcm_nature_code"] = self.l10n_ae_edi_rcm_nature_code
        if self.l10n_ae_edi_rcm_standard_id:
            classification["rcm_standard_id"] = self.l10n_ae_edi_rcm_standard_id
        return classification

    def _l10n_ae_fiscal_line_fields(self):
        return {
            "price_unit",
            "quantity",
            "discount",
            "name",
            "product_id",
            "product_uom_id",
            "tax_ids",
            "currency_id",
            "display_type",
            "balance",
            "amount_currency",
            "move_id",
            "l10n_ae_edi_rcm_nature_code",
            "l10n_ae_edi_rcm_standard_id",
        }

    def _l10n_ae_check_fiscal_line_edit(self, moves):
        moves = moves.filtered(
            lambda move: (
                move.country_code == "AE" and move.is_invoice(include_receipts=True)
            )
        )
        if not moves:
            return
        moves.check_access("write")
        moves.flush_recordset(["l10n_ae_transmission_id"])
        self.env.cr.execute(
            "SELECT id FROM account_move WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [moves.ids],
        )
        moves.invalidate_recordset(["l10n_ae_transmission_id"])
        if moves.filtered("l10n_ae_transmission_id"):
            raise UserError(
                self.env._("The frozen UAE invoice lines cannot be changed.")
            )
        moves.write({"write_date": self.env.cr.now()})
        moves.flush_recordset(["write_uid", "write_date"])

    def write(self, vals):
        fiscal_fields = self._l10n_ae_fiscal_line_fields().intersection(vals)
        native_period_fields = {
            "deferred_start_date",
            "deferred_end_date",
        }.intersection(vals)
        if fiscal_fields or native_period_fields:
            self.check_access("write")
            changed = self.filtered(
                lambda line: any(
                    self.env["account.move"]._field_will_change(line, vals, name)
                    for name in fiscal_fields
                )
            )
            if native_period_fields:
                changed |= self.filtered(
                    lambda line: (
                        line.display_type in (False, "product")
                        and line.move_id._l10n_ae_uses_native_period_dates()
                        and any(
                            self.env["account.move"]._field_will_change(
                                line, vals, name
                            )
                            for name in native_period_fields
                        )
                    )
                )
            moves = changed.move_id
            if changed and vals.get("move_id"):
                moves |= self.env["account.move"].browse(vals["move_id"])
            self._l10n_ae_check_fiscal_line_edit(moves)
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        self.check_access("create")
        moves = self.env["account.move"].browse(
            [vals["move_id"] for vals in vals_list if vals.get("move_id")]
        )
        self._l10n_ae_check_fiscal_line_edit(moves)
        return super().create(vals_list)

    def unlink(self):
        self.check_access("unlink")
        self._l10n_ae_check_fiscal_line_edit(self.move_id)
        return super().unlink()
