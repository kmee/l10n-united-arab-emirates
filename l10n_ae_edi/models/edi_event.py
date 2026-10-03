# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import UserError


class UaeEdiEvent(models.Model):
    _name = "l10n_ae.edi.event"
    _description = "UAE Authenticated Controlled Event"
    _check_company_auto = True
    _order = "id"
    _rec_name = "event_id"

    transmission_id = fields.Many2one(
        "l10n_ae.edi.transmission",
        required=True,
        check_company=True,
        ondelete="restrict",
        index=True,
    )
    binding_id = fields.Many2one(
        "l10n_ae.edi.binding",
        required=True,
        check_company=True,
        ondelete="restrict",
        index=True,
    )
    company_id = fields.Many2one("res.company", required=True, index=True)
    event_id = fields.Char(required=True, readonly=True, index=True)
    dimension = fields.Char(required=True, readonly=True)
    fact_state = fields.Char(required=True, readonly=True)
    sequence = fields.Integer(required=True, readonly=True)
    occurred_at = fields.Datetime(required=True, readonly=True)
    received_at = fields.Datetime(required=True, readonly=True)
    original_body = fields.Text(required=True, readonly=True)
    body_hash = fields.Char(required=True, readonly=True)
    outcome = fields.Selection(
        [
            ("applied", "Applied"),
            ("replay_conflict", "Divergent Replay"),
            ("obsolete", "Older Fact"),
            ("conflict", "Contradictory Fact"),
        ],
        required=True,
        readonly=True,
    )

    _event_identity_unique = models.UniqueIndex(
        "(binding_id, event_id) WHERE outcome != 'replay_conflict'",
        "This UAE event identity already exists for this binding.",
    )

    _event_bytes_unique = models.UniqueIndex(
        "(binding_id, event_id, body_hash)",
        "These authenticated UAE event bytes already exist for this binding.",
    )

    def write(self, vals):
        # pylint: disable=method-required-super
        raise UserError(self.env._("UAE authenticated event evidence is immutable."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_event_evidence(self):
        raise UserError(
            self.env._("UAE authenticated event evidence cannot be deleted.")
        )
