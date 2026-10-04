# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import models
from odoo.exceptions import UserError


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    def _l10n_ae_fiscal_line_fields(self):
        return super()._l10n_ae_fiscal_line_fields() | {
            "sale_line_ids",
            "is_downpayment",
        }

    def write(self, vals):
        consumption_fields = {"quantity", "price_unit", "discount"}.intersection(vals)
        if consumption_fields:
            self.check_access("write")
            moves = self.filtered(
                lambda line: (
                    line.move_id.state == "posted"
                    and line.company_id.country_code == "AE"
                    and line.move_id.move_type == "out_invoice"
                    and line.is_downpayment
                    and line.quantity < 0
                    and any(
                        self.env["account.move"]._field_will_change(line, vals, name)
                        for name in consumption_fields
                    )
                )
            ).move_id
            moves._l10n_ae_lock_advance_sources()
            if moves:
                moves.check_access("write")
                moves.flush_recordset()
                self.env.cr.execute(
                    "SELECT id FROM account_move "
                    "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                    [moves.ids],
                )
                moves.invalidate_recordset()
                if moves.filtered(
                    lambda move: (
                        move.l10n_ae_transmission_id or move.l10n_ae_received_document
                    )
                ):
                    raise UserError(
                        self.env._("The frozen UAE deduction lines cannot be changed.")
                    )
                moves.write({"write_date": self.env.cr.now()})
                moves.flush_recordset(["write_uid", "write_date"])
        return super().write(vals)
