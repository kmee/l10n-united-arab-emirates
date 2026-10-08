# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models


class ResCurrencyRate(models.Model):
    _inherit = "res.currency.rate"

    l10n_ae_cbuae_review_id = fields.Many2one(
        "l10n_ae.cbuae.rate.review", readonly=True, copy=False, ondelete="restrict"
    )

    def _create_cbuae_rate(self, review, values):
        return super().create({**values, "l10n_ae_cbuae_review_id": review.id})
