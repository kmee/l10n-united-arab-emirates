# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from datetime import timedelta
from decimal import Decimal
from typing import ClassVar

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError


class CbuaeRateReview(models.Model):
    _name = "l10n_ae.cbuae.rate.review"
    _description = "CBUAE Reviewed Native Currency Rate"
    _rec_name = "source_label"
    _check_company_auto = True

    publication_id = fields.Many2one(
        "l10n_ae.cbuae.publication",
        required=True,
        ondelete="restrict",
        check_company=True,
    )
    company_id = fields.Many2one(
        related="publication_id.company_id", store=True, readonly=True
    )
    source_label = fields.Char(required=True)
    currency_id = fields.Many2one("res.currency", required=True)
    review_note = fields.Text(required=True)
    native_rate_id = fields.Many2one("res.currency.rate", readonly=True, copy=False)
    quote_literal = fields.Char(readonly=True, copy=False)
    reviewed_by = fields.Many2one("res.users", readonly=True, copy=False)
    reviewed_at = fields.Datetime(readonly=True, copy=False)
    anchor_rate_id = fields.Many2one("res.currency.rate", readonly=True, copy=False)
    anchor_rate_literal = fields.Char(readonly=True, copy=False)

    _import_fields: ClassVar[set[str]] = {
        "native_rate_id",
        "quote_literal",
        "reviewed_by",
        "reviewed_at",
        "anchor_rate_id",
        "anchor_rate_literal",
    }

    def _check_actor(self, operation):
        principal = self.env.user.with_user(self.env.uid)
        if (
            not principal.active
            or principal.share
            or not principal.has_group("account.group_account_manager")
        ):
            raise AccessError(
                self.env._("An active CBUAE company reviewer is required.")
            )
        self.with_user(self.env.uid).check_access(operation)
        for review in self:
            review.publication_id._check_actor("read")

    def _lock_review(self):
        if self:
            self.env.cr.execute(
                "SELECT id FROM l10n_ae_cbuae_rate_review WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(self.ids),),
            )
            self.invalidate_recordset()

    @api.model_create_multi
    def create(self, vals_list):
        self._check_actor("create")
        prepared = []
        for values in vals_list:
            if self._import_fields.union({"company_id"}).intersection(values):
                raise UserError(self.env._("Use the reviewed CBUAE import action."))
            vals = dict(values)
            publication_id = vals.get(
                "publication_id",
                self.default_get(["publication_id"]).get("publication_id"),
            )
            publication = (
                self.env["l10n_ae.cbuae.publication"].browse(publication_id).exists()
            )
            if len(publication) != 1:
                raise UserError(self.env._("Select a retained CBUAE publication."))
            publication._check_actor("read")
            vals["publication_id"] = publication.id
            vals.update(dict.fromkeys(self._import_fields, False))
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        self._check_actor("write")
        self._lock_review()
        self._check_actor("write")
        if self._import_fields.union({"company_id"}).intersection(vals):
            raise UserError(self.env._("CBUAE import evidence is protected."))
        if self.filtered("native_rate_id"):
            raise UserError(self.env._("Imported CBUAE reviews are immutable."))
        if "publication_id" in vals:
            publication = (
                self.env["l10n_ae.cbuae.publication"]
                .browse(vals["publication_id"])
                .exists()
            )
            if len(publication) != 1:
                raise UserError(self.env._("Select a retained CBUAE publication."))
            publication._check_actor("read")
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_imported_review(self):
        self._check_actor("unlink")
        self._lock_review()
        self._check_actor("unlink")
        if self.filtered("native_rate_id"):
            raise UserError(self.env._("Imported CBUAE reviews cannot be deleted."))

    def action_import(self):
        self.ensure_one()
        self._check_actor("write")
        self._lock_review()
        self._check_actor("write")
        publication = self.publication_id
        if self.native_rate_id:
            return True
        company = self.company_id
        if company != company.root_id:
            raise UserError(
                self.env._("Review the root-company currency policy before importing.")
            )
        if (
            publication.state != "captured"
            or not publication.source_complete
            or publication.publication_date != publication.requested_date
        ):
            raise UserError(self.env._("A complete dated CBUAE capture is required."))
        if (
            not self.currency_id.active
            or not self.review_note.strip()
            or self.source_label not in publication.quotes
        ):
            raise UserError(
                self.env._("Review an exact source label, active currency and note.")
            )
        rates = self.env["res.currency.rate"].with_company(company)
        if rates.search(
            [
                ("company_id", "in", [False, company.id]),
                ("currency_id", "=", self.currency_id.id),
                ("name", "=", publication.publication_date),
            ],
            limit=1,
        ):
            raise UserError(
                self.env._("An existing native rate cannot be overwritten.")
            )
        aed = self.env.ref("base.AED")
        selected_date = (
            aed.with_company(company)
            .with_context(
                date=publication.publication_date + timedelta(days=1),
                to_currency=aed.id,
            )
            .rate_date
        )
        anchor = rates.browse()
        if selected_date:
            anchor = rates.search(
                [
                    ("company_id", "in", [False, company.id]),
                    ("currency_id", "=", aed.id),
                    ("name", "=", selected_date),
                ],
                order="company_id, id desc",
                limit=1,
            )
            if selected_date > publication.publication_date or not anchor:
                raise UserError(self.env._("The dated AED anchor requires review."))
        anchor_literal = str(anchor.rate if anchor else 1.0)
        quote_literal = publication.quotes[self.source_label]
        technical_rate = float(Decimal(anchor_literal) / Decimal(quote_literal))
        imported = rates._create_cbuae_rate(
            self,
            {
                "name": publication.publication_date,
                "company_id": company.id,
                "currency_id": self.currency_id.id,
                "rate": technical_rate,
            },
        )
        super().write(
            {
                "native_rate_id": imported.id,
                "quote_literal": quote_literal,
                "reviewed_by": self.env.uid,
                "reviewed_at": fields.Datetime.now(),
                "anchor_rate_id": anchor.id,
                "anchor_rate_literal": anchor_literal,
            }
        )
        self.flush_recordset()
        return True
