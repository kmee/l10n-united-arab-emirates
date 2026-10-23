# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import base64
import binascii
import hashlib
import json
import re
from typing import ClassVar

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.binary import BinaryBytes


class UaeEdiIncidentNotice(models.Model):
    _name = "l10n_ae.edi.incident.notice"
    _description = "UAE Supplied Incident Notice Evidence"
    _rec_name = "external_reference"
    _check_company_auto = True

    incident_id = fields.Many2one(
        "l10n_ae.edi.incident",
        required=True,
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    evidence_key = fields.Char(required=True, readonly=True)
    evidence_origin = fields.Selection(
        [("simulated", "Controlled Simulation"), ("external", "Externally Supplied")],
        required=True,
        readonly=True,
    )
    notice_purpose = fields.Selection(
        [
            ("taxpayer_system_failure", "Taxpayer System Failure Notice to FTA"),
            ("asp_service_disruption", "ASP Service Disruption Notice"),
            ("taxpayer_asp_contact", "Taxpayer Contact to ASP"),
            ("taxpayer_registration", "Taxpayer Registration Amendment Notice to ASP"),
        ],
        required=True,
        readonly=True,
    )
    sender_role = fields.Selection(
        [("taxpayer", "Taxpayer"), ("asp", "ASP")], required=True, readonly=True
    )
    sender_name = fields.Char(required=True, readonly=True)
    taxpayer_roles = fields.Selection(
        [("issuer", "Issuer"), ("recipient", "Recipient"), ("both", "Both")],
        readonly=True,
    )
    recipient_role = fields.Selection(
        [("fta", "FTA"), ("asp", "ASP"), ("taxpayer", "Taxpayer")],
        required=True,
        readonly=True,
    )
    recipient_name = fields.Char(required=True, readonly=True)
    recipient_address = fields.Text(required=True, readonly=True)
    channel = fields.Selection(
        [
            ("email", "Email"),
            ("portal", "Portal"),
            ("letter", "Letter"),
            ("other", "Other"),
        ],
        required=True,
        readonly=True,
    )
    sent_at = fields.Datetime(
        string="Declared Sent At",
        required=True,
        readonly=True,
        help="Declared by the supplied artifact; this does not verify delivery.",
    )
    external_reference = fields.Text(required=True, readonly=True)
    review_note = fields.Text(required=True, readonly=True)
    document_content = fields.Binary(
        required=True, readonly=True, attachment=True, groups="base.group_system"
    )
    document = fields.Binary(
        compute="_compute_document", compute_sudo=False, attachment=False
    )
    document_filename = fields.Char(required=True, readonly=True)
    document_sha256 = fields.Char(required=True, readonly=True)
    evidence_hash = fields.Char(required=True, readonly=True)
    recorded_by_id = fields.Many2one(
        "res.users", required=True, readonly=True, ondelete="restrict"
    )
    recorded_at = fields.Datetime(required=True, readonly=True)

    _notice_key_unique = models.UniqueIndex(
        "(incident_id, evidence_key)", "This incident notice key already exists."
    )
    _required_names: ClassVar[set[str]] = {
        "evidence_key",
        "evidence_origin",
        "sender_role",
        "sender_name",
        "recipient_role",
        "recipient_name",
        "recipient_address",
        "channel",
        "sent_at",
        "external_reference",
        "review_note",
        "document_filename",
        "document",
    }
    _max_document_bytes = 10 * 1024 * 1024

    @api.depends_context("uid", "allowed_company_ids")
    @api.depends("document_content", "document_sha256", "incident_id")
    def _compute_document(self):
        for record in self:
            actual = record.with_user(self.env.uid)
            actual.check_access("read")
            actual.incident_id._check_actor("read")
            original = actual.sudo().document_content
            content = original.content if original else b""
            if hashlib.sha256(content).hexdigest() != actual.document_sha256:
                raise UserError(
                    self.env._("The stored notice failed integrity review.")
                )
            record.document = BinaryBytes(content)

    @api.model_create_multi
    def create(self, vals_list):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Use the incident notice evidence action."))

    def write(self, vals):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Supplied notice evidence is immutable."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_notice_evidence(self):
        raise UserError(self.env._("Supplied notice evidence cannot be deleted."))

    @api.model
    def _normalize_notice(self, incident, evidence):
        if not isinstance(evidence, dict) or (
            self._required_names.difference(evidence)
            or set(evidence).difference(self._required_names | {"taxpayer_roles"})
        ):
            raise ValidationError(self.env._("Supply the complete notice evidence."))
        values = {}
        for name in self._required_names - {"document"}:
            value = evidence[name]
            if not isinstance(value, str) or not value.strip() or len(value) > 10000:
                raise ValidationError(
                    self.env._("Supply a valid notice value for %s.", name)
                )
            values[name] = value.strip()
        if values["evidence_origin"] not in {"simulated", "external"} or values[
            "channel"
        ] not in {"email", "portal", "letter", "other"}:
            raise ValidationError(self.env._("Review the evidence origin and channel."))
        filename = values["document_filename"]
        if (
            len(values["evidence_key"]) > 128
            or len(filename) > 255
            or filename in {".", ".."}
            or re.search(r"[/\\\x00-\x1f\x7f]", filename)
        ):
            raise ValidationError(self.env._("Use a stable key and a plain filename."))
        pair = (values["sender_role"], values["recipient_role"])
        if incident.incident_kind == "registration_change":
            if pair != ("taxpayer", "asp"):
                raise ValidationError(
                    self.env._("Registration notice evidence must be taxpayer to ASP.")
                )
            purpose = "taxpayer_registration"
        else:
            purpose = {
                ("taxpayer", "fta"): "taxpayer_system_failure",
                ("taxpayer", "asp"): "taxpayer_asp_contact",
                ("asp", "fta"): "asp_service_disruption",
                ("asp", "taxpayer"): "asp_service_disruption",
            }.get(pair)
            if not purpose:
                raise ValidationError(self.env._("Review the notice sender/recipient."))
        roles = evidence.get("taxpayer_roles")
        roles = False if roles is None else roles
        if values["sender_role"] == "taxpayer":
            if (
                not isinstance(roles, str)
                or roles not in {"issuer", "recipient", "both"}
                or (
                    incident.incident_kind == "system_failure"
                    and incident.affected_roles != "both"
                    and roles != incident.affected_roles
                )
            ):
                raise ValidationError(
                    self.env._("Review explicit taxpayer notice roles.")
                )
        elif roles is not False:
            raise ValidationError(
                self.env._("An ASP's own notice cannot take taxpayer roles.")
            )
        values.update(notice_purpose=purpose, taxpayer_roles=roles)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", values["sent_at"]):
            raise ValidationError(self.env._("Use a declared UTC sent timestamp."))
        try:
            sent_at = fields.Datetime.to_datetime(values["sent_at"])
        except (ValueError, TypeError) as exc:
            raise ValidationError(
                self.env._("Use a declared UTC sent timestamp.")
            ) from exc
        anchor_at = (
            incident.fta_confirmation_received_at
            if incident.incident_kind == "registration_change"
            else incident.occurrence_at
        )
        if not anchor_at <= sent_at <= fields.Datetime.now():
            raise ValidationError(
                self.env._("Review the declared sent time against the incident event.")
            )
        values["sent_at"] = fields.Datetime.to_string(sent_at)
        content = self._decode_notice_document(evidence["document"])
        values["document_sha256"] = hashlib.sha256(content).hexdigest()
        values["evidence_hash"] = hashlib.sha256(
            json.dumps(values, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        values["document_content"] = BinaryBytes(content)
        return values

    @api.model
    def _decode_notice_document(self, encoded):
        if (
            not isinstance(encoded, str)
            or len(encoded) > ((self._max_document_bytes + 2) // 3) * 4
        ):
            raise ValidationError(self.env._("Supply a Base64 document up to 10 MiB."))
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError(
                self.env._("Supply a valid Base64 document.")
            ) from exc
        if not content or len(content) > self._max_document_bytes:
            raise ValidationError(
                self.env._("Supply a nonempty document up to 10 MiB.")
            )
        return content

    @api.model
    def _record(self, incident, evidence):
        incident._check_actor("write")
        if incident.state != "open":
            raise UserError(
                self.env._(
                    "Open the reviewed incident before recording notice evidence."
                )
            )
        values = self._normalize_notice(incident, evidence)
        existing = self.with_user(self.env.uid).search(
            [
                ("incident_id", "=", incident.id),
                ("evidence_key", "=", values["evidence_key"]),
            ],
            limit=1,
        )
        if existing:
            if existing.evidence_hash != values["evidence_hash"]:
                raise UserError(self.env._("This notice key has conflicting evidence."))
            return existing
        # Parent action has checked the actual actor, company and reviewed case.
        # Elevation persists only immutable evidence, never an accounting entry.
        return (
            super(UaeEdiIncidentNotice, self.sudo().with_company(incident.company_id))
            .create(
                {
                    **values,
                    "incident_id": incident.id,
                    "company_id": incident.company_id.id,
                    "recorded_by_id": self.env.uid,
                    "recorded_at": fields.Datetime.now(),
                }
            )
            .sudo(False)
        )
