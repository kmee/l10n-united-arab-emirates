# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import UserError


class ProductTemplate(models.Model):
    _inherit = "product.template"

    l10n_ae_is_good_and_service = fields.Boolean(
        string="UAE Goods and Services",
        compute="_compute_l10n_ae_is_good_and_service",
        inverse="_inverse_l10n_ae_is_good_and_service",
        copy=False,
        help="Reviewed mixed item; requires separate HS and SAC identifiers.",
    )
    l10n_ae_classification_code = fields.Char(
        string="UAE HS Code",
        compute="_compute_l10n_ae_classification_code",
        inverse="_inverse_l10n_ae_classification_code",
        copy=False,
        help="Reviewed HS for a unique variant; configure other variants separately.",
    )
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

    @api.depends("product_variant_ids.l10n_ae_is_good_and_service")
    def _compute_l10n_ae_is_good_and_service(self):
        self._compute_template_field_from_variant_field("l10n_ae_is_good_and_service")

    def _inverse_l10n_ae_is_good_and_service(self):
        self._set_product_variant_field("l10n_ae_is_good_and_service")

    @api.depends("product_variant_ids.l10n_ae_classification_code")
    def _compute_l10n_ae_classification_code(self):
        self._compute_template_field_from_variant_field("l10n_ae_classification_code")

    def _inverse_l10n_ae_classification_code(self):
        self._set_product_variant_field("l10n_ae_classification_code")

    def _get_related_fields_variant_template(self):
        # Native create reapplies these proxy values after creating the variant.
        # Without this hook an inverse runs too early and loses new Form input.
        return super()._get_related_fields_variant_template() + [
            "l10n_ae_is_good_and_service",
            "l10n_ae_classification_code",
            "l10n_ae_service_accounting_code",
            "l10n_ae_service_accounting_version",
        ]

    def write(self, vals):
        if {
            "l10n_ae_is_good_and_service",
            "l10n_ae_classification_code",
            "l10n_ae_service_accounting_code",
            "l10n_ae_service_accounting_version",
        }.intersection(vals):
            for template in self:
                variants = (
                    template.product_variant_ids
                    or template.with_context(active_test=False).product_variant_ids
                )
                if len(variants) > 1:
                    # The native proxy inverse otherwise silently ignores this
                    # write. Make the required variant-level review explicit.
                    raise UserError(
                        self.env._(
                            "Configure UAE classification on each individual variant."
                        )
                    )
        return super().write(vals)
