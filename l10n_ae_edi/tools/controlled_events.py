# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""KMEE's own controlled signed-event boundary; no vendor protocol is implied."""

import hashlib
import hmac
import json
import re
from datetime import datetime

from psycopg2.errors import SerializationFailure, UniqueViolation

from odoo import api, fields
from odoo.exceptions import AccessError, UserError
from odoo.tools import LazyTranslate

_lt = LazyTranslate(__name__)

DIMENSION_STATES = {
    "validation_c2_state": {"passed", "rejected"},
    "delivery_c3_state": {"delivered", "rejected"},
    "reporting_c2_state": {"confirmed", "rejected"},
    "reporting_c3_state": {"confirmed", "rejected"},
}
REQUIRED_KEYS = {
    "contract_version",
    "event_id",
    "provider",
    "tin",
    "request_key",
    "remote_id",
    "revision",
    "snapshot_sha256",
    "xml_sha256",
    "dimension",
    "state",
    "sequence",
    "occurred_at",
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate controlled event key")
        result[key] = value
    return result


class ControlledEventReceiver:
    """Consume authenticated bytes in a dedicated transaction after API acceptance."""

    def __init__(self, registry, uid, company_id, secret):
        if not isinstance(secret, bytes) or len(secret) < 16:
            raise ValueError("A process-supplied controlled event secret is required.")
        self.registry, self.uid, self.company_id = registry, uid, company_id
        self.secret = secret

    def _decode(self, raw, signature):
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 65536:
            raise ValueError("Invalid controlled event body size.")
        if (
            not isinstance(signature, str)
            or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature)
            or not hmac.compare_digest(
                signature[7:], hmac.new(self.secret, raw, hashlib.sha256).hexdigest()
            )
        ):
            # Authentication/decoding has no ORM environment or cursor.
            # pylint: disable-next=prefer-env-translation
            raise AccessError(_lt("Controlled event authentication failed."))
        try:
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(body, dict) or set(body) != REQUIRED_KEYS:
                raise ValueError
            if any(
                not isinstance(body[key], str) or not 0 < len(body[key]) <= 256
                for key in REQUIRED_KEYS - {"revision", "sequence"}
            ):
                raise ValueError
            if any(
                type(body[key]) is not int or not 1 <= body[key] <= 2147483647
                for key in ("revision", "sequence")
            ):
                raise ValueError
            if body["contract_version"] != "kmee-controlled-events-1" or (
                body["state"] not in DIMENSION_STATES.get(body["dimension"], set())
            ):
                raise ValueError
            # Contract timestamps are UTC; Odoo stores naive UTC datetimes.
            body["occurred_at"] = datetime.strptime(
                body["occurred_at"] + " +0000", "%Y-%m-%d %H:%M:%S %z"
            ).replace(tzinfo=None)
        except (ValueError, UnicodeError, TypeError, RecursionError) as error:
            raise ValueError("Invalid controlled event contract.") from error
        return body

    def receive(self, binding_id, raw_body, signature):
        body = self._decode(raw_body, signature)
        for attempt in range(3):
            try:
                return self._persist(binding_id, body, raw_body)
            except (SerializationFailure, UniqueViolation) as error:
                if isinstance(
                    error, UniqueViolation
                ) and error.diag.constraint_name not in {
                    "l10n_ae_edi_event_event_identity_unique",
                    "l10n_ae_edi_event_event_bytes_unique",
                }:
                    raise
                if attempt == 2:
                    raise
        return None  # The bounded loop returns or raises.

    def _persist(self, binding_id, body, raw_body):
        with self.registry.cursor() as cursor:
            env = api.Environment(
                cursor, self.uid, {"allowed_company_ids": [self.company_id]}
            )
            if not (
                env.user.has_group("l10n_ae_edi.group_event_receiver")
                or env.user.has_group("account.group_account_manager")
            ):
                raise AccessError(env._("UAE event receiving permission is required."))
            binding = env["l10n_ae.edi.binding"].browse(binding_id).exists()
            binding.ensure_one()
            binding.check_access("read")
            if binding.company_id.id != self.company_id:
                raise AccessError(env._("The UAE binding belongs to another company."))
            if binding.environment != "sandbox":
                raise UserError(env._("Controlled events require a sandbox binding."))
            cursor.execute(
                "SELECT id FROM l10n_ae_edi_binding WHERE id = %s FOR UPDATE",
                [binding.id],
            )
            record = env["l10n_ae.edi.transmission"].search(
                [
                    ("binding_id", "=", binding.id),
                    ("company_id", "=", self.company_id),
                    ("idempotency_key", "=", body["request_key"]),
                ],
                limit=1,
            )
            if not record or record.submission_state != "accepted":
                raise UserError(
                    env._("Reconcile API acceptance before applying UAE events.")
                )
            record.check_access("read")
            cursor.execute(
                "SELECT id FROM l10n_ae_edi_transmission WHERE id = %s FOR UPDATE",
                [record.id],
            )
            record.invalidate_recordset()
            if any(
                body[name] != value
                for name, value in {
                    "provider": record.provider,
                    "tin": json.loads(record.payload)["company"]["tin"],
                    "remote_id": record.remote_id,
                    "revision": record.revision,
                    "snapshot_sha256": record.payload_hash,
                    "xml_sha256": record.xml_hash,
                }.items()
            ):
                raise UserError(env._("Controlled UAE event correlation failed."))
            digest = hashlib.sha256(raw_body).hexdigest()
            previous = env["l10n_ae.edi.event"].search(
                [
                    ("binding_id", "=", binding.id),
                    ("event_id", "=", body["event_id"]),
                ]
            )
            if previous.filtered(lambda item: item.body_hash == digest):
                return "duplicate"
            outcome = "replay_conflict" if previous else "applied"
            current_fact = env["l10n_ae.edi.event"].search(
                [
                    ("transmission_id", "=", record.id),
                    ("dimension", "=", body["dimension"]),
                    ("outcome", "=", "applied"),
                ],
                order="sequence desc, id desc",
                limit=1,
            )
            if not previous and record[body["dimension"]] == "not_applicable":
                outcome = "unexpected"
            elif not previous and current_fact:
                if body["sequence"] < current_fact.sequence:
                    outcome = "obsolete"
                elif body["state"] != current_fact.fact_state:
                    outcome = "conflict"
            event = (
                env["l10n_ae.edi.event"]
                .sudo()
                .create(
                    {
                        "company_id": self.company_id,
                        "binding_id": binding.id,
                        "transmission_id": record.id,
                        "event_id": body["event_id"],
                        "dimension": body["dimension"],
                        "fact_state": body["state"],
                        "sequence": body["sequence"],
                        "occurred_at": body["occurred_at"],
                        "received_at": fields.Datetime.now(),
                        "original_body": raw_body.decode("utf-8"),
                        "body_hash": digest,
                        "outcome": outcome,
                    }
                )
            )
            if outcome in {"replay_conflict", "conflict", "unexpected"}:
                (
                    record | previous.transmission_id
                ).sudo()._mark_controlled_event_review()
            elif outcome == "applied":
                record.sudo()._apply_controlled_event(event)
            cursor.commit()
            return outcome
