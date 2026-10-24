# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Retain bounded retrieval refusals without inventing an invoice or ingestion."""

import hashlib
import re

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
        [
            ("empty_xml", "Empty XML Artifact"),
            ("response_limit", "HTTP Response Exceeds Retrieval Limit"),
        ],
        required=True,
        readonly=True,
    )
    artifact = fields.Json(readonly=True)
    xml_retrieved = fields.Boolean(compute="_compute_xml_retrieved")
    xml_size = fields.Integer(
        string="XML Byte Count",
        readonly=True,
        help="Available only when the complete XML was retrieved.",
    )
    xml_hash = fields.Char(string="XML SHA-256", readonly=True)
    response_limit_bytes = fields.Integer(
        string="HTTP Response Byte Limit", readonly=True
    )
    observed_response_bytes = fields.Integer(
        string="Observed HTTP Response Bytes (Lower Bound)",
        readonly=True,
        help="Read stopped here; this is not the full response or XML size.",
    )
    declared_xml_hash = fields.Char(
        string="Listing XML SHA-256 (Unverified)",
        readonly=True,
        help="Declared by the authenticated listing; not verified against an original.",
    )
    operator_id = fields.Many2one(
        "res.users", required=True, readonly=True, ondelete="restrict"
    )
    received_at = fields.Datetime(required=True, readonly=True)

    _retrieved_observation = models.UniqueIndex(
        "(binding_id, remote_id, COALESCE(event_id, ''))",
        "This rejected retrieval is already retained.",
    )
    _empty_artifact = models.Constraint(
        """CHECK(xml_size IS NOT NULL AND xml_size = 0 AND (
            (reason = 'empty_xml'
             AND artifact IS NOT NULL AND xml_hash IS NOT NULL
             AND artifact = jsonb_build_object('xml', '', 'xml_sha256', xml_hash)
             AND xml_hash =
                 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
             AND COALESCE(response_limit_bytes, 0) = 0
             AND COALESCE(observed_response_bytes, 0) = 0
             AND declared_xml_hash IS NULL)
            OR (reason = 'response_limit'
                AND artifact IS NULL AND xml_hash IS NULL
                AND response_limit_bytes IS NOT NULL AND response_limit_bytes > 0
                AND observed_response_bytes IS NOT NULL
                AND observed_response_bytes = response_limit_bytes + 1
                AND declared_xml_hash IS NOT NULL
                AND declared_xml_hash ~ '^[0-9a-f]{64}$')
        ))""",
        "Rejected retrieval evidence must retain only the observed facts.",
    )

    @api.depends("reason")
    def _compute_xml_retrieved(self):
        for record in self:
            record.xml_retrieved = record.reason == "empty_xml"

    def _record_empty(self, binding, remote_id, event_id=None):
        digest = hashlib.sha256(b"").hexdigest()
        return self._record_observation(
            binding,
            remote_id,
            event_id,
            {
                "reason": "empty_xml",
                "artifact": {"xml": "", "xml_sha256": digest},
                "xml_size": 0,
                "xml_hash": digest,
                "response_limit_bytes": 0,
                "observed_response_bytes": 0,
                "declared_xml_hash": False,
            },
        )

    def _record_response_limit(
        self,
        binding,
        remote_id,
        limit_bytes,
        observed_bytes,
        declared_xml_hash,
        event_id=None,
    ):
        if (
            type(limit_bytes) is not int
            or not 0 < limit_bytes < 2147483647
            or type(observed_bytes) is not int
            or observed_bytes != limit_bytes + 1
            or not isinstance(declared_xml_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", declared_xml_hash)
        ):
            raise UserError(self.env._("Invalid bounded retrieval observation."))
        return self._record_observation(
            binding,
            remote_id,
            event_id,
            {
                "reason": "response_limit",
                "artifact": False,
                "xml_size": 0,  # Integer storage sentinel; no XML was retrieved.
                "xml_hash": False,
                "response_limit_bytes": limit_bytes,
                "observed_response_bytes": observed_bytes,
                "declared_xml_hash": declared_xml_hash,
            },
        )

    def _record_observation(self, binding, remote_id, event_id, facts):
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
            if any(current[name] != value for name, value in facts.items()):
                raise UserError(
                    self.env._("The retained rejected retrieval has different facts.")
                )
            return current
        current = (
            super(UaeEdiInboxRejection, ledger)
            .create(
                {
                    "binding_id": binding.id,
                    "company_id": binding.company_id.id,
                    "remote_id": remote_id,
                    "event_id": event_id or False,
                    **facts,
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
