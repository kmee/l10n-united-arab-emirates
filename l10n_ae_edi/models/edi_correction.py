# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import UserError


class UaeEdiCorrection(models.Model):
    _name = "l10n_ae.edi.correction"
    _description = "UAE Local Invoice Correction"
    _rec_name = "reason"
    _check_company_auto = True

    move_id = fields.Many2one(
        "account.move", required=True, readonly=True, ondelete="restrict"
    )
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    source_transmission_id = fields.Many2one(
        "l10n_ae.edi.transmission",
        required=True,
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )
    replacement_transmission_id = fields.Many2one(
        "l10n_ae.edi.transmission",
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )
    reason = fields.Text(required=True, readonly=True)
    started_by = fields.Many2one(
        "res.users", required=True, readonly=True, ondelete="restrict"
    )
    started_at = fields.Datetime(required=True, readonly=True)
    completed_at = fields.Datetime(readonly=True)
    state = fields.Selection(
        [("editing", "Editing"), ("completed", "Completed")],
        compute="_compute_state",
    )

    @api.depends("replacement_transmission_id")
    def _compute_state(self):
        for correction in self:
            correction.state = (
                "completed" if correction.replacement_transmission_id else "editing"
            )

    _one_correction_per_source = models.UniqueIndex(
        "(source_transmission_id)", "This UAE issue already has a local correction."
    )

    @api.model
    def _begin(self, move, source, reason):
        return super().create(
            {
                "move_id": move.id,
                "company_id": move.company_id.id,
                "source_transmission_id": source.id,
                "reason": reason,
                "started_by": self.env.uid,
                "started_at": fields.Datetime.now(),
            }
        )

    def _complete(self, replacement):
        self.ensure_one()
        return super().write(
            {
                "replacement_transmission_id": replacement.id,
                "completed_at": fields.Datetime.now(),
            }
        )

    @api.model_create_multi
    def create(self, vals_list):
        # Only the invoice action may create trusted correction history.
        # pylint: disable=method-required-super
        raise UserError(self.env._("Use the native UAE invoice correction action."))

    def write(self, vals):
        # Public writes must never rewrite protected history.
        # pylint: disable=method-required-super
        raise UserError(self.env._("UAE local correction history is immutable."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_correction_history(self):
        raise UserError(self.env._("UAE local correction history cannot be deleted."))
