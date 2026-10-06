# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class ResCompany(models.Model):
    _inherit = "res.company"

    l10n_ae_inbox_reviewer_id = fields.Many2one(
        "res.users",
        string="UAE Incoming Document Reviewer",
        groups="account.group_account_manager",
        copy=False,
        help="Explicit internal Accounting Manager for received-document review.",
    )

    @api.constrains("l10n_ae_inbox_reviewer_id")
    def _check_l10n_ae_inbox_reviewer(self):
        for company in self:
            owner = company.l10n_ae_inbox_reviewer_id
            if owner and (
                company.country_code != "AE"
                or not owner.active
                or owner.share
                or company not in owner.company_ids
                or not owner.has_group("account.group_account_manager")
            ):
                raise ValidationError(
                    self.env._(
                        "Select an active internal Accounting Manager in the UAE "
                        "company as incoming document reviewer."
                    )
                )

    def write(self, vals):
        if "l10n_ae_inbox_reviewer_id" in vals:
            actor = self.env.user.sudo(False)
            current = self.with_user(self.env.uid).sudo(False)
            if not actor.active or not actor.has_group("account.group_account_manager"):
                raise AccessError(
                    self.env._("An active Accounting Manager must configure review.")
                )
            current.check_access("write")
            if any(company not in actor.company_ids for company in current):
                raise AccessError(self.env._("The review company is not authorized."))
        return super().write(vals)
