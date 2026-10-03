# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError


class UaeEdiTransmission(models.Model):
    _name = "l10n_ae.edi.transmission"
    _description = "UAE Frozen Invoice Transmission"
    _check_company_auto = True
    _rec_name = "origin_document_id"

    move_id = fields.Many2one(
        "account.move", required=True, ondelete="restrict", index=True
    )
    company_id = fields.Many2one("res.company", required=True, index=True)
    binding_id = fields.Many2one(
        "l10n_ae.edi.binding", required=True, check_company=True, ondelete="restrict"
    )
    provider = fields.Char(required=True, readonly=True)
    environment = fields.Selection(
        [("sandbox", "Sandbox"), ("production", "Production")], required=True
    )
    process = fields.Selection(
        [("billing", "Billing"), ("selfbilling", "Self-Billing")], required=True
    )
    origin_system = fields.Char(required=True)
    origin_document_id = fields.Char(required=True)
    revision = fields.Integer(default=1, required=True)
    payload = fields.Text(required=True, readonly=True)
    payload_hash = fields.Char(required=True, readonly=True)
    xml_content = fields.Text(readonly=True)
    xml_hash = fields.Char(readonly=True)
    idempotency_key = fields.Char(
        required=True, readonly=True, default=lambda self: str(uuid.uuid4()), copy=False
    )
    submission_state = fields.Selection(
        [("queued", "Queued")], default="queued", required=True, readonly=True
    )

    _business_revision_unique = models.UniqueIndex(
        "(company_id, environment, process, origin_system, origin_document_id, "
        "revision)",
        "This UAE invoice revision already exists, independently of its ASP.",
    )

    def write(self, vals):
        immutable = {
            "move_id",
            "company_id",
            "binding_id",
            "provider",
            "environment",
            "process",
            "origin_system",
            "origin_document_id",
            "revision",
            "payload",
            "payload_hash",
            "idempotency_key",
            "xml_content",
            "xml_hash",
        }
        if immutable.intersection(vals):
            raise UserError(self.env._("Frozen UAE invoice evidence is immutable."))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_frozen_evidence(self):
        raise UserError(
            self.env._(
                "Frozen UAE invoice evidence is immutable and cannot be deleted."
            )
        )
