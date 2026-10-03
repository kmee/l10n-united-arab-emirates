# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import os

from odoo import models
from odoo.exceptions import UserError

from ..tools.validation import OfflinePintValidator, ValidationUnavailable


class AccountMove(models.Model):
    _inherit = "account.move"

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

    def action_l10n_ae_validate_pint(self):
        report = self.validate_l10n_ae_pint()
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
