# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain


class UaeInboxEventReview(models.TransientModel):
    _name = "l10n_ae.edi.inbox.event.review"
    _description = "UAE Received Event Conflict Review"
    _check_company_auto = True

    inbox_id = fields.Many2one(
        "l10n_ae.edi.inbox",
        string="Event Original",
        required=True,
        readonly=True,
        check_company=True,
    )
    company_id = fields.Many2one(related="inbox_id.company_id", compute_sudo=False)
    original_ids = fields.Many2many(
        "l10n_ae.edi.inbox",
        relation="l10n_ae_edi_event_review_original_rel",
        column1="review_id",
        column2="inbox_id",
        string="Conflicting Originals",
        readonly=True,
    )
    approved_inbox_ids = fields.Many2many(
        "l10n_ae.edi.inbox",
        relation="l10n_ae_edi_event_review_approved_rel",
        column1="review_id",
        column2="inbox_id",
        string="Approved Originals",
        domain="[('id', 'in', original_ids)]",
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
                    current.env._("Select an original with an event conflict.")
                )
            source._check_event_review_operator()
            family = source._ingestion_family().with_env(current.env)
            family.check_access("read")
            if not source.ingestion_conflict or len(family) < 2:
                raise UserError(
                    current.env._("At least two conflicting originals are required.")
                )
            captured.append(
                {
                    "inbox_id": source.id,
                    "original_ids": [fields.Command.set(family.ids)],
                    "review_snapshot": source._event_review_snapshot(),
                    "justification": vals.get("justification", False),
                    "review_reference": vals.get("review_reference", False),
                }
            )
        # Defaults cannot fabricate the source or the retained review snapshot.
        # pylint: disable=context-overridden
        recorder = current.with_context(
            {"allowed_company_ids": current.env.companies.ids}
        )
        return super(UaeInboxEventReview, recorder).create(captured)

    def write(self, vals):
        if {
            "inbox_id",
            "company_id",
            "original_ids",
            "review_snapshot",
        }.intersection(vals):
            raise UserError(
                self.env._("The event review snapshot is protected. Reopen the review.")
            )
        return super().write(vals)

    def action_confirm(self):
        self.ensure_one()
        current = self.with_user(self.env.uid).sudo(False)
        current.check_access("read")
        current.inbox_id._check_event_review_operator()
        if (
            not current.approved_inbox_ids
            or not current.approved_inbox_ids <= current.original_ids
        ):
            raise UserError(
                current.env._("Choose originals from the reviewed event family.")
            )
        current.inbox_id.action_approve_event_originals(
            current.approved_inbox_ids.ids,
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
