# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import io
import json
import zipfile

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.binary import BinaryBytes


def _received_metadata(records, names):
    records.check_access("read")
    result = []
    for record in records.sorted("id"):
        values = {"id": record.id}
        for name in names:
            value = record[name]
            if record._fields[name].type == "many2one":
                value = value.id or False
            elif record._fields[name].type == "datetime":
                value = fields.Datetime.to_string(value) if value else False
            values[name] = value
        result.append(values)
    return result


class UaeEdiInboxEvidence(models.Model):
    _inherit = "l10n_ae.edi.inbox"

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
    )
    def _compute_received_evidence_archive(self):
        for inbox in self:
            inbox.evidence_archive = BinaryBytes(
                inbox._build_received_evidence_archive()
            )

    def _build_received_evidence_archive(self):
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
            "relationships": {"processing_history": history.ids},
            "artifacts": {
                name: {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
                for name, content in sorted(artifacts.items())
            },
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
