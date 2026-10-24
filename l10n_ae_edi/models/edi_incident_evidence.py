# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import base64
import binascii
import hashlib

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.binary import BinaryBytes


class UaeEdiIncidentEvidence(models.AbstractModel):
    _name = "l10n_ae.edi.incident.evidence.mixin"
    _description = "UAE Incident Original Evidence"

    incident_id = fields.Many2one(
        "l10n_ae.edi.incident",
        required=True,
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    document_content = fields.Binary(
        required=True, readonly=True, attachment=True, groups="base.group_system"
    )
    document = fields.Binary(
        compute="_compute_document", compute_sudo=False, attachment=False
    )
    document_filename = fields.Char(required=True, readonly=True)
    document_sha256 = fields.Char(required=True, readonly=True)

    _max_document_bytes = 10 * 1024 * 1024

    @api.depends_context("uid", "allowed_company_ids")
    @api.depends("document_content", "document_sha256", "incident_id")
    def _compute_document(self):
        for record in self:
            actual = record.with_user(self.env.uid)
            actual.check_access("read")
            actual.incident_id._check_actor("read")
            original = actual.sudo().document_content
            content = original.content if original else b""
            if hashlib.sha256(content).hexdigest() != actual.document_sha256:
                raise UserError(actual._document_integrity_message())
            record.document = BinaryBytes(content)

    def _document_integrity_message(self):
        """Each evidence type preserves its existing user-facing diagnostic."""
        raise NotImplementedError("Supply the incident evidence integrity diagnostic.")

    @api.model
    def _decode_original_document(self, encoded):
        if (
            not isinstance(encoded, str)
            or len(encoded) > ((self._max_document_bytes + 2) // 3) * 4
        ):
            raise ValidationError(self.env._("Supply a Base64 document up to 10 MiB."))
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError(
                self.env._("Supply a valid Base64 document.")
            ) from exc
        if not content or len(content) > self._max_document_bytes:
            raise ValidationError(
                self.env._("Supply a nonempty document up to 10 MiB.")
            )
        return content
