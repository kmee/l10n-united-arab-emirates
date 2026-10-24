# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json

from odoo import api, fields, models
from odoo.exceptions import UserError


class UaeEdiInboxReview(models.Model):
    _name = "l10n_ae.edi.inbox.review"
    _description = "UAE Received Document Processing History"
    _check_company_auto = True
    _order = "id"
    _rec_name = "outcome"

    inbox_id = fields.Many2one(
        "l10n_ae.edi.inbox",
        required=True,
        readonly=True,
        check_company=True,
        ondelete="restrict",
        index=True,
    )
    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, index=True
    )
    operator_id = fields.Many2one(
        "res.users", required=True, readonly=True, ondelete="restrict"
    )
    occurred_at = fields.Datetime(required=True, readonly=True)
    xml_hash = fields.Char(string="Retained XML SHA-256", required=True, readonly=True)
    normative_ruleset = fields.Char(
        string="Selected PINT Package", readonly=True, copy=False
    )
    normative_manifest_hash = fields.Char(
        string="Selected Manifest SHA-256", size=64, readonly=True, copy=False
    )
    outcome = fields.Selection(
        [("review", "Requires Review"), ("processed", "Processed")],
        required=True,
        readonly=True,
    )
    diagnostic = fields.Text(readonly=True)
    validation_report = fields.Text(string="Retained PINT Validation", readonly=True)
    validation_report_hash = fields.Char(
        string="Validation Report SHA-256", readonly=True
    )
    move_id = fields.Many2one(
        "account.move",
        string="Accounting Document",
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )

    @api.model_create_multi
    def create(self, vals_list):
        # pylint: disable=method-required-super
        raise UserError(
            self.env._("Received processing history is recorded by the processor.")
        )

    def write(self, vals):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Received processing history is immutable."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_processing_evidence(self):
        raise UserError(self.env._("Received processing history cannot be deleted."))

    def _record(
        self,
        inbox,
        outcome,
        diagnostic=False,
        move=False,
        validation_report=False,
        selected_package=False,
    ):
        inbox.ensure_one()
        inbox = self.env["l10n_ae.edi.inbox"].browse(inbox.id)
        inbox._check_operator()
        inbox.check_access("read")
        inbox._check_receiving_company(inbox.company_id)
        # Processing evidence must not inherit caller-supplied ORM defaults.
        # pylint: disable=context-overridden
        history = self.sudo().with_context(
            {"allowed_company_ids": [inbox.company_id.id]}
        )
        report_text = (
            json.dumps(
                validation_report,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if validation_report
            else False
        )
        return (
            super(UaeEdiInboxReview, history)
            .create(
                {
                    "inbox_id": inbox.id,
                    "company_id": inbox.company_id.id,
                    "operator_id": self.env.uid,
                    "occurred_at": fields.Datetime.now(),
                    "xml_hash": inbox.xml_hash,
                    "normative_ruleset": (selected_package or {}).get("ruleset"),
                    "normative_manifest_hash": (selected_package or {}).get(
                        "manifest_sha256"
                    ),
                    "outcome": outcome,
                    "diagnostic": diagnostic,
                    "validation_report": report_text,
                    "validation_report_hash": (
                        hashlib.sha256(report_text.encode("utf-8")).hexdigest()
                        if report_text
                        else False
                    ),
                    "move_id": move.id if move else False,
                }
            )
            .with_env(self.env)
        )
