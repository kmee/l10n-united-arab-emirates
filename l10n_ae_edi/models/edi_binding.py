# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import os

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..tools.controlled_executor import ControlledExecutor


class UaeEdiBinding(models.Model):
    _name = "l10n_ae.edi.binding"
    _description = "UAE E-Invoicing Provider Binding"
    _check_company_auto = True

    name = fields.Char(required=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company, index=True
    )
    active = fields.Boolean(default=True)
    controlled_dispatch_enabled = fields.Boolean(
        string="Automatic Controlled Dispatch",
        default=False,
        copy=False,
        help="Dispatch committed sandbox invoices through the native scheduled action. "
        "Requires a loopback endpoint and synthetic token in protected "
        "process configuration.",
    )
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
    fiscal_uuid_policy = fields.Selection(
        [
            ("supplied", "Externally Supplied UUID"),
            ("controlled_local", "Local UUID for Controlled Simulation"),
        ],
        string="Fiscal UUID Policy",
        required=True,
        default="supplied",
        help="Local UUID assignment is available only in the controlled sandbox. "
        "Production UUID allocation requires a verified ASP contract.",
    )

    @api.constrains("fiscal_uuid_policy", "environment")
    def _check_fiscal_uuid_policy(self):
        if any(
            binding.fiscal_uuid_policy == "controlled_local"
            and binding.environment != "sandbox"
            for binding in self
        ):
            raise ValidationError(
                self.env._("Local UUID assignment requires the controlled sandbox.")
            )

    @api.constrains("controlled_dispatch_enabled", "environment")
    def _check_controlled_dispatch_environment(self):
        if any(
            binding.controlled_dispatch_enabled and binding.environment != "sandbox"
            for binding in self
        ):
            raise ValidationError(
                self.env._("Automatic controlled dispatch requires the sandbox.")
            )

    @api.model
    def _cron_dispatch_controlled(self):
        if not self.env.context.get("cron_id"):
            raise UserError(self.env._("Use the native UAE scheduled action."))
        if not self.env.user.has_group("account.group_account_manager"):
            raise AccessError(
                self.env._("Only accounting managers can execute UAE sending.")
            )
        bindings = self.with_context(active_test=False).search(
            [
                ("controlled_dispatch_enabled", "=", True),
                ("environment", "=", "sandbox"),
            ],
            order="id",
        )
        routes = []
        for binding in bindings:
            prefix = f"KMEE_UAE_CONTROLLED_{binding.id}"
            endpoint = os.environ.get(prefix + "_ENDPOINT")
            token = os.environ.get(prefix + "_TOKEN")
            if not endpoint or not token:
                raise UserError(
                    self.env._(
                        "Protected controlled dispatch configuration is unavailable "
                        "for binding %(binding)s.",
                        binding=binding.name,
                    )
                )
            routes.append((binding.id, binding.company_id.id, endpoint, token))
        registry, uid = self.env.registry, self.env.uid
        progress = self.env["ir.cron"]
        if not progress._commit_progress(remaining=len(routes)):
            return
        for binding_id, company_id, endpoint, token in routes:
            ControlledExecutor(registry, uid, company_id, endpoint, token).run_pending(
                binding_id
            )
            if not progress._commit_progress(1):
                return

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
