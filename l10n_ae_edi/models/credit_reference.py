# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json

from odoo import api, fields, models
from odoo.exceptions import UserError


class UaeCreditReference(models.Model):
    _name = "l10n_ae.credit.reference"
    _description = "UAE Credit Allocation to a Preceding Invoice"
    _check_company_auto = True
    _order = "id"

    move_id = fields.Many2one("account.move", required=True, ondelete="cascade")
    company_id = fields.Many2one(related="move_id.company_id", store=True)
    currency_id = fields.Many2one(related="move_id.currency_id")
    invoice_id = fields.Many2one(
        "account.move",
        string="Local Original Invoice",
        check_company=True,
        ondelete="restrict",
    )
    number = fields.Char(string="Original Invoice Number", required=True)
    issue_date = fields.Date(string="Original Issue Date", required=True)
    allocated_net = fields.Monetary()
    allocated_tax = fields.Monetary(string="Allocated VAT")
    review_evidence = fields.Char(
        string="External Original Review Evidence",
        help="Reference to reviewed external original invoices and their allocations.",
    )

    def _invoice_identity(self, invoice):
        invoice.check_access("read")
        if invoice.l10n_ae_transmission_id:
            snapshot = json.loads(invoice.l10n_ae_transmission_id.payload)
            return snapshot["invoice_number"], snapshot["issue_date"]
        return invoice.name or "/", invoice.invoice_date

    @api.onchange("invoice_id")
    def _onchange_invoice_id(self):
        if self.invoice_id:
            self.number, self.issue_date = self._invoice_identity(self.invoice_id)

    @api.model_create_multi
    def create(self, vals_list):
        moves = self.env["account.move"].browse(
            [vals["move_id"] for vals in vals_list if vals.get("move_id")]
        )
        self._check_editable(moves)
        for vals in vals_list:
            if vals.get("invoice_id"):
                invoice = self.env["account.move"].browse(vals["invoice_id"])
                vals["number"], vals["issue_date"] = self._invoice_identity(invoice)
        return super().create(vals_list)

    def _check_editable(self, moves):
        moves.check_access("write")
        protected = ["l10n_ae_transmission_id", "l10n_ae_received_document"]
        moves.flush_recordset(protected)
        if moves:
            self.env.cr.execute(
                "SELECT id FROM account_move WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                [moves.ids],
            )
            moves.invalidate_recordset(protected)
        if moves.filtered(
            lambda move: move.l10n_ae_transmission_id or move.l10n_ae_received_document
        ):
            raise UserError(
                self.env._("The frozen UAE credit references cannot be changed.")
            )

    def write(self, vals):
        moves = self.move_id
        if vals.get("move_id"):
            moves |= self.env["account.move"].browse(vals["move_id"])
        self._check_editable(moves)
        if vals.get("invoice_id"):
            invoice = self.env["account.move"].browse(vals["invoice_id"])
            vals["number"], vals["issue_date"] = self._invoice_identity(invoice)
        return super().write(vals)

    def unlink(self):
        self._check_editable(self.move_id)
        return super().unlink()
