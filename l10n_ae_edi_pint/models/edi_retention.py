# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import ValidationError


class UaeEdiRetention(models.Model):
    _inherit = "l10n_ae.edi.retention"

    inbox_id = fields.Many2one(
        "l10n_ae.edi.inbox", check_company=True, ondelete="restrict", index=True
    )

    @api.model_create_multi
    def create(self, vals_list):
        # Native defaults are applied after base pre-validation. Preserve only the
        # explicit source so a default cannot introduce a second fiscal original.
        prepared = [
            dict(
                values,
                transmission_id=values.get("transmission_id") or False,
                inbox_id=values.get("inbox_id") or False,
            )
            for values in vals_list
        ]
        return super().create(prepared)

    @api.model
    def _document_from_values(self, vals):
        if bool(vals.get("transmission_id")) == bool(vals.get("inbox_id")):
            raise ValidationError(
                self.env._("A retention obligation needs exactly one UAE original.")
            )
        if not vals.get("inbox_id"):
            return super()._document_from_values(vals)
        document = self.env["l10n_ae.edi.inbox"].browse(vals["inbox_id"]).exists()
        if len(document) != 1:
            raise ValidationError(
                self.env._("A retention obligation needs exactly one UAE original.")
            )
        return document

    def _document(self):
        self.ensure_one()
        return self._document_from_values(self._target_values())

    def _target_values(self):
        return super()._target_values() | {"inbox_id": self.inbox_id.id}


class UaeEdiInbox(models.Model):
    _inherit = "l10n_ae.edi.inbox"

    retention_ids = fields.One2many("l10n_ae.edi.retention", "inbox_id", readonly=True)

    def get_retention_status(self, as_of=None):
        self.ensure_one()
        return self.env["l10n_ae.edi.retention"]._assess(self, as_of, "inbox_id")
