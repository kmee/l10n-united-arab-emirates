# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models


class CbuaePublication(models.Model):
    _name = "l10n_ae.cbuae.publication"
    _description = "CBUAE Dated Exchange Publication"
    _check_company_auto = True

    requested_date = fields.Date(required=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        readonly=True,
    )
    state = fields.Selection(
        [("draft", "Not Captured"), ("captured", "Requires Review")],
        default="draft",
        required=True,
        readonly=True,
        copy=False,
    )
    source_url = fields.Char(readonly=True, copy=False)
    retrieved_at = fields.Datetime(readonly=True, copy=False)
    http_status = fields.Integer(readonly=True, copy=False)
    content_type = fields.Char(readonly=True, copy=False)
    source_body = fields.Binary(readonly=True, copy=False, attachment=False)
    source_sha256 = fields.Char(readonly=True, copy=False)
    publication_stamp = fields.Char(readonly=True, copy=False)
    publication_date = fields.Date(readonly=True, copy=False)
    quotes = fields.Json(readonly=True, copy=False)

    def action_capture(self):
        return True
