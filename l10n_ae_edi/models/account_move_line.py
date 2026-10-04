# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, models
from odoo.exceptions import UserError


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

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
        }

    def _l10n_ae_check_fiscal_line_edit(self, moves):
        moves = moves.filtered(lambda move: move.company_id.country_code == "AE")
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
        if fiscal_fields:
            self.check_access("write")
            changed = self.filtered(
                lambda line: any(
                    self.env["account.move"]._field_will_change(line, vals, name)
                    for name in fiscal_fields
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
