# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import UserError


class UaeEdiBillingRequest(models.Model):
    _name = "l10n_ae.edi.billing.request"
    _description = "UAE Original Billing Request Receipt"
    _rec_name = "origin_document_id"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, index=True
    )
    environment = fields.Selection(
        [("sandbox", "Sandbox"), ("production", "Production")],
        required=True,
        readonly=True,
    )
    process = fields.Selection(
        [("billing", "Billing"), ("selfbilling", "Self-Billing")],
        required=True,
        readonly=True,
    )
    origin_system = fields.Char(required=True, readonly=True)
    origin_document_id = fields.Char(required=True, readonly=True)
    request_hash = fields.Char(required=True, readonly=True)
    move_id = fields.Many2one(
        "account.move",
        required=True,
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )

    _origin_identity_unique = models.UniqueIndex(
        "(company_id, environment, process, origin_system, origin_document_id)",
        "This original UAE billing request already exists.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Billing request receipts are protected evidence."))

    def write(self, vals):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Billing request receipts are protected evidence."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_protected_receipt(self):
        raise UserError(self.env._("Billing request receipts are protected evidence."))

    def _record_prepared_request(self, identity, request_hash, move):
        move.check_access("read")
        if (
            move.company_id.id != identity["company_id"]
            or not move.l10n_ae_transmission_id
        ):
            raise UserError(
                self.env._(
                    "Prepare the authorized invoice before recording billing evidence."
                )
            )
        # The public billing facade has already checked native create/write access.
        # Elevation only persists this immutable receipt, never accounting entries.
        return (
            super(UaeEdiBillingRequest, self.sudo())
            .create({**identity, "request_hash": request_hash, "move_id": move.id})
            .sudo(False)
        )
