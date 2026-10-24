# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain


class UaeInboxEventDecision(models.Model):
    _name = "l10n_ae.edi.inbox.event.decision"
    _description = "UAE Received Event Original Review"
    _check_company_auto = True
    _order = "id desc"

    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, index=True
    )
    binding_id = fields.Many2one(
        "l10n_ae.edi.binding",
        required=True,
        readonly=True,
        check_company=True,
        ondelete="restrict",
        index=True,
    )
    member_ids = fields.Many2many(
        "l10n_ae.edi.inbox",
        relation="l10n_ae_edi_event_decision_member_rel",
        column1="decision_id",
        column2="inbox_id",
        string="Reviewed Originals",
        readonly=True,
        check_company=True,
    )
    approved_inbox_ids = fields.Many2many(
        "l10n_ae.edi.inbox",
        relation="l10n_ae_edi_event_decision_approved_rel",
        column1="decision_id",
        column2="inbox_id",
        string="Approved Originals",
        readonly=True,
        check_company=True,
    )
    member_snapshot = fields.Json(required=True, readonly=True)
    operator_id = fields.Many2one(
        "res.users", required=True, readonly=True, ondelete="restrict"
    )
    occurred_at = fields.Datetime(required=True, readonly=True)
    justification = fields.Text(required=True, readonly=True)
    review_reference = fields.Char(required=True, readonly=True)

    def _access_domain(self, operation):
        domain = super()._access_domain(operation)
        if self.env.su:
            return domain
        originals = self.env["l10n_ae.edi.inbox"].sudo(False)
        return domain & Domain(
            "member_ids", "not any!", ~originals._access_domain("read")
        )

    @api.model_create_multi
    def create(self, vals_list):
        # pylint: disable=method-required-super
        raise UserError(
            self.env._("Event review decisions are recorded by their review action.")
        )

    def write(self, vals):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Event review decision history is protected."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_event_history(self):
        raise UserError(self.env._("Event review decision history cannot be deleted."))
