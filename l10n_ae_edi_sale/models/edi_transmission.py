# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json

from odoo import api, fields, models
from odoo.exceptions import UserError


class UaeEdiTransmission(models.Model):
    _inherit = "l10n_ae.edi.transmission"

    l10n_ae_advance_source_ids = fields.Many2many(
        "account.move",
        "l10n_ae_advance_transmission_rel",
        "transmission_id",
        "source_move_id",
        string="Taxed Advance Sources",
        compute="_compute_l10n_ae_advance_source_ids",
        store=True,
        readonly=True,
        copy=False,
    )

    @api.depends("payload")
    def _compute_l10n_ae_advance_source_ids(self):
        for transmission in self:
            snapshot = json.loads(transmission.payload or "{}")
            transmission.l10n_ae_advance_source_ids = self.env["account.move"].browse(
                [ref["source_move_id"] for ref in snapshot.get("taxed_advances", [])]
            )

    def write(self, vals):
        if "l10n_ae_advance_source_ids" in vals:
            raise UserError(self.env._("Frozen UAE advance references are immutable."))
        return super().write(vals)
