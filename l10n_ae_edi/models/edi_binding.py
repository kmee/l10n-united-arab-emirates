# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import ValidationError


class UaeEdiBinding(models.Model):
    _name = "l10n_ae.edi.binding"
    _description = "UAE E-Invoicing Provider Binding"
    _check_company_auto = True

    name = fields.Char(required=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company, index=True
    )
    active = fields.Boolean(default=True)
    environment = fields.Selection(
        [("sandbox", "Sandbox"), ("production", "Production")],
        required=True,
        default="sandbox",
        copy=False,
    )
    provider = fields.Selection(
        [("controlled", "KMEE Controlled Test Service")],
        required=True,
        default="controlled",
    )

    _one_production_asp = models.UniqueIndex(
        "(company_id) WHERE active IS TRUE AND environment = 'production'",
        "A legal entity can have only one active production ASP.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get(["company_id", "active", "environment"])
        production_companies = set()
        for vals in vals_list:
            values = {**defaults, **vals}
            if values.get("active") and values.get("environment") == "production":
                company_id = values["company_id"]
                if company_id in production_companies or self.search_count(
                    [
                        ("company_id", "=", company_id),
                        ("active", "=", True),
                        ("environment", "=", "production"),
                    ],
                    limit=1,
                ):
                    raise ValidationError(
                        self.env._(
                            "A legal entity can have only one active production ASP."
                        )
                    )
                production_companies.add(company_id)
        return super().create(vals_list)
