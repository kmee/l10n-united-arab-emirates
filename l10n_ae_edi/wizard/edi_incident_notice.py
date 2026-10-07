# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import uuid

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class UaeEdiIncidentNoticeWizard(models.TransientModel):
    _name = "l10n_ae.edi.incident.notice.wizard"
    _description = "Record Supplied UAE Incident Notice"

    incident_id = fields.Many2one("l10n_ae.edi.incident", required=True, readonly=True)
    evidence_key = fields.Char(
        required=True, readonly=True, copy=False, default=lambda self: uuid.uuid4().hex
    )
    evidence_origin = fields.Selection(
        [("simulated", "Controlled Simulation"), ("external", "Externally Supplied")],
        required=True,
    )
    sender_role = fields.Selection(
        [("taxpayer", "Taxpayer"), ("asp", "ASP")], required=True, default="taxpayer"
    )
    sender_name = fields.Char(required=True)
    taxpayer_roles = fields.Selection(
        [("issuer", "Issuer"), ("recipient", "Recipient"), ("both", "Both")]
    )
    recipient_role = fields.Selection(
        [("fta", "FTA"), ("asp", "ASP"), ("taxpayer", "Taxpayer")], required=True
    )
    recipient_name = fields.Char(required=True)
    recipient_address = fields.Text(required=True)
    channel = fields.Selection(
        [
            ("email", "Email"),
            ("portal", "Portal"),
            ("letter", "Letter"),
            ("other", "Other"),
        ],
        required=True,
    )
    sent_at = fields.Datetime(
        string="Declared Sent At",
        required=True,
        help="Declared by the supplied artifact; this does not verify delivery.",
    )
    external_reference = fields.Text(required=True)
    review_note = fields.Text(required=True)
    document = fields.Binary(required=True, attachment=False)
    document_filename = fields.Char(required=True)

    @api.onchange("sender_role")
    def _onchange_sender_role(self):
        if self.sender_role != "taxpayer":
            self.taxpayer_roles = False

    def action_confirm(self):
        self.ensure_one()
        self.with_user(self.env.uid).check_access("read")
        if not self.document:
            raise ValidationError(self.env._("Supply the original notice document."))
        evidence = {
            name: self[name]
            for name in (
                "evidence_key",
                "evidence_origin",
                "sender_role",
                "sender_name",
                "taxpayer_roles",
                "recipient_role",
                "recipient_name",
                "recipient_address",
                "channel",
                "external_reference",
                "review_note",
                "document_filename",
            )
        }
        evidence.update(
            sent_at=fields.Datetime.to_string(self.sent_at),
            document=self.document.to_base64(),
        )
        self.incident_id.record_notice(evidence)
        return {"type": "ir.actions.act_window_close"}
