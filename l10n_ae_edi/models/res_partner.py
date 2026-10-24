# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import LazyTranslate

_lt = LazyTranslate(__name__)


class ResPartner(models.Model):
    _inherit = "res.partner"

    l10n_ae_registration_type = fields.Selection(
        [
            ("TL", "Trade License"),
            ("CL", "Commercial License"),
            ("EID", "Emirates ID"),
            ("PAS", "Passport"),
            ("CD", "Cabinet Decision"),
        ],
        string="UAE Legal Registration Type",
    )
    l10n_ae_registration_authority = fields.Char(
        string="UAE Registration Authority",
        help=(
            "Issuing authority for a trade license, or ISO country code for a passport."
        ),
    )

    @api.model
    def _get_all_additional_identifiers_metadata(self):
        # Core resolves these cached labels in the requesting user's language.
        # pylint: disable=prefer-env-translation
        result = super()._get_all_additional_identifiers_metadata()
        for code, label in [
            ("TL", _lt("UAE Trade License")),
            ("CL", _lt("UAE Commercial License")),
            ("EID", _lt("UAE Emirates ID")),
            ("PAS", _lt("UAE Passport")),
            ("CD", _lt("UAE Cabinet Decision")),
        ]:
            result[f"AE_{code}"] = {
                "label": label,
                "countries": ["AE"],
                "category": "CN" if code in ("EID", "PAS") else "EN",
                "sequence": 10,
            }
        return result

    def _l10n_ae_country_subentity(self):
        self.ensure_one()
        if self.country_code != "AE":
            return self.state_id.name or None
        # Native Odoo state codes remain shared with accounting/localizations.
        # PINT-AE IBR-128 uses these identifiers in CountrySubentity itself.
        code = {
            "AZ": "AUH",
            "AJ": "AJM",
            "DU": "DXB",
            "FU": "FUJ",
            "RK": "RAK",
            "SH": "SHJ",
            "UQ": "UAQ",
        }.get(self.state_id.code)
        if not code or self.state_id.country_id != self.country_id:
            raise UserError(
                self.env._(
                    "IBR-128-AE: Review the UAE emirate on the address of %s.",
                    self.display_name,
                )
            )
        return code

    def _l10n_ae_legal_registration_number(self):
        self.ensure_one()
        return (self.additional_identifiers or {}).get(
            f"AE_{self.l10n_ae_registration_type}"
        )
