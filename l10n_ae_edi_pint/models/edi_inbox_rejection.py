# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Retain correlated empty retrievals without inventing an invoice or ingestion."""

import hashlib

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..tools.controlled_inbox import UnreadableReceivedReplay


class UaeEdiInboxRejection(models.Model):
    _name = "l10n_ae.edi.inbox.rejection"
    _description = "UAE Rejected Retrieved Artifact Evidence"
    _check_company_auto = True
    _rec_name = "remote_id"
    _order = "id desc"

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
    remote_id = fields.Char(required=True, readonly=True, index=True)
    event_id = fields.Char(string="Retrieved Delivery Event Reference", readonly=True)
    reason = fields.Selection(
        [("empty_xml", "Empty XML Artifact")], required=True, readonly=True
    )
    artifact = fields.Json(required=True, readonly=True)
    xml_size = fields.Integer(string="XML Byte Count", required=True, readonly=True)
    xml_hash = fields.Char(string="XML SHA-256", required=True, readonly=True)
    operator_id = fields.Many2one(
        "res.users", required=True, readonly=True, ondelete="restrict"
    )
    received_at = fields.Datetime(required=True, readonly=True)

    _retrieved_observation = models.UniqueIndex(
        "(binding_id, remote_id, COALESCE(event_id, ''))",
        "This rejected retrieval is already retained.",
    )
    _empty_artifact = models.Constraint(
        "CHECK(xml_size = 0 AND reason = 'empty_xml')",
        "Only an empty XML artifact belongs in this evidence ledger.",
    )

    def _record_empty(self, binding, remote_id, event_id=None):
        """Called by the page worker after authenticated/correlated HTTP retrieval.

        The worker holds the binding lock and commits this observation atomically
        with accepted originals and their queue checkpoint. This is not an accepted
        invoice or ingestion event; event_id identifies the rejected retrieval only.
        """
        inbox = self.env["l10n_ae.edi.inbox"]
        inbox._check_operator(process=False)
        binding.ensure_one()
        binding.check_access("read")
        inbox._check_receiving_company(binding.company_id)
        if not binding.active or binding.environment != "sandbox":
            raise UserError(self.env._("Select an active controlled sandbox binding."))
        binding._check_transport_allowed()
        binding._require_receiving_capabilities()
        if not isinstance(remote_id, str) or not 1 <= len(remote_id) <= 256:
            raise UserError(self.env._("Invalid received document identifier."))
        if event_id is not None and (
            not isinstance(event_id, str) or not 1 <= len(event_id) <= 256
        ):
            raise UserError(self.env._("Invalid received delivery event identifier."))
        # Caller defaults cannot fabricate evidence or widen its company scope.
        # pylint: disable=context-overridden
        ledger = self.sudo().with_context(
            {"allowed_company_ids": [binding.company_id.id]}
        )
        existing = ledger.search(
            [
                ("company_id", "=", binding.company_id.id),
                ("binding_id", "=", binding.id),
                ("remote_id", "=", remote_id),
                ("event_id", "=", event_id or False),
            ],
            limit=1,
        )
        if existing:
            current = existing.with_user(self.env.uid).sudo(False)
            if not current.has_access("read"):
                raise UnreadableReceivedReplay(
                    self.env._("The retained rejected retrieval is not readable.")
                )
            return current
        digest = hashlib.sha256(b"").hexdigest()
        current = (
            super(UaeEdiInboxRejection, ledger)
            .create(
                {
                    "binding_id": binding.id,
                    "company_id": binding.company_id.id,
                    "remote_id": remote_id,
                    "event_id": event_id or False,
                    "reason": "empty_xml",
                    "artifact": {"xml": "", "xml_sha256": digest},
                    "xml_size": 0,
                    "xml_hash": digest,
                    "operator_id": self.env.uid,
                    "received_at": fields.Datetime.now(),
                }
            )
            .with_env(self.env)
        )
        if not current.has_access("read"):
            raise UnreadableReceivedReplay(
                self.env._("The retained rejected retrieval is not readable.")
            )
        return current

    @api.model_create_multi
    def create(self, vals_list):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Rejected retrievals are recorded by the receiver."))

    def write(self, vals):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Rejected retrieval evidence is immutable."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_rejected_evidence(self):
        raise UserError(self.env._("Rejected retrieval evidence cannot be deleted."))
