# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    available_additional_identifiers_metadata = fields.Json(
        related=False,
        compute="_compute_l10n_ae_available_identifiers_metadata",
        compute_sudo=True,
        readonly=True,
    )
    l10n_ae_fiscal_country_code = fields.Char(
        related="account_fiscal_country_id.code", string="Fiscal Country Code"
    )
    l10n_ae_tin = fields.Char(
        string="UAE Taxpayer Identification Number",
        help=(
            "Entity-specific TIN confirmed during onboarding; "
            "distinct from the VAT TRN."
        ),
        copy=False,
    )
    l10n_ae_tin_confirmed = fields.Boolean(
        string="TIN Confirmed During Onboarding",
        copy=False,
    )
    l10n_ae_edi_auto_prepare = fields.Boolean(
        string="Queue UAE E-Invoices on Posting",
        help=(
            "Create a frozen transmission with the posted invoice. "
            "Sending runs after commit."
        ),
        copy=False,
    )
    l10n_ae_edi_binding_id = fields.Many2one(
        "l10n_ae.edi.binding",
        string="Default UAE ASP Binding",
        check_company=True,
        copy=False,
    )
    l10n_ae_registration_type = fields.Selection(
        related="partner_id.l10n_ae_registration_type", readonly=False
    )
    l10n_ae_registration_authority = fields.Char(
        related="partner_id.l10n_ae_registration_authority", readonly=False
    )

    @api.depends_context("company")
    @api.depends(
        "account_fiscal_country_id",
        "partner_id.available_additional_identifiers_metadata",
    )
    def _compute_l10n_ae_available_identifiers_metadata(self):
        for company in self:
            partner = company.partner_id
            metadata = dict(partner.available_additional_identifiers_metadata or {})
            if company.account_fiscal_country_id.code == "AE":
                # Company jurisdiction can differ from the physical address. Keep
                # native address choices and offer its UAE legal registration too.
                all_metadata = partner._get_all_additional_identifiers_metadata()
                for key in ("AE_TL", "AE_CL", "AE_EID", "AE_PAS", "AE_CD"):
                    metadata[key] = (
                        partner._lazy_translate_additional_identifiers_metadata(
                            all_metadata[key]
                        )
                    )
            company.available_additional_identifiers_metadata = metadata

    @api.constrains("l10n_ae_edi_binding_id")
    def _check_l10n_ae_default_binding_company(self):
        self._check_company(["l10n_ae_edi_binding_id"])

    def write(self, vals):
        if "l10n_ae_tin" in vals and "l10n_ae_tin_confirmed" not in vals:
            changed = self.filtered(
                lambda company: company.l10n_ae_tin != vals["l10n_ae_tin"]
            )
            if changed:
                super(ResCompany, changed).write({"l10n_ae_tin_confirmed": False})
        return super().write(vals)
