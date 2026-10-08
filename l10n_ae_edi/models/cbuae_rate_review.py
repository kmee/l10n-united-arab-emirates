# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models
from odoo.exceptions import UserError


class CbuaeRateReview(models.Model):
    _name = "l10n_ae.cbuae.rate.review"
    _description = "CBUAE Reviewed Native Currency Rate"
    _rec_name = "source_label"
    _check_company_auto = True

    publication_id = fields.Many2one(
        "l10n_ae.cbuae.publication",
        required=True,
        ondelete="restrict",
        check_company=True,
    )
    company_id = fields.Many2one(
        related="publication_id.company_id", store=True, readonly=True
    )
    source_label = fields.Char(required=True)
    currency_id = fields.Many2one("res.currency", required=True)
    review_note = fields.Text(required=True)
    native_rate_id = fields.Many2one("res.currency.rate", readonly=True, copy=False)
    quote_literal = fields.Char(readonly=True, copy=False)
    reviewed_by = fields.Many2one("res.users", readonly=True, copy=False)
    reviewed_at = fields.Datetime(readonly=True, copy=False)
    anchor_rate_id = fields.Many2one("res.currency.rate", readonly=True, copy=False)
    anchor_rate_literal = fields.Char(readonly=True, copy=False)

    def action_import(self):
        raise UserError(self.env._("Reviewed CBUAE rate import is not implemented."))
