# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain


class UaeInboxIdentityDecision(models.Model):
    _name = "l10n_ae.edi.inbox.decision"
    _description = "UAE Received Identity Internal Selection"
    _check_company_auto = True
    _rec_name = "remote_id"
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
    remote_id = fields.Char(required=True, readonly=True, index=True)
    selected_inbox_id = fields.Many2one(
        "l10n_ae.edi.inbox",
        string="Selected Original",
        required=True,
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )
    selected_xml_hash = fields.Char(
        string="Selected XML SHA-256", required=True, readonly=True
    )
    member_ids = fields.Many2many(
        "l10n_ae.edi.inbox",
        string="Reviewed Originals",
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
        # Native Domain conditions support this internal operator; XML lists do not.
        # Check every member, including those hidden by original record access.
        return domain & Domain(
            "member_ids", "not any!", ~originals._access_domain("read")
        )

    @api.model_create_multi
    def create(self, vals_list):
        # pylint: disable=method-required-super
        raise UserError(
            self.env._("Identity selections are recorded by their review action.")
        )

    def write(self, vals):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Received identity selection history is protected."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_identity_history(self):
        raise UserError(
            self.env._("Received identity selection history cannot be deleted.")
        )
