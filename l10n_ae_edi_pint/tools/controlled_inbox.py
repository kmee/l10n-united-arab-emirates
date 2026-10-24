# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Receive controlled XML outside accounting transactions and preserve it first."""

from psycopg2.errors import SerializationFailure, UniqueViolation

from odoo import api
from odoo.exceptions import AccessError, UserError

from odoo.addons.l10n_ae_edi.tools.controlled_client import (
    ControlledAspError,
    ControlledAspResponseLimit,
)


class UnreadableReceivedReplay(AccessError):
    """Exact received evidence exists, but its original cannot be disclosed."""


class ControlledInboxWorker:
    """Loopback receipt of a known original or a retained recipient queue page."""

    def __init__(self, registry, uid, company_id, endpoint, token, *, scheduled=False):
        self.registry, self.uid, self.company_id = registry, uid, company_id
        self.endpoint, self.token = endpoint, token
        self.scheduled = scheduled

    def _binding(self, cursor, binding_id, process=False):
        env = api.Environment(
            cursor, self.uid, {"allowed_company_ids": [self.company_id]}
        )
        if self.scheduled:
            env["l10n_ae.edi.binding"]._check_controlled_receiving_scheduler_actor()
        principal = env.user.exists()
        if (
            not principal
            or not principal.active
            or not (
                principal.has_group("account.group_account_manager")
                or not process
                and principal.has_group("l10n_ae_edi_pint.group_document_receiver")
            )
            or self.company_id not in principal.company_ids.ids
        ):
            raise AccessError(
                env._(
                    "An active authorized UAE document receiving account is required."
                )
            )
        binding = env["l10n_ae.edi.binding"].browse(binding_id).exists()
        binding.ensure_one()
        binding.check_access("read")
        if binding.company_id.id != self.company_id:
            raise AccessError(env._("The UAE binding belongs to another company."))
        if not binding.active or binding.environment != "sandbox":
            raise UserError(env._("An active controlled sandbox binding is required."))
        binding._check_transport_allowed()
        return binding

    def run_once(self, binding_id, remote_id, process=True, event_id=None):
        """Fetch, commit evidence, then optionally process in a new transaction."""
        if not isinstance(remote_id, str) or not 1 <= len(remote_id) <= 256:
            raise ValueError("Invalid controlled incoming document identifier.")
        if event_id is not None and (
            not isinstance(event_id, str) or not 1 <= len(event_id) <= 256
        ):
            raise ValueError("Invalid controlled incoming event identifier.")
        with self.registry.cursor() as cursor:
            binding = self._binding(cursor, binding_id, process=process)
            binding._require_receiving_capabilities()
            tin, provider = binding.company_id.l10n_ae_tin, binding.provider
            if not tin or not binding.company_id.l10n_ae_tin_confirmed:
                raise UserError(
                    binding.env._(
                        "Confirm the UAE recipient TIN before retrieving documents."
                    )
                )
            client = binding._get_controlled_client(self.endpoint, self.token)
        # No live database cursor or lock during remote retrieval.
        receipt = client.lookup_inbound(tin, remote_id, event_id=event_id)
        if receipt is None:
            with self.registry.cursor() as cursor:
                binding = self._binding(cursor, binding_id, process=process)
                binding._require_receiving_capabilities()
                self._check_received_identity(binding, tin, provider)
            return {"state": "not_found", "inbox_id": None}
        for attempt in range(3):
            try:
                with self.registry.cursor() as cursor:
                    binding = self._binding(cursor, binding_id, process=process)
                    self._check_received_identity(binding, tin, provider)
                    inbox = binding.env[
                        "l10n_ae.edi.inbox"
                    ].receive_controlled_document(
                        binding.id,
                        remote_id,
                        receipt["xml"].encode(),
                        event_id=event_id,
                    )
                    inbox_id = inbox.id
                    cursor.commit()
                break
            except (SerializationFailure, UniqueViolation):
                if attempt == 2:
                    raise
        # Original bytes survive validation/import failures or process termination.
        for attempt in range(3):
            try:
                with self.registry.cursor() as cursor:
                    binding = self._binding(cursor, binding_id, process=process)
                    inbox = binding.env["l10n_ae.edi.inbox"].browse(inbox_id)
                    if process:
                        processed = inbox.action_process()
                    result = {"inbox_id": inbox.id, "state": inbox.state}
                    if process and not processed:
                        reasons = [
                            field
                            for field in (
                                "event_review_pending",
                                "identity_review_pending",
                            )
                            if inbox[field]
                        ]
                        if reasons:
                            result.update(
                                processing_status="review_pending",
                                pending_review_reasons=reasons,
                            )
                    cursor.commit()
                    return result
            except SerializationFailure:
                if attempt == 2:
                    raise

    def _check_received_identity(self, binding, tin, provider):
        if not binding.company_id.l10n_ae_tin_confirmed:
            raise UserError(
                binding.env._(
                    "Confirm the UAE recipient TIN before retrieving documents."
                )
            )
        if binding.provider != provider or binding.company_id.l10n_ae_tin != tin:
            raise UserError(
                binding.env._("The UAE receiving route changed during retrieval.")
            )

    def _receiving_route(self, binding):
        tin = binding.company_id.l10n_ae_tin
        if not tin or not binding.company_id.l10n_ae_tin_confirmed:
            raise UserError(
                binding.env._(
                    "Confirm the UAE recipient TIN before retrieving documents."
                )
            )
        route = binding.provider + ":" + tin
        if (
            binding.controlled_inbound_route
            and binding.controlled_inbound_route != route
        ):
            raise UserError(
                binding.env._("The retained UAE receiving route has changed.")
            )
        return tin, binding.provider, route

    def run_page(self, binding_id, limit=20):
        """Commit a whole fetched page with its cursor, without accounting import."""
        with self.registry.cursor() as cursor:
            binding = self._binding(cursor, binding_id)
            binding._require_receiving_capabilities()
            tin, provider, route = self._receiving_route(binding)
            checkpoint = binding.controlled_inbound_cursor or ""
            client = binding._get_controlled_client(self.endpoint, self.token)
        # Neither listing nor artifact retrieval keeps a database transaction open.
        page = client.list_inbound(tin, checkpoint, limit=limit)
        originals = []
        for document in page["documents"]:
            event_id = document.get("event_id")
            try:
                receipt = client.lookup_inbound(
                    tin, document["remote_id"], event_id=event_id
                )
            except ControlledAspResponseLimit as error:
                # The authenticated listing supplies identity, not verified XML.
                # Only this measured cap refusal can advance with retained evidence.
                originals.append(
                    (
                        document["remote_id"],
                        None,
                        event_id,
                        {
                            "limit_bytes": error.limit_bytes,
                            "observed_bytes": error.observed_bytes,
                            "declared_xml_hash": document["xml_sha256"],
                        },
                    )
                )
                continue
            if receipt is None:
                raise ControlledAspError("missing_inbound_artifact")
            if receipt["xml_sha256"] != document["xml_sha256"]:
                raise ControlledAspError("correlation")
            originals.append(
                (document["remote_id"], receipt["xml"].encode(), event_id, None)
            )
        for attempt in range(3):
            try:
                with self.registry.cursor() as cursor:
                    binding = self._binding(cursor, binding_id)
                    binding._require_receiving_capabilities()
                    if self._receiving_route(binding) != (tin, provider, route):
                        raise UserError(
                            binding.env._(
                                "The UAE receiving route changed during retrieval."
                            )
                        )
                    cursor.execute(
                        "SELECT id FROM l10n_ae_edi_binding WHERE id=%s FOR UPDATE",
                        [binding.id],
                    )
                    binding.invalidate_recordset(["controlled_inbound_cursor"])
                    current = binding.controlled_inbound_cursor or ""
                    if current != checkpoint:
                        return {
                            "state": "stale_page",
                            "inbox_ids": [],
                            "rejection_ids": [],
                            "next_cursor": current,
                            "has_more": False,
                        }
                    inbox_ids = []
                    rejection_ids = []
                    for remote_id, content, event_id, response_limit in originals:
                        try:
                            if response_limit is not None:
                                rejected = binding.env[
                                    "l10n_ae.edi.inbox.rejection"
                                ]._record_response_limit(
                                    binding,
                                    remote_id,
                                    event_id=event_id,
                                    **response_limit,
                                )
                                rejection_ids.append(rejected.id)
                                continue
                            if not content:
                                rejected = binding.env[
                                    "l10n_ae.edi.inbox.rejection"
                                ]._record_empty(binding, remote_id, event_id=event_id)
                                rejection_ids.append(rejected.id)
                                continue
                            inbox = binding.env[
                                "l10n_ae.edi.inbox"
                            ].receive_controlled_document(
                                binding.id, remote_id, content, event_id=event_id
                            )
                        except UnreadableReceivedReplay:
                            # Exact bytes are retained; reveal no unreadable source ID.
                            # Other access/authority failures still abort the page.
                            continue
                        inbox_ids.append(inbox.id)
                    if originals:
                        binding._store_controlled_inbound_checkpoint(
                            page["next_cursor"], route
                        )
                    cursor.commit()
                    return {
                        "state": "received" if originals else "empty",
                        "inbox_ids": inbox_ids,
                        "rejection_ids": rejection_ids,
                        "next_cursor": page["next_cursor"],
                        "has_more": page["has_more"],
                    }
            except (SerializationFailure, UniqueViolation):
                if attempt == 2:
                    raise
