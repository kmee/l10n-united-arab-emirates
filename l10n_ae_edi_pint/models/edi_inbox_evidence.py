# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import io
import json
import zipfile

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.binary import BinaryBytes

from .edi_inbox import UaeEdiInbox
from .edi_inbox_identity import UaeInboxIdentityDecision


def _received_metadata(records, names):
    records.check_access("read")
    result = []
    for record in records.sorted("id"):
        values = {"id": record.id}
        for name in names:
            value = record[name]
            if record._fields[name].type == "many2one":
                value = value.id or False
            elif record._fields[name].type == "many2many":
                value = value.ids
            elif record._fields[name].type == "datetime":
                value = fields.Datetime.to_string(value) if value else False
            values[name] = value
        result.append(values)
    return result


class UaeEdiInboxEvidence(models.Model):
    _inherit = "l10n_ae.edi.inbox"

    retention_ids = fields.One2many("l10n_ae.edi.retention", "inbox_id", readonly=True)

    def get_retention_status(self, as_of=None):
        self.ensure_one()
        return self.env["l10n_ae.edi.retention"]._assess(self, as_of, "inbox_id")

    evidence_archive = fields.Binary(
        string="Received Evidence Package",
        compute="_compute_received_evidence_archive",
        compute_sudo=False,
        attachment=False,
        groups="base.group_allow_export",
    )
    evidence_archive_filename = fields.Char(
        compute="_compute_received_evidence_filename", compute_sudo=False
    )

    def _compute_received_evidence_filename(self):
        for inbox in self:
            inbox.evidence_archive_filename = f"uae-received-evidence-{inbox.id}.zip"

    @api.depends_context("uid", "allowed_company_ids")
    @api.depends(
        "xml_hash",
        "state",
        "diagnostic",
        "identity_conflict",
        "move_id",
        "credit_reference_snapshot",
        "processing_history_ids",
        "processing_history_ids.validation_report_hash",
        "retention_ids",
        "retention_ids.write_date",
        "retention_ids.hold_ids",
        "retention_ids.hold_ids.write_date",
    )
    def _compute_received_evidence_archive(self):
        for inbox in self:
            inbox.evidence_archive = BinaryBytes(
                inbox._build_received_evidence_archive()
            )

    def _build_received_evidence_archive(self, include_identity=True):
        self.ensure_one()
        principal = self.env.user.with_user(self.env.uid).sudo(False)
        if not principal.active or not principal.has_group("base.group_allow_export"):
            raise AccessError(self.env._("Native Data Export permission is required."))
        inbox = self.sudo(False)
        inbox.check_access("read")
        # Discover only IDs linked to an already authorized original. Check every
        # row with actual rights, so record rules cannot silently omit evidence.
        history = (
            inbox.env["l10n_ae.edi.inbox.review"]
            .sudo()
            .search([("inbox_id", "=", inbox.id)], order="id")
            .with_env(inbox.env)
        )
        history.check_access("read")
        artifacts = {}

        def preserve(name, content, expected_hash=None):
            digest = hashlib.sha256(content).hexdigest()
            if expected_hash is not None and digest != expected_hash:
                raise UserError(
                    self.env._("UAE fiscal evidence integrity check failed: %s", name)
                )
            artifacts[name] = content

        def preserve_json(name, value):
            preserve(
                name,
                json.dumps(
                    value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8"),
            )

        if not inbox.original_xml or not inbox.xml_hash:
            raise UserError(self.env._("Original received XML and hash are required."))
        preserve("document.xml", inbox.original_xml.content, inbox.xml_hash)
        receipt = _received_metadata(
            inbox,
            [
                "company_id",
                "binding_id",
                "remote_id",
                "received_at",
                "state",
                "diagnostic",
                "identity_conflict",
                "move_id",
                "xml_hash",
            ],
        )[0]
        preserve_json("receipt.json", receipt)
        metadata = _received_metadata(
            history,
            [
                "inbox_id",
                "company_id",
                "operator_id",
                "occurred_at",
                "xml_hash",
                "outcome",
                "diagnostic",
                "move_id",
                "validation_report_hash",
            ],
        )
        for row, values in zip(history, metadata, strict=True):
            if row.company_id != inbox.company_id or row.xml_hash != inbox.xml_hash:
                raise UserError(
                    self.env._("Received processing evidence is inconsistent.")
                )
            report, report_hash = row.validation_report, row.validation_report_hash
            if bool(report) != bool(report_hash):
                raise UserError(
                    self.env._("A retained validation report or hash is missing.")
                )
            values["validation_available"] = bool(report)
            if report:
                preserve(
                    f"validation-reports/{row.id}.json",
                    report.encode("utf-8"),
                    report_hash,
                )
        preserve_json("processing-history.json", metadata)
        preserve_json("credit-references.json", inbox.credit_reference_snapshot or [])
        retention = inbox.env["l10n_ae.edi.retention"]._evidence_appendix(
            inbox, "inbox_id"
        )
        preserve_json("retention.json", retention)
        manifest = {
            "format": "kmee-uae-evidence-1",
            "scope": "received_original",
            "identity": {
                "inbox_id": inbox.id,
                "company_id": inbox.company_id.id,
                "binding_id": inbox.binding_id.id,
                "remote_id": inbox.remote_id,
                "move_id": inbox.move_id.id or False,
            },
            "relationships": {
                "processing_history": history.ids,
                "retention_obligations": [
                    row["id"] for row in retention["obligations"]
                ],
                "retention_holds": [row["id"] for row in retention["holds"]],
            },
            "artifacts": {
                name: {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
                for name, content in sorted(artifacts.items())
            },
        }
        if include_identity and inbox.identity_conflict:
            family = inbox._identity_family().with_env(inbox.env)
            family.check_access("read")
            decisions = (
                inbox.env["l10n_ae.edi.inbox.decision"]
                .sudo()
                .search(inbox._identity_decision_domain(), order="id")
                .with_env(inbox.env)
            )
            decisions.check_access("read")
            for decision in decisions:
                members = decision.member_ids.sorted("id")
                if (
                    not members
                    or members - family
                    or decision.member_snapshot != inbox._identity_snapshot(members)
                    or decision.selected_inbox_id not in members
                    or decision.selected_xml_hash != decision.selected_inbox_id.xml_hash
                ):
                    raise UserError(
                        self.env._("Received identity review evidence is inconsistent.")
                    )
            selected = family.filtered(lambda row: row._identity_selection_is_current())
            review = {
                "format": "kmee-uae-identity-review-1",
                "identity": {
                    "company_id": inbox.company_id.id,
                    "binding_id": inbox.binding_id.id,
                    "remote_id": inbox.remote_id,
                },
                "originals": inbox._identity_snapshot(family),
                "selected_inbox_id": selected.id if len(selected) == 1 else False,
                "review_pending": inbox.identity_review_pending,
                "decisions": _received_metadata(
                    decisions,
                    [
                        "company_id",
                        "binding_id",
                        "remote_id",
                        "selected_inbox_id",
                        "selected_xml_hash",
                        "member_ids",
                        "member_snapshot",
                        "operator_id",
                        "occurred_at",
                        "justification",
                        "review_reference",
                    ],
                ),
            }
            preserve_json("identity-review.json", review)
            for original in family - inbox:
                preserve(
                    f"identity-originals/{original.id}.zip",
                    original._build_received_evidence_archive(include_identity=False),
                )
            manifest["relationships"].update(
                {
                    "identity_originals": family.ids,
                    "identity_decisions": decisions.ids,
                }
            )
            manifest["artifacts"] = {
                name: {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
                for name, content in sorted(artifacts.items())
            }
        preserve_json("manifest.json", manifest)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, content in sorted(artifacts.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100600 << 16
                archive.writestr(info, content)
        return output.getvalue()

    identity_review_pending = fields.Boolean(
        compute="_compute_identity_review",
    )
    identity_decision_ids = fields.Many2many(
        "l10n_ae.edi.inbox.decision",
        string="Internal Identity Selections",
        compute="_compute_identity_review",
    )

    def _identity_family(self):
        self.ensure_one()
        return self.sudo().search(
            [
                ("company_id", "=", self.company_id.id),
                ("binding_id", "=", self.binding_id.id),
                ("remote_id", "=", self.remote_id),
            ],
            order="id",
        )

    def _identity_decision_domain(self):
        self.ensure_one()
        return [
            ("company_id", "=", self.company_id.id),
            ("binding_id", "=", self.binding_id.id),
            ("remote_id", "=", self.remote_id),
        ]

    def _identity_snapshot(self, family):
        return [{"inbox_id": row.id, "xml_hash": row.xml_hash} for row in family]

    def _identity_selection_is_current(self):
        self.ensure_one()
        if not self.identity_conflict:
            return True
        current = self.with_user(self.env.uid).sudo(False)
        family = current._identity_family()
        readable = family.with_env(current.env)
        if not readable.has_access("read"):
            return False
        decisions = current.env["l10n_ae.edi.inbox.decision"]
        latest = decisions.sudo().search(current._identity_decision_domain(), limit=1)
        if not latest or not latest.with_env(current.env).has_access("read"):
            return False
        linked = family.filtered("move_id")
        if linked and not linked.with_env(current.env).move_id.has_access("read"):
            return False
        return (
            latest.selected_inbox_id.id == current.id
            and latest.member_snapshot == current._identity_snapshot(family)
            and (not linked or linked.ids == current.ids)
        )

    @api.depends_context("uid", "allowed_company_ids")
    @api.depends("identity_conflict", "binding_id", "remote_id", "move_id")
    def _compute_identity_review(self):
        decisions = self.env["l10n_ae.edi.inbox.decision"]
        for inbox in self:
            inbox.identity_review_pending = not inbox._identity_selection_is_current()
            inbox.identity_decision_ids = (
                decisions.search(inbox._identity_decision_domain())
                if inbox.identity_conflict
                else decisions
            )

    def action_select_identity_variant(
        self, justification, review_reference, expected_snapshot=None
    ):
        """Record internal eligibility without importing or fiscal acknowledgement."""
        self.ensure_one()
        current = self.with_user(self.env.uid).sudo(False)
        current._check_operator()
        if current.env.user.share:
            raise AccessError(
                current.env._("An internal Accounting Manager is required.")
            )
        current.check_access("read")
        current._check_receiving_company(current.company_id)
        current.binding_id.check_access("read")
        current.binding_id._check_transport_allowed()
        if (
            not isinstance(justification, str)
            or not justification.strip()
            or len(justification) > 4096
            or not isinstance(review_reference, str)
            or not review_reference.strip()
            or len(review_reference) > 512
        ):
            raise UserError(
                current.env._("Provide a justification and review evidence reference.")
            )
        current.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_binding WHERE id=%s FOR UPDATE",
            [current.binding_id.id],
        )
        current.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_inbox WHERE id=%s FOR UPDATE",
            [current.id],
        )
        current.invalidate_recordset()
        family = current._identity_family()
        family.with_env(current.env).check_access("read")
        if not current.identity_conflict or len(family) < 2:
            raise UserError(
                current.env._("At least two conflicting originals are required.")
            )
        linked = family.filtered("move_id")
        linked.with_env(current.env).move_id.check_access("read")
        if linked and linked.ids != current.ids:
            raise UserError(
                current.env._(
                    "Select the already linked original; "
                    "multiple links require escalation."
                )
            )
        if linked:
            current.move_id.check_access("read")
        for member in family:
            if (
                hashlib.sha256(member.original_xml.content).hexdigest()
                != member.xml_hash
            ):
                raise UserError(
                    current.env._("Received XML failed its integrity check.")
                )
        snapshot = current._identity_snapshot(family)
        if expected_snapshot is not None and expected_snapshot != snapshot:
            raise UserError(
                current.env._("Received originals changed. Reopen the identity review.")
            )
        decisions = current.env["l10n_ae.edi.inbox.decision"]
        latest = decisions.sudo().search(current._identity_decision_domain(), limit=1)
        if latest:
            latest = latest.with_env(current.env)
            latest.check_access("read")
            if (
                latest.selected_inbox_id == current
                and latest.member_snapshot == snapshot
                and latest.justification == justification.strip()
                and latest.review_reference == review_reference.strip()
            ):
                return latest
        # Actual decision facts never inherit caller defaults.
        # pylint: disable=context-overridden
        recorder = decisions.with_context(
            {"allowed_company_ids": current.company_id.ids}
        )
        decision = super(UaeInboxIdentityDecision, recorder).create(
            {
                "company_id": current.company_id.id,
                "binding_id": current.binding_id.id,
                "remote_id": current.remote_id,
                "selected_inbox_id": current.id,
                "selected_xml_hash": current.xml_hash,
                "member_ids": [fields.Command.set(family.ids)],
                "member_snapshot": snapshot,
                "operator_id": current.env.uid,
                "occurred_at": fields.Datetime.now(),
                "justification": justification.strip(),
                "review_reference": review_reference.strip(),
            }
        )
        # PostgreSQL repeatable-read must retry any older selection/import snapshot.
        # All family originals are touched under the common binding lock; their
        # bytes, links and historical conflict fact remain unchanged.
        super(UaeEdiInbox, family.sudo()).write({"identity_conflict": True})
        family.flush_recordset(["identity_conflict", "write_date"])
        family.invalidate_recordset(
            ["identity_review_pending", "identity_decision_ids"]
        )
        return decision

    def action_review_identity_variant(self):
        self.ensure_one()
        current = self.with_user(self.env.uid).sudo(False)
        current._check_operator()
        current.check_access("read")
        current._check_receiving_company(current.company_id)
        current.binding_id.check_access("read")
        current.binding_id._check_transport_allowed()
        current._identity_family().with_env(current.env).check_access("read")
        if not current.identity_conflict:
            raise UserError(
                current.env._("Select an original with a remote ID conflict.")
            )
        # pylint: disable=context-overridden
        wizard = (
            current.env["l10n_ae.edi.inbox.selection"]
            .with_context({"allowed_company_ids": current.company_id.ids})
            .create({"inbox_id": current.id})
        )
        return {
            "type": "ir.actions.act_window",
            "name": current.env._("Review Identity Variant"),
            "res_model": "l10n_ae.edi.inbox.selection",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
            "context": {"allowed_company_ids": current.company_id.ids},
        }
