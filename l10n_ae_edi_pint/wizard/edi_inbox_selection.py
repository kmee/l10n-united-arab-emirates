# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.fields import Domain


class UaeInboxIdentitySelection(models.TransientModel):
    _name = "l10n_ae.edi.inbox.selection"
    _description = "UAE Received Original Identity Review"
    _check_company_auto = True

    inbox_id = fields.Many2one(
        "l10n_ae.edi.inbox",
        string="Selected Original",
        required=True,
        readonly=True,
        check_company=True,
    )
    company_id = fields.Many2one(related="inbox_id.company_id", compute_sudo=False)
    xml_hash = fields.Char(
        related="inbox_id.xml_hash", string="Selected XML SHA-256", compute_sudo=False
    )
    original_ids = fields.Many2many(
        "l10n_ae.edi.inbox", string="Reviewed Originals", readonly=True
    )
    review_snapshot = fields.Json(readonly=True)
    justification = fields.Text()
    review_reference = fields.Char()

    def _access_domain(self, operation):
        domain = super()._access_domain(operation)
        if self.env.su:
            return domain
        originals = self.env["l10n_ae.edi.inbox"].sudo(False)
        return domain & Domain(
            "original_ids", "not any!", ~originals._access_domain("read")
        )

    @api.model_create_multi
    def create(self, vals_list):
        current = self.with_user(self.env.uid).sudo(False)
        captured = []
        for vals in vals_list:
            source_id = vals.get("inbox_id") or current.default_get(["inbox_id"]).get(
                "inbox_id"
            )
            source = current.env["l10n_ae.edi.inbox"].browse(source_id).exists()
            if not source:
                raise UserError(
                    current.env._("Select an original with a remote ID conflict.")
                )
            source._check_operator()
            if current.env.user.share:
                raise AccessError(
                    current.env._("An internal Accounting Manager is required.")
                )
            source.check_access("read")
            source._check_receiving_company(source.company_id)
            source.binding_id.check_access("read")
            source.binding_id._check_transport_allowed()
            family = source._identity_family().with_env(current.env)
            family.check_access("read")
            if not source.identity_conflict or len(family) < 2:
                raise UserError(
                    current.env._("At least two conflicting originals are required.")
                )
            captured.append(
                {
                    "inbox_id": source.id,
                    "original_ids": [fields.Command.set(family.ids)],
                    "review_snapshot": source._identity_snapshot(family),
                    "justification": vals.get("justification", False),
                    "review_reference": vals.get("review_reference", False),
                }
            )
        # Defaults cannot fabricate the retained review snapshot or its source.
        # pylint: disable=context-overridden
        recorder = current.with_context(
            {"allowed_company_ids": current.env.companies.ids}
        )
        return super(UaeInboxIdentitySelection, recorder).create(captured)

    def write(self, vals):
        if {
            "inbox_id",
            "company_id",
            "xml_hash",
            "original_ids",
            "review_snapshot",
        }.intersection(vals):
            raise UserError(
                self.env._(
                    "The original review snapshot is protected. Reopen the review."
                )
            )
        return super().write(vals)

    def action_confirm(self):
        self.ensure_one()
        current = self.with_user(self.env.uid).sudo(False)
        current.check_access("read")
        current.inbox_id.action_select_identity_variant(
            current.justification,
            current.review_reference,
            expected_snapshot=current.review_snapshot,
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": "l10n_ae.edi.inbox",
            "res_id": current.inbox_id.id,
            "view_mode": "form",
            "target": "current",
        }
