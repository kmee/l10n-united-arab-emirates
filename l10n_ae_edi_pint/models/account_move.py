# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import os
import re

from odoo import models
from odoo.exceptions import UserError

from ..tools.validation import OfflinePintValidator, ValidationUnavailable


class AccountMove(models.Model):
    _inherit = "account.move"

    def action_l10n_ae_validate(self):
        result = super().action_l10n_ae_validate()
        formatter = self.env["account.edi.ubl.kmee_ae"]
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
                        "categories E, O or Z. Review the line taxes before posting."
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
        return result

    def preview_l10n_ae_pint(self):
        """Read frozen XML when prepared; otherwise preview without submitting."""
        self.ensure_one()
        self.check_access("read")
        transmission = self.l10n_ae_transmission_id
        if transmission:
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
            return OfflinePintValidator(directory).validate(content)
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
