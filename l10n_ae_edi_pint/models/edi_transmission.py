# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json

from odoo import fields, models
from odoo.exceptions import UserError

from odoo.addons.l10n_ae_edi.tools.errors import UaeDocumentValidationError


class UaeEdiTransmission(models.Model):
    _inherit = "l10n_ae.edi.transmission"

    def _validate_controlled_document(self):
        self.ensure_one()
        if self.submission_state in {"sending", "unknown"}:
            # Lookup resolves an existing remote obligation, never a fresh issue.
            # Check preserved bytes without reinterpreting them with current rules.
            self.move_id.preview_l10n_ae_pint()
            try:
                proof = json.loads(self.validation_receipt or "null")
                if not isinstance(proof, dict):
                    raise TypeError("Missing original validation proof")
                checked_at = proof["checked_at"]
                if not isinstance(checked_at, str):
                    raise TypeError("Invalid original validation time")
                if not fields.Datetime.to_datetime(checked_at):
                    raise ValueError("Missing original validation time")
                report = proof["report"]
                identity = {
                    "ruleset": report["ruleset"],
                    "manifest_sha256": report["resources"]["manifest_sha256"],
                }
                pin = json.loads(self.payload).get("normative")
                if (
                    proof["xml_sha256"] != self.xml_hash
                    or report["valid"] is not True
                    or report["xsd_valid"] is not True
                    or report["xsd_errors"] != []
                    or not isinstance(report["layers"], list)
                    or [layer["name"] for layer in report["layers"]] != ["shared", "ae"]
                    or any(layer["failures"] != [] for layer in report["layers"])
                    or (pin is not None and pin != identity)
                ):
                    raise ValueError("Inconsistent original validation proof")
            except (KeyError, TypeError, ValueError) as error:
                raise UaeDocumentValidationError(
                    self.env._(
                        "The original UAE validation evidence is unavailable or "
                        "inconsistent. Review the preserved issue before lookup."
                    )
                ) from error
            return report
        # Resource/configuration and access failures remain outside this mapping.
        report = self.move_id.validate_l10n_ae_pint()
        try:
            self.move_id._l10n_ae_check_pint_report(report)
        except UserError as error:
            if report["valid"] or error.__class__ is not UserError:
                raise
            raise UaeDocumentValidationError(str(error), report=report) from error
        return report
