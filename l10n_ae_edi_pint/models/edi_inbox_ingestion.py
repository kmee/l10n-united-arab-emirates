# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import UserError

from .edi_inbox import UaeEdiInbox


class UaeEdiInboxIngestion(models.Model):
    _name = "l10n_ae.edi.inbox.ingestion"
    _description = "UAE Received Delivery Event Evidence"
    _check_company_auto = True
    _order = "id"
    _rec_name = "event_id"

    inbox_id = fields.Many2one(
        "l10n_ae.edi.inbox",
        required=True,
        readonly=True,
        check_company=True,
        ondelete="restrict",
        index=True,
    )
    binding_id = fields.Many2one(
        "l10n_ae.edi.binding",
        required=True,
        readonly=True,
        check_company=True,
        ondelete="restrict",
        index=True,
    )
    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, index=True
    )
    event_id = fields.Char(
        string="Delivery Event Reference", required=True, readonly=True, index=True
    )
    remote_id = fields.Char(
        string="ASP Document Reference", required=True, readonly=True
    )
    xml_hash = fields.Char(string="Retained XML SHA-256", required=True, readonly=True)
    operator_id = fields.Many2one(
        "res.users", required=True, readonly=True, ondelete="restrict"
    )
    occurred_at = fields.Datetime(required=True, readonly=True)
    provider = fields.Char(required=True, readonly=True)
    environment = fields.Char(required=True, readonly=True)
    recipient_tin = fields.Char(readonly=True)
    disposition = fields.Selection(
        [("retained", "Retained"), ("collision", "Event Identity Conflict")],
        required=True,
        readonly=True,
    )

    _event_observation = models.UniqueIndex(
        "(binding_id, event_id, inbox_id)",
        "This delivery event and original are already retained.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        # pylint: disable=method-required-super
        raise UserError(
            self.env._("Delivery event evidence is recorded by the receiver.")
        )

    def write(self, vals):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Delivery event evidence is immutable."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_ingestion_evidence(self):
        raise UserError(self.env._("Delivery event evidence cannot be deleted."))

    def _record(self, inbox, event_id):
        inbox = self.env["l10n_ae.edi.inbox"].browse(inbox.id)
        inbox._check_operator(process=False)
        inbox.check_access("read")
        inbox._check_receiving_company(inbox.company_id)
        # The receipt boundary holds the binding lock and supplies validated IDs.
        # Do not inherit caller defaults or grant reading rights to other originals.
        # pylint: disable=context-overridden
        ledger = self.sudo().with_context(
            {"allowed_company_ids": [inbox.company_id.id]}
        )
        existing = ledger.search(
            [
                ("company_id", "=", inbox.company_id.id),
                ("binding_id", "=", inbox.binding_id.id),
                ("event_id", "=", event_id),
                ("inbox_id", "=", inbox.id),
            ],
            limit=1,
        )
        if existing:
            return existing.with_env(self.env)
        prior = ledger.search(
            [
                ("company_id", "=", inbox.company_id.id),
                ("binding_id", "=", inbox.binding_id.id),
                ("event_id", "=", event_id),
            ]
        )
        row = super(UaeEdiInboxIngestion, ledger).create(
            {
                "inbox_id": inbox.id,
                "binding_id": inbox.binding_id.id,
                "company_id": inbox.company_id.id,
                "event_id": event_id,
                "remote_id": inbox.remote_id,
                "xml_hash": inbox.xml_hash,
                "operator_id": self.env.uid,
                "occurred_at": fields.Datetime.now(),
                "provider": inbox.binding_id.provider,
                "environment": inbox.binding_id.environment,
                "recipient_tin": inbox.company_id.l10n_ae_tin or False,
                "disposition": "collision" if prior else "retained",
            }
        )
        if prior:
            # Keep every association unchanged, including originals hidden to this
            # receiver. The authorized binding bounds the internal update.
            originals = (prior.inbox_id | row.inbox_id).sudo()
            super(UaeEdiInbox, originals).write(
                {
                    "ingestion_conflict": True,
                    "state": "review",
                    "diagnostic": self.env._(
                        "A delivery event reference identifies "
                        "different received originals."
                    ),
                }
            )
            for original in originals:
                original._identity_family().invalidate_recordset(["evidence_archive"])
        for original in inbox._ingestion_family():
            original._identity_family().invalidate_recordset(["evidence_archive"])
        inbox.invalidate_recordset(["ingestion_event_ids", "evidence_archive"])
        return row.with_env(self.env)
