# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import io
import json
import zipfile

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.binary import BinaryBytes


def _evidence_metadata(records, names):
    records.check_access("read")
    # No display names or live accounting/configuration are traversed.
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


class UaeEdiEvidence(models.Model):
    _inherit = "l10n_ae.edi.transmission"

    evidence_archive = fields.Binary(
        string="Fiscal Evidence Package",
        compute="_compute_evidence_archive",
        compute_sudo=False,
        attachment=False,
        groups="base.group_allow_export",
    )
    evidence_archive_filename = fields.Char(
        compute="_compute_evidence_archive_filename", compute_sudo=False
    )

    @api.depends("revision")
    def _compute_evidence_archive_filename(self):
        for issue in self:
            issue.evidence_archive_filename = (
                f"uae-evidence-{issue.id}-r{issue.revision}.zip"
            )

    @api.depends_context("uid", "allowed_company_ids")
    def _compute_evidence_archive(self):
        for issue in self:
            content = issue._build_evidence_archive()
            issue.evidence_archive = BinaryBytes(content) if content else False

    def write(self, vals):
        if {"evidence_archive", "evidence_archive_filename"}.intersection(vals):
            raise UserError(
                self.env._("Fiscal evidence export projections are readonly.")
            )
        return super().write(vals)

    def _build_evidence_archive(self):
        self.ensure_one()
        # Apply the actual actor's native rights even to an elevated projection.
        principal = self.env.user.with_user(self.env.uid).sudo(False)
        if not principal.active or not principal.has_group("base.group_allow_export"):
            raise AccessError(self.env._("Native Data Export permission is required."))
        issue = self.sudo(False)
        issue.check_access("read")
        if not issue.xml_content and not issue.xml_hash:
            # A core-only preparation remains inspectable without inventing XML.
            return None
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
                    value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode(),
            )

        if not issue.xml_content or not issue.xml_hash or not issue.payload_hash:
            raise UserError(
                self.env._("Original UAE snapshot and XML are required for export.")
            )
        preserve("snapshot.json", issue.payload.encode(), issue.payload_hash)
        preserve("document.xml", issue.xml_content.encode(), issue.xml_hash)
        attempts = issue.attempt_ids
        preserve_json(
            "attempts.json",
            _evidence_metadata(
                attempts,
                [
                    "company_id",
                    "transmission_id",
                    "operation",
                    "started_at",
                    "completed_at",
                    "state",
                    "error_kind",
                    "receipt",
                    "validation_receipt",
                    "create_uid",
                ],
            ),
        )
        events = issue.event_ids
        event_metadata = _evidence_metadata(
            events,
            [
                "company_id",
                "transmission_id",
                "binding_id",
                "event_id",
                "dimension",
                "fact_state",
                "sequence",
                "occurred_at",
                "received_at",
                "outcome",
                "body_hash",
            ],
        )
        preserve_json("events.json", event_metadata)
        for event in events.sorted("id"):
            preserve(
                f"events/{event.id}.json", event.original_body.encode(), event.body_hash
            )
        receipts = issue.env["l10n_ae.edi.billing.request"].search(
            [
                ("move_id", "=", issue.move_id.id),
                ("company_id", "=", issue.company_id.id),
                ("environment", "=", issue.environment),
                ("process", "=", issue.process),
                ("origin_system", "=", issue.origin_system),
                ("origin_document_id", "=", issue.origin_document_id),
            ]
        )
        preserve_json(
            "billing-receipts.json",
            _evidence_metadata(
                receipts,
                [
                    "company_id",
                    "move_id",
                    "environment",
                    "process",
                    "origin_system",
                    "origin_document_id",
                    "request_hash",
                    "create_date",
                    "create_uid",
                ],
            ),
        )
        corrections = issue.env["l10n_ae.edi.correction"].search(
            [
                ("company_id", "=", issue.company_id.id),
                "|",
                ("source_transmission_id", "=", issue.id),
                ("replacement_transmission_id", "=", issue.id),
            ]
        )
        preserve_json(
            "corrections.json",
            _evidence_metadata(
                corrections,
                [
                    "company_id",
                    "move_id",
                    "source_transmission_id",
                    "replacement_transmission_id",
                    "reason",
                    "started_by",
                    "started_at",
                    "completed_at",
                    "state",
                ],
            ),
        )
        optional = {"conventional_html": False, "conventional_pdf": False}
        if issue.conventional_html_content:
            if not issue.conventional_html_hash:
                raise UserError(
                    self.env._("The preserved invoice presentation has no hash.")
                )
            preserve(
                "conventional.html",
                issue.conventional_html_content.encode(),
                issue.conventional_html_hash,
            )
            optional["conventional_html"] = True
        if issue.conventional_pdf_content:
            if not issue.conventional_pdf_hash:
                raise UserError(self.env._("The preserved invoice PDF has no hash."))
            preserve(
                "conventional.pdf",
                issue.conventional_pdf_content.content,
                issue.conventional_pdf_hash,
            )
            optional["conventional_pdf"] = True
        identity = {
            name: issue[name]
            for name in [
                "provider",
                "environment",
                "process",
                "origin_system",
                "origin_document_id",
                "revision",
            ]
        }
        identity.update(
            transmission_id=issue.id,
            company_id=issue.company_id.id,
            move_id=issue.move_id.id,
            binding_id=issue.binding_id.id,
        )
        manifest = {
            "format": "kmee-uae-evidence-1",
            "scope": "issued_revision",
            "identity": identity,
            "facts": {
                name: issue[name]
                for name in [
                    "exchange_mode",
                    "submission_state",
                    "remote_id",
                    "validation_c2_state",
                    "delivery_c3_state",
                    "reporting_c2_state",
                    "reporting_c3_state",
                    "confirmation_state",
                    "event_review_required",
                    "dispatch_review_required",
                ]
            },
            "relationships": {
                "attempts": attempts.sorted("id").ids,
                "events": events.sorted("id").ids,
                "billing_receipts": receipts.sorted("id").ids,
                "local_corrections": corrections.sorted("id").ids,
            },
            "optional_artifacts": optional,
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
