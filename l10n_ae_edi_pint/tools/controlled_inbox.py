# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Receive controlled XML outside accounting transactions and preserve it first."""

from psycopg2.errors import SerializationFailure, UniqueViolation

from odoo import api
from odoo.exceptions import AccessError, UserError

from odoo.addons.l10n_ae_edi.tools.controlled_client import ControlledAspClient


class ControlledInboxWorker:
    """Loopback test service only; the caller supplies an authenticated remote ID."""

    def __init__(self, registry, uid, company_id, endpoint, token):
        self.registry, self.uid, self.company_id = registry, uid, company_id
        self.endpoint, self.token = endpoint, token

    def _binding(self, cursor, binding_id, process=False):
        env = api.Environment(
            cursor, self.uid, {"allowed_company_ids": [self.company_id]}
        )
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
        return binding

    def run_once(self, binding_id, remote_id, process=True):
        """Fetch, commit evidence, then optionally process in a new transaction."""
        if not isinstance(remote_id, str) or not 1 <= len(remote_id) <= 256:
            raise ValueError("Invalid controlled incoming document identifier.")
        with self.registry.cursor() as cursor:
            binding = self._binding(cursor, binding_id, process=process)
            tin, provider = binding.company_id.l10n_ae_tin, binding.provider
            if not tin or not binding.company_id.l10n_ae_tin_confirmed:
                raise UserError(
                    binding.env._(
                        "Confirm the UAE recipient TIN before retrieving documents."
                    )
                )
        # No live database cursor or lock during remote retrieval.
        receipt = ControlledAspClient(
            self.endpoint, self.token, provider
        ).lookup_inbound(tin, remote_id)
        if receipt is None:
            with self.registry.cursor() as cursor:
                self._binding(cursor, binding_id, process=process)
            return {"state": "not_found", "inbox_id": None}
        for attempt in range(3):
            try:
                with self.registry.cursor() as cursor:
                    binding = self._binding(cursor, binding_id, process=process)
                    if (
                        binding.provider != provider
                        or binding.company_id.l10n_ae_tin != tin
                    ):
                        raise UserError(
                            binding.env._(
                                "The UAE receiving route changed during retrieval."
                            )
                        )
                    inbox = binding.env[
                        "l10n_ae.edi.inbox"
                    ].receive_controlled_document(
                        binding.id, remote_id, receipt["xml"].encode()
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
                        inbox.action_process()
                    result = {"inbox_id": inbox.id, "state": inbox.state}
                    cursor.commit()
                    return result
            except SerializationFailure:
                if attempt == 2:
                    raise
