# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    l10n_ae_service_accounting_code = fields.Char(
        string="UAE Service Accounting Code",
        compute="_compute_l10n_ae_service_accounting_code",
        inverse="_inverse_l10n_ae_service_accounting_code",
        copy=False,
        help="Reviewed SAC for a unique variant; configure other variants separately.",
    )
    l10n_ae_service_accounting_version = fields.Char(
        string="UAE SAC Scheme Version",
        compute="_compute_l10n_ae_service_accounting_version",
        inverse="_inverse_l10n_ae_service_accounting_version",
        copy=False,
        help="Reviewed SAC scheme version; no version is supplied by default.",
    )

    @api.depends("product_variant_ids.l10n_ae_service_accounting_code")
    def _compute_l10n_ae_service_accounting_code(self):
        self._compute_template_field_from_variant_field(
            "l10n_ae_service_accounting_code"
        )

    def _inverse_l10n_ae_service_accounting_code(self):
        self._set_product_variant_field("l10n_ae_service_accounting_code")

    @api.depends("product_variant_ids.l10n_ae_service_accounting_version")
    def _compute_l10n_ae_service_accounting_version(self):
        self._compute_template_field_from_variant_field(
            "l10n_ae_service_accounting_version"
        )

    def _inverse_l10n_ae_service_accounting_version(self):
        self._set_product_variant_field("l10n_ae_service_accounting_version")
