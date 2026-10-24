# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import UserError


class ResCurrencyRate(models.Model):
    _inherit = "res.currency.rate"

    l10n_ae_cbuae_review_id = fields.Many2one(
        "l10n_ae.cbuae.rate.review", readonly=True, copy=False, ondelete="restrict"
    )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for values in vals_list:
            if "l10n_ae_cbuae_review_id" in values:
                raise UserError(self.env._("Use the reviewed CBUAE import action."))
            prepared.append({**values, "l10n_ae_cbuae_review_id": False})
        return super().create(prepared)

    def write(self, vals):
        if "l10n_ae_cbuae_review_id" in vals or self.filtered(
            "l10n_ae_cbuae_review_id"
        ):
            raise UserError(
                self.env._("Imported CBUAE rates and provenance are immutable.")
            )
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_cbuae_import(self):
        if self.filtered("l10n_ae_cbuae_review_id"):
            raise UserError(self.env._("Imported CBUAE rates cannot be deleted."))

    def _create_cbuae_rate(self, review, values):
        return super().create({**values, "l10n_ae_cbuae_review_id": review.id})

    def _l10n_ae_cbuae_provenance(self):
        """Return the selected row's reviewed source chain, without fiscal approval."""
        self.ensure_one()
        self.with_user(self.env.uid).check_access("read")
        review = self.l10n_ae_cbuae_review_id.with_user(self.env.uid)
        if not review:
            return None
        review.check_access("read")
        publication = review.publication_id
        publication.check_access("read")
        if self.currency_id.name != review.import_facts["currency_code"]:
            raise UserError(
                self.env._(
                    "The selected currency code differs from its reviewed "
                    "CBUAE import. Review the currency identity before "
                    "preparing this e-invoice."
                )
            )
        return {
            "review_id": review.id,
            "publication_id": publication.id,
            "native_rate_id": self.id,
            "source_label": review.source_label,
            "quote_literal": review.quote_literal,
            "currency_code": review.import_facts["currency_code"],
            "review_note": review.review_note,
            "reviewed_by": review.reviewed_by.id,
            "reviewed_at": fields.Datetime.to_string(review.reviewed_at),
            "import_facts": review.import_facts,
            "source": {
                "url": publication.source_url,
                "date": fields.Date.to_string(publication.publication_date),
                "stamp": publication.publication_stamp,
                "sha256": publication.source_sha256,
                "http_status": publication.http_status,
                "content_type": publication.content_type,
                "retrieved_at": fields.Datetime.to_string(publication.retrieved_at),
                "complete": publication.source_complete,
            },
        }
