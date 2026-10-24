# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import ValidationError


class UaeEdiRetention(models.Model):
    _inherit = "l10n_ae.edi.retention"

    inbox_id = fields.Many2one(
        "l10n_ae.edi.inbox",
        string="Received Original",
        check_company=True,
        ondelete="restrict",
        index=True,
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
        records = super().create(prepared)
        records._invalidate_received_identity_packages()
        return records

    def _invalidate_received_identity_packages(self):
        for inbox in self.mapped("inbox_id"):
            inbox._identity_family().invalidate_recordset(["evidence_archive"])

    def write(self, vals):
        before = self.mapped("inbox_id")
        result = super().write(vals)
        for inbox in before | self.mapped("inbox_id"):
            inbox._identity_family().invalidate_recordset(["evidence_archive"])
        return result

    def action_approve(self):
        result = super().action_approve()
        self._invalidate_received_identity_packages()
        return result

    def unlink(self):
        originals = self.mapped("inbox_id")
        result = super().unlink()
        for inbox in originals:
            inbox._identity_family().invalidate_recordset(["evidence_archive"])
        return result

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


class UaeEdiRetentionHold(models.Model):
    _inherit = "l10n_ae.edi.retention.hold"

    @api.model_create_multi
    def create(self, vals_list):
        holds = super().create(vals_list)
        holds.mapped("retention_id")._invalidate_received_identity_packages()
        return holds

    def action_release(self, reason):
        result = super().action_release(reason)
        self.mapped("retention_id")._invalidate_received_identity_packages()
        return result
