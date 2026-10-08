# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import UserError


class ResCurrencyRate(models.Model):
    _inherit = "res.currency.rate"

    l10n_ae_cbuae_review_id = fields.Many2one(
        "l10n_ae.cbuae.rate.review", readonly=True, copy=False, ondelete="restrict"
    )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for values in vals_list:
            if "l10n_ae_cbuae_review_id" in values:
                raise UserError(self.env._("Use the reviewed CBUAE import action."))
            prepared.append({**values, "l10n_ae_cbuae_review_id": False})
        return super().create(prepared)

    def write(self, vals):
        if "l10n_ae_cbuae_review_id" in vals or self.filtered(
            "l10n_ae_cbuae_review_id"
        ):
            raise UserError(
                self.env._("Imported CBUAE rates and provenance are immutable.")
            )
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_cbuae_import(self):
        if self.filtered("l10n_ae_cbuae_review_id"):
            raise UserError(self.env._("Imported CBUAE rates cannot be deleted."))

    def _create_cbuae_rate(self, review, values):
        return super().create({**values, "l10n_ae_cbuae_review_id": review.id})
