# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Explicit transaction boundaries for KMEE's loopback-only ASP simulation."""

import hashlib
import json
import random

from psycopg2.errors import SerializationFailure

from odoo import api, fields
from odoo.exceptions import AccessError, UserError

from .controlled_client import ControlledAspError
from .errors import UaeControlledEndpointMismatch, UaeDocumentValidationError


class ControlledExecutor:
    """Run after accounting commit, never inside a posting hook or HTTP RPC."""

    def __init__(
        self, registry, uid, company_id, endpoint, token, clock=None, retry_jitter=None
    ):
        self.registry, self.uid, self.company_id = registry, uid, company_id
        self.endpoint, self.token = endpoint, token
        self.clock = clock or fields.Datetime.now
        self.retry_jitter = retry_jitter or (lambda: random.randint(0, 15))

    def _environment(self, cursor):
        env = api.Environment(
            cursor, self.uid, {"allowed_company_ids": [self.company_id]}
        )
        principal = env.user.exists()
        if (
            not principal
            or not principal.active
            or not (
                principal.has_group("l10n_ae_edi.group_controlled_sender")
                or principal.has_group("account.group_account_manager")
            )
            or self.company_id not in principal.company_ids.ids
        ):
            raise AccessError(
                env._("An active authorized UAE sending account is required.")
            )
        env["l10n_ae.edi.binding"]._check_transport_allowed()
        return env

    def _record(self, cursor, transmission_id):
        env = self._environment(cursor)
        record = env["l10n_ae.edi.transmission"].browse(transmission_id).exists()
        record.ensure_one()
        record.check_access("read")
        if record.company_id.id != self.company_id:
            raise AccessError(env._("The UAE transmission belongs to another company."))
        if record.environment != "sandbox" or (
            record.submission_state == "queued" and not record.binding_id.active
        ):
            raise UserError(env._("An active controlled sandbox binding is required."))
        return record

    def run_once(self, transmission_id):
        # Read/validate frozen bytes without a row lock. No remote HTTP here.
        with self.registry.cursor() as cursor:
            record = self._record(cursor, transmission_id)
            if record.local_correction_id:
                return "local_correction"
            if record.submission_state == "accepted":
                return "accepted"
            if record.provider != record.binding_id.provider:
                raise UaeDocumentValidationError(
                    record.env._(
                        "The UAE provider binding differs from the frozen transmission."
                    ),
                    kind="provider_mismatch",
                )
            if (
                record.submission_state == "unknown"
                and record.next_attempt_at
                and record.next_attempt_at > self.clock()
            ):
                return "unknown"
            # Validate only this accessible frozen issue; do not grant invoice ACLs.
            validation = record.sudo()._validate_controlled_document()
            document = {
                "contract_version": "kmee-controlled-1",
                "snapshot": record.payload,
                "snapshot_sha256": record.payload_hash,
                "xml": record.xml_content or None,
                "xml_sha256": record.xml_hash or None,
            }
            if (
                hashlib.sha256(record.payload.encode()).hexdigest()
                != record.payload_hash
            ):
                raise UaeDocumentValidationError(
                    record.env._("Frozen UAE snapshot integrity check failed.")
                )
            key, provider = record.idempotency_key, record.provider
            tin = json.loads(record.payload)["company"]["tin"]
            pending_dimensions = record._controlled_pending_dimensions()
            client = record.binding_id._get_controlled_client(self.endpoint, self.token)
        # Commit the durable reservation BEFORE remote I/O; a crash leaves a lease.
        try:
            with self.registry.cursor() as cursor:
                record = self._record(cursor, transmission_id).sudo()
                claim = record._claim_controlled(
                    self.endpoint, self.clock(), validation
                )
                if not claim:
                    if record.local_correction_id:
                        return "local_correction"
                    return record.submission_state
                # Invalid system policy rolls back the tentative reservation.
                # Sample only after a claim, before durable state or remote I/O.
                jitter = self.retry_jitter()
                if type(jitter) is not int or not 0 <= jitter <= 15:
                    raise ValueError(
                        "Controlled retry jitter must be between 0 and 15 seconds."
                    )
                cursor.commit()
        except SerializationFailure:
            return "busy"
        receipt, error_kind, evidence = None, None, None
        try:
            receipt = (
                client.submit(key, document)
                if claim["operation"] == "submit"
                else client.lookup(tin, key)
            )
            evidence = receipt
            if receipt and any(
                receipt.get(name) != expected
                for name, expected in {
                    "provider": provider,
                    "tin": tin,
                    "request_key": key,
                    "snapshot_sha256": document["snapshot_sha256"],
                    "xml_sha256": document["xml_sha256"],
                }.items()
            ):
                receipt, error_kind = None, "correlation"
            if receipt and (
                receipt.get("submission_state") != "accepted"
                or any(
                    receipt.get(name) != expected
                    for name, expected in pending_dimensions.items()
                )
            ):
                receipt, error_kind = None, "unknown"
            if not receipt and not error_kind:
                error_kind = "not_found"
        except ControlledAspError as error:
            error_kind = error.kind
        # Unknown/not-found is never permission to POST again.
        jitter = 0 if receipt else jitter
        with self.registry.cursor() as cursor:
            record = self._record(cursor, transmission_id).sudo()
            result = record._finish_controlled(
                claim["token"],
                receipt,
                error_kind,
                self.clock(),
                evidence,
                retry_jitter=jitter,
            )
            cursor.commit()
            return result

    def run_pending(self, binding_id, limit=20):
        """Dispatch committed intents from one company/provider route."""
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("The controlled batch limit must be between 1 and 200.")
        with self.registry.cursor() as cursor:
            env = self._environment(cursor)
            binding = env["l10n_ae.edi.binding"].browse(binding_id).exists()
            binding.ensure_one()
            binding.check_access("read")
            if binding.company_id.id != self.company_id:
                raise AccessError(env._("The UAE binding belongs to another company."))
            transmission_ids = (
                env["l10n_ae.edi.transmission"]
                .search(
                    [
                        ("company_id", "=", self.company_id),
                        ("binding_id", "=", binding_id),
                        ("local_correction_id", "=", False),
                        ("dispatch_review_required", "=", False),
                        "|",
                        "|",
                        "&",
                        ("submission_state", "=", "queued"),
                        ("binding_id.active", "=", True),
                        "&",
                        ("submission_state", "=", "unknown"),
                        "|",
                        ("next_attempt_at", "=", False),
                        ("next_attempt_at", "<=", self.clock()),
                        "&",
                        ("submission_state", "=", "sending"),
                        ("lease_until", "<=", self.clock()),
                    ],
                    limit=limit,
                    order="id",
                )
                .ids
            )
        outcomes = []
        for record_id in transmission_ids:
            outcome = {"transmission_id": record_id}
            try:
                outcome["result"] = self.run_once(record_id)
            except UaeDocumentValidationError as error:
                with self.registry.cursor() as cursor:
                    record = self._record(cursor, record_id).sudo()
                    outcome["result"] = record._mark_controlled_dispatch_review(
                        self.clock(), report=error.report, error_kind=error.kind
                    )
                    cursor.commit()
                if outcome["result"] == "blocked":
                    outcome["error_kind"] = error.kind
            except UaeControlledEndpointMismatch:
                with self.registry.cursor() as cursor:
                    record = self._record(cursor, record_id).sudo()
                    outcome["result"] = record._mark_controlled_endpoint_review(
                        self.endpoint, self.clock()
                    )
                    cursor.commit()
                if outcome["result"] == "blocked":
                    outcome["error_kind"] = "endpoint_mismatch"
            outcomes.append(outcome)
        return outcomes
