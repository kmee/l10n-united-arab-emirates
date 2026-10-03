# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import UserError


class UaeEdiAttempt(models.Model):
    _name = "l10n_ae.edi.attempt"
    _description = "UAE Durable Transport Attempt"
    _check_company_auto = True
    _order = "id"

    transmission_id = fields.Many2one(
        "l10n_ae.edi.transmission",
        required=True,
        check_company=True,
        ondelete="restrict",
        index=True,
    )
    company_id = fields.Many2one("res.company", required=True, index=True)
    operation = fields.Selection(
        [("submit", "Submit"), ("lookup", "Reconcile")],
        required=True,
        readonly=True,
    )
    claim_token = fields.Char(required=True, readonly=True, groups="base.group_system")
    started_at = fields.Datetime(required=True, readonly=True)
    completed_at = fields.Datetime(readonly=True)
    state = fields.Selection(
        [
            ("started", "Started"),
            ("accepted", "Accepted by API"),
            ("unknown", "Indeterminate"),
            ("superseded", "Superseded Lease"),
        ],
        default="started",
        required=True,
        readonly=True,
    )
    receipt = fields.Text(readonly=True)
    validation_receipt = fields.Text(readonly=True)
    error_kind = fields.Char(readonly=True)

    def _complete_controlled(self, values):
        self.ensure_one()
        if self.state != "started":
            raise UserError(self.env._("A completed UAE attempt is immutable."))
        return super().write(values)

    def write(self, vals):
        # This model exposes read-only evidence; only _complete_controlled writes.
        # pylint: disable=method-required-super
        raise UserError(self.env._("UAE transport attempt evidence is immutable."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_attempt_evidence(self):
        raise UserError(self.env._("UAE transport attempt evidence cannot be deleted."))
