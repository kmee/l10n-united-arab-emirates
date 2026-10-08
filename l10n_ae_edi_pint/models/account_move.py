# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
import os
import re

from odoo import api, fields, models
from odoo.exceptions import UserError

from odoo.addons.base.models.res_partner_bank import sanitize_account_number

from ..tools.validation import (
    DEFAULT_RULESET,
    OfflinePintValidator,
    ValidationUnavailable,
    describe_ruleset,
)
from .account_tax import UAE_EXEMPTION_REASONS
from .account_payment_method import UAE_PAYMENT_MEANS


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_ae_received_inbox_ids = fields.One2many(
        "l10n_ae.edi.inbox", "move_id", readonly=True
    )
    l10n_ae_received_credit_reference_summary = fields.Text(
        string="Received Invoice References",
        compute="_compute_l10n_ae_received_credit_reference_summary",
    )
    l10n_ae_received_delivery_summary = fields.Text(
        string="Received Delivery", compute="_compute_l10n_ae_received_delivery_summary"
    )
    l10n_ae_normative_ruleset = fields.Char(
        string="Pinned PINT Package",
        compute="_compute_l10n_ae_normative_identity",
        compute_sudo=False,
        help="Pinned at preparation; does not prove validation or acceptance.",
    )
    l10n_ae_normative_manifest_sha256 = fields.Char(
        string="Pinned Manifest SHA-256",
        compute="_compute_l10n_ae_normative_identity",
        compute_sudo=False,
    )

    @api.depends("l10n_ae_transmission_id.payload")
    @api.depends_context("uid")
    def _compute_l10n_ae_normative_identity(self):
        for move in self:
            issue = move.l10n_ae_transmission_id
            if issue:
                issue.check_access("read")
            identity = (
                (json.loads(issue.payload).get("normative") or {}) if issue else {}
            )
            move.l10n_ae_normative_ruleset = identity.get("ruleset") or False
            move.l10n_ae_normative_manifest_sha256 = (
                identity.get("manifest_sha256") or False
            )

    def _l10n_ae_payment_means(self):
        """Describe the observed native instruction, independently of settlement."""
        self.ensure_one()
        method = self.preferred_payment_method_line_id
        journal = method.journal_id
        configured_code = method.l10n_ae_edi_payment_means_code
        if configured_code:
            code, name = configured_code, dict(UAE_PAYMENT_MEANS)[configured_code]
        elif not method:
            code, name = "1", "Instrument not defined"
        elif journal.type == "cash":
            code, name = "10", "In cash"
        elif journal.type == "bank" and method.code == "manual":
            code, name = "30", "Credit transfer"
        else:
            return None
        account = self.partner_bank_id if code == "30" else self.env["res.partner.bank"]
        identifier = (
            sanitize_account_number(account.account_number) if account else None
        )
        if code == "30" and not identifier:
            raise UserError(
                self.env._(
                    "IBR-192-AE: credit transfer requires the recipient bank account. "
                    "Review the native invoice recipient bank before preparation."
                )
            )
        instruction = {
            "code": code,
            "name": name,
            "payment_reference": self.payment_reference or self.name,
            "method_line_id": method.id or None,
            "method_code": method.code or None,
            "journal_id": journal.id or None,
            "journal_type": journal.type or None,
            "account_id": account.id or None,
            "account_identifier": identifier,
        }
        if configured_code:
            instruction["configured_code"] = configured_code
        return instruction

    def _l10n_ae_snapshot(self):
        snapshot = super()._l10n_ae_snapshot()
        if instruction := self._l10n_ae_payment_means():
            snapshot["payment_means"] = instruction
        product_lines = self.invoice_line_ids.filtered(
            lambda line: not line.display_type or line.display_type == "product"
        )
        for line, snapshot_line in zip(product_lines, snapshot["lines"], strict=True):
            additional_taxes = line.tax_ids.flatten_taxes_hierarchy().filtered(
                lambda tax: tax.ubl_cii_tax_category_code == "N"
            )
            if additional_taxes:
                snapshot_line["additional_vat_rates"] = [
                    {
                        "category": "N",
                        "nominal_rate": str(tax.l10n_ae_edi_additional_vat_rate),
                    }
                    for tax in additional_taxes
                ]
        ruleset = os.environ.get("KMEE_UAE_NORMATIVE_RULESET", DEFAULT_RULESET)
        try:
            snapshot["normative"] = describe_ruleset(ruleset)
        except ValidationUnavailable as error:
            raise UserError(
                self.env._("UAE normative configuration is unavailable: %s", str(error))
            ) from error
        return snapshot

    @api.depends("l10n_ae_received_inbox_ids.delivery_snapshot")
    def _compute_l10n_ae_received_delivery_summary(self):
        for move in self:
            original = move.l10n_ae_received_inbox_ids.sorted("id").filtered(
                "delivery_snapshot"
            )[:1]
            move.l10n_ae_received_delivery_summary = original.delivery_summary or False

    @api.depends("l10n_ae_received_inbox_ids.credit_reference_snapshot")
    def _compute_l10n_ae_received_credit_reference_summary(self):
        for move in self:
            original = move.l10n_ae_received_inbox_ids.sorted("id").filtered(
                "credit_reference_snapshot"
            )[:1]
            move.l10n_ae_received_credit_reference_summary = (
                original.credit_reference_summary or False
            )

    def action_l10n_ae_validate(self):
        result = super().action_l10n_ae_validate()
        formatter = self.env["account.edi.ubl.kmee_ae"]
        for move in self:
            supplier, customer = (
                (move.partner_id.commercial_partner_id, move.company_id.partner_id)
                if move.is_self_billing
                else (move.company_id.partner_id, move.partner_id.commercial_partner_id)
            )
            for tax in move.invoice_line_ids.tax_ids.flatten_taxes_hierarchy():
                if formatter._get_tax_category_code(
                    customer, supplier, tax
                ) == "E" and tax.ubl_cii_tax_exemption_reason_code not in dict(
                    UAE_EXEMPTION_REASONS
                ):
                    raise UserError(
                        self.env._(
                            "IBR-167-AE: exempt tax '%s' requires an official UAE "
                            "exemption code DL8.46.1, DL8.46.2, DL8.46.3 or DL8.46.4. "
                            "Review the tax configuration before preparing "
                            "the invoice.",
                            tax.display_name,
                        )
                    )
        for move in self.filtered(
            lambda record: record.l10n_ae_document_category == "commercial"
        ):
            taxes = move.invoice_line_ids.tax_ids.flatten_taxes_hierarchy()
            if any(
                formatter._get_tax_category_code(
                    move.partner_id.commercial_partner_id,
                    move.company_id.partner_id,
                    tax,
                )
                not in {"E", "O", "Z"}
                for tax in taxes
            ):
                raise UserError(
                    self.env._(
                        "IBR-122-AE: commercial invoices and credits require tax "
                        "categories E, O or Z. Review the line taxes "
                        "before preparation."
                    )
                )
        for move in self.filtered("is_self_billing"):
            supplier = move.partner_id.commercial_partner_id
            buyer = move.company_id.partner_id
            if (
                supplier.routing_scheme != "0235"
                or not re.fullmatch(r"[0-9]{10}", supplier.routing_endpoint or "")
                or supplier.routing_endpoint
                in {"9900000097", "9900000098", "9900000099"}
                or buyer.routing_scheme != "0235"
                or buyer.routing_endpoint != move.company_id.l10n_ae_tin
            ):
                raise UserError(
                    self.env._(
                        "Review self-billing onboarding: both parties need confirmed "
                        "UAE participant endpoints; placeholder destinations "
                        "are not eligible."
                    )
                )
        for move in self:
            for party in (
                move.company_id.partner_id | move.partner_id.commercial_partner_id
            ):
                party._l10n_ae_country_subentity()
        for move in self.filtered(
            lambda record: (
                record.l10n_ae_document_category == "tax" and not record.is_self_billing
            )
        ):
            # Check the same generated categories used by Preview/Prepare/Send;
            # the public core decides which taxes become actual item nodes.
            formatter._export_invoice(move)
        return result

    def preview_l10n_ae_pint(self):
        """Read frozen XML when prepared; otherwise preview without submitting."""
        self.ensure_one()
        self.check_access("read")
        transmission = self.l10n_ae_transmission_id
        if transmission:
            transmission.check_access("read")
            if (
                transmission.move_id != self
                or transmission.company_id != self.company_id
            ):
                raise UserError(
                    self.env._("The frozen UAE XML belongs to another invoice.")
                )
            if not transmission.xml_content:
                raise UserError(
                    self.env._("No frozen XML is available for this transmission.")
                )
            content = transmission.xml_content.encode("utf-8")
            if hashlib.sha256(content).hexdigest() != transmission.xml_hash:
                raise UserError(
                    self.env._("The frozen UAE XML failed its integrity check.")
                )
            return transmission.xml_content
        self.action_l10n_ae_validate()
        content, errors = self.env["account.edi.ubl.kmee_ae"]._export_invoice(self)
        if errors:
            raise UserError("\n".join(sorted(errors)))
        return content.decode("utf-8")

    def _get_invoice_legal_documents(self, filetype, allow_fallback=False):
        self.ensure_one()
        if filetype == "ubl" and self.l10n_ae_transmission_id:
            content = self.preview_l10n_ae_pint().encode("utf-8")
            return [
                {
                    "filename": self._get_invoice_report_filename(extension="xml"),
                    "filetype": "application/xml",
                    "content": content,
                }
            ]
        return super()._get_invoice_legal_documents(
            filetype, allow_fallback=allow_fallback
        )

    def _get_invoice_legal_documents_all(self, allow_fallback=False):
        self.ensure_one()
        if self.l10n_ae_transmission_id.conventional_html_content:
            return self._get_invoice_legal_documents(
                "pdf", allow_fallback=allow_fallback
            ) + self._get_invoice_legal_documents("ubl", allow_fallback=allow_fallback)
        return super()._get_invoice_legal_documents_all(allow_fallback=allow_fallback)

    def action_l10n_ae_download_xml(self):
        """Download the checked preserved issue through the native XML route."""
        self.ensure_one()
        self.check_access("read")
        if not self.l10n_ae_transmission_id:
            raise UserError(
                self.env._("Prepare the UAE invoice before downloading its XML.")
            )
        self.preview_l10n_ae_pint()
        return {
            "type": "ir.actions.act_url",
            "target": "download",
            "url": f"/account/download_invoice_documents/{self.id}/ubl",
        }

    def _l10n_ae_prepare_xml(self):
        return self.preview_l10n_ae_pint()

    def validate_l10n_ae_pint(self):
        """Validate generated XML offline; return all normative failures."""
        self.ensure_one()
        self.check_access("read")
        directory = os.environ.get("KMEE_UAE_NORMATIVE_DIR")
        if not directory:
            raise UserError(
                self.env._("Configure the offline UAE normative resource directory.")
            )
        content = self.preview_l10n_ae_pint().encode("utf-8")
        try:
            ruleset = os.environ.get("KMEE_UAE_NORMATIVE_RULESET", DEFAULT_RULESET)
            identity = describe_ruleset(ruleset)
            if self.l10n_ae_transmission_id:
                pinned = json.loads(self.l10n_ae_transmission_id.payload).get(
                    "normative"
                )
                if pinned != identity:
                    raise UserError(
                        self.env._(
                            "The pinned PINT package differs from the configured "
                            "package. Restore the originally pinned configuration "
                            "before validating this prepared document."
                        )
                    )
            return OfflinePintValidator(directory, ruleset=ruleset).validate(content)
        except ValidationUnavailable as error:
            raise UserError(
                self.env._("UAE formal validation is unavailable: %s", str(error))
            ) from error

    def _l10n_ae_check_pint_report(self, report):
        if not report["valid"]:
            diagnostics = [
                f"UBL {issue['code']} (line {issue['line']})"
                for issue in report["xsd_errors"]
            ]
            for layer in report["layers"]:
                diagnostics.extend(
                    f"{failure['code']}: {failure['message']}"
                    for failure in layer["failures"]
                )
            for check in report.get("supplemental_checks", []):
                diagnostics.extend(
                    f"KMEE supplemental {failure['code']}: {failure['message']}"
                    for failure in check["failures"]
                )
            raise UserError(
                self.env._("PINT-AE validation failed:\n%s", "\n".join(diagnostics))
            )

    def action_l10n_ae_validate_pint(self):
        report = self.validate_l10n_ae_pint()
        self._l10n_ae_check_pint_report(report)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("PINT-AE validation complete"),
                "message": self.env._("The document passed UBL and PINT-AE rules."),
                "type": "success",
                "sticky": False,
            },
        }
