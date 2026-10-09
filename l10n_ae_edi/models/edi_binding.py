# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
import os

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..tools.controlled_client import ControlledAspClient, ControlledAspError
from ..tools.controlled_executor import ControlledExecutor

SCHEDULED_DIAGNOSTIC_FIELDS = {
    "controlled_dispatch_error",
    "controlled_dispatch_error_at",
}
CAPABILITY_REVIEW_FIELDS = {
    "capability_reviewed_by",
    "capability_reviewed_at",
    "capability_review_hash",
    "capability_review_state",
}
CAPABILITY_DECLARATION_FIELDS = {
    "company_id",
    "provider",
    "environment",
    "fiscal_uuid_policy",
    "capability_profile",
    "capability_ids",
    "capability_review_note",
}
OUTBOUND_CAPABILITIES = {
    "billing_invoice",
    "billing_credit",
    "selfbilling_invoice",
    "selfbilling_credit",
}


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
    controlled_dispatch_error = fields.Selection(
        [("configuration", "Controlled Sending Configuration Required")],
        string="Scheduled Sending Review",
        readonly=True,
        copy=False,
    )
    controlled_dispatch_error_at = fields.Datetime(
        string="Scheduled Sending Review Time",
        readonly=True,
        copy=False,
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
    capability_profile = fields.Selection(
        [("kmee-controlled-1", "KMEE Controlled Contract v1")],
        string="Capability Contract Profile",
        copy=False,
        help="A local simulation contract, not verified ClearTax or Suntech support.",
    )
    capability_ids = fields.Many2many(
        "l10n_ae.edi.capability", string="Declared Contract Capabilities", copy=False
    )
    capability_review_note = fields.Text(
        string="Contract Capability Review Note", copy=False
    )
    capability_reviewed_by = fields.Many2one("res.users", readonly=True, copy=False)
    capability_reviewed_at = fields.Datetime(readonly=True, copy=False)
    capability_review_hash = fields.Char(readonly=True, copy=False)
    capability_review_state = fields.Selection(
        [
            ("review", "Requires Contract Review"),
            ("ready", "Reviewed Simulation Contract"),
        ],
        compute="_compute_capability_review_state",
    )

    def _capability_facts(self):
        self.ensure_one()
        return {
            "company_id": self.company_id.id,
            "provider": self.provider,
            "environment": self.environment,
            "uuid_policy": self.fiscal_uuid_policy,
            "profile": self.capability_profile or None,
            "capabilities": sorted(self.capability_ids.mapped("code")),
            "review_note": self.capability_review_note or None,
        }

    def _capability_fingerprint(self):
        return hashlib.sha256(
            json.dumps(
                self._capability_facts(), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    @api.depends(
        "company_id",
        "provider",
        "environment",
        "fiscal_uuid_policy",
        "capability_profile",
        "capability_ids.code",
        "capability_review_note",
        "capability_review_hash",
    )
    def _compute_capability_review_state(self):
        for binding in self:
            binding.capability_review_state = (
                "ready"
                if binding.capability_review_hash
                and binding.capability_review_hash == binding._capability_fingerprint()
                else "review"
            )

    def _check_capability_reviewer(self):
        principal = self.env.user.with_user(self.env.uid)
        if (
            not principal.active
            or principal.share
            or not principal.has_group("account.group_account_manager")
        ):
            raise AccessError(
                self.env._("An active accounting capability reviewer is required.")
            )
        self.with_user(self.env.uid).check_access("write")
        if any(
            binding.company_id not in principal.company_ids
            or binding.company_id not in self.env.companies
            for binding in self
        ):
            raise AccessError(
                self.env._("Review capabilities only for an authorized active company.")
            )

    def _lock_capability_contract(self):
        if self:
            self.env.cr.execute(
                "SELECT id FROM l10n_ae_edi_binding "
                "WHERE id IN %s ORDER BY id FOR UPDATE",
                (tuple(self.ids),),
            )
            self.invalidate_recordset()

    def action_review_capabilities(self):
        self.ensure_one()
        self._check_capability_reviewer()
        self._lock_capability_contract()
        self._check_capability_reviewer()
        if (
            self.environment != "sandbox"
            or self.capability_profile != "kmee-controlled-1"
        ):
            raise ValidationError(
                self.env._("Select the controlled sandbox capability contract.")
            )
        if not self.capability_ids or not (self.capability_review_note or "").strip():
            raise ValidationError(
                self.env._("Declare capabilities and record the contract review note.")
            )
        return super().write(
            {
                "capability_reviewed_by": self.env.uid,
                "capability_reviewed_at": fields.Datetime.now(),
                "capability_review_hash": self._capability_fingerprint(),
            }
        )

    def _require_sending_capabilities(self):
        self.ensure_one()
        self.with_user(self.env.uid).check_access("read")
        if self.capability_review_state != "ready":
            raise ValidationError(
                self.env._(
                    "Review the binding's contract capabilities "
                    "before enabling sending."
                )
            )
        codes = set(self.capability_ids.mapped("code"))
        missing = {"submit", "lookup", "report"} - codes
        if missing:
            raise ValidationError(
                self.env._(
                    "Missing required sending capabilities: %s.",
                    ", ".join(sorted(missing)),
                )
            )
        if not codes.intersection(OUTBOUND_CAPABILITIES):
            raise ValidationError(
                self.env._(
                    "Declare at least one outbound document capability "
                    "before enabling sending."
                )
            )

    def _require_receiving_capabilities(self):
        self.ensure_one()
        self.with_user(self.env.uid).check_access("read")
        self._lock_capability_contract()
        self.with_user(self.env.uid).check_access("read")
        if self.capability_review_state != "ready":
            raise ValidationError(
                self.env._(
                    "Review the binding's contract capabilities before receiving."
                )
            )
        if "receive" not in self.capability_ids.mapped("code"):
            raise ValidationError(
                self.env._("The reviewed binding contract does not support receive.")
            )

    def _require_document_capability(self, process, credit):
        self._require_sending_capabilities()
        self._lock_capability_contract()
        self._require_sending_capabilities()
        document = "credit" if credit else "invoice"
        code = f"{process}_{document}"
        if code not in self.capability_ids.mapped("code"):
            raise ValidationError(
                self.env._("The reviewed binding contract does not support %s.", code)
            )

    @api.constrains(
        "controlled_dispatch_enabled",
        "capability_review_hash",
        "capability_ids",
        "capability_profile",
    )
    def _check_sending_capabilities(self):
        for binding in self.filtered("controlled_dispatch_enabled"):
            binding._check_capability_reviewer()
            binding._require_sending_capabilities()

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
    def _check_transport_allowed(self):
        # Callers first validate the actual account and company. Read only the
        # native database safety flag, without granting parameter ACLs to workers.
        if self.env["ir.config_parameter"].sudo().get_bool("database.is_neutralized"):
            raise AccessError(
                self.env._("UAE integration is blocked in a neutralized database.")
            )

    def _controlled_environment_prefix(self):
        """Pin process credentials to the exact database name and native UUID."""
        self.ensure_one()
        database_uuid = self.env["ir.config_parameter"].sudo().get_str("database.uuid")
        if not database_uuid:
            return None
        identity = json.dumps(
            [self.env.cr.dbname, database_uuid],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        scope = hashlib.sha256(identity).hexdigest().upper()
        return f"KMEE_UAE_CONTROLLED_DB_{scope}_{self.id}"

    @api.model
    def _cron_dispatch_controlled(self):
        if not self.env.context.get("cron_id"):
            raise UserError(self.env._("Use the native UAE scheduled action."))
        principal = self.env.user.exists()
        if (
            not principal
            or not principal.active
            or not (
                principal.has_group("l10n_ae_edi.group_controlled_sender")
                or principal.has_group("account.group_account_manager")
            )
        ):
            raise AccessError(
                self.env._("An active authorized UAE sending account is required.")
            )
        self._check_transport_allowed()
        bindings = self.with_context(active_test=False).search(
            [
                ("controlled_dispatch_enabled", "=", True),
                ("environment", "=", "sandbox"),
            ],
            order="id",
        )
        routes = []
        for binding in bindings:
            prefix = binding._controlled_environment_prefix()
            endpoint = os.environ.get(prefix + "_ENDPOINT") if prefix else None
            token = os.environ.get(prefix + "_TOKEN") if prefix else None
            routes.append((binding.id, binding.company_id.id, endpoint, token))
        registry, uid = self.env.registry, self.env.uid
        progress = self.env["ir.cron"]
        if not progress._commit_progress(remaining=len(routes)):
            return
        for binding_id, company_id, endpoint, token in routes:
            binding = self.browse(binding_id)
            configured = bool(endpoint and token)
            if configured:
                try:
                    ControlledAspClient(endpoint, token, binding.provider)
                except (ControlledAspError, ValueError):
                    configured = False
            if not configured:
                binding._record_controlled_dispatch_configuration(True)
            else:
                binding._record_controlled_dispatch_configuration(False)
                ControlledExecutor(
                    registry, uid, company_id, endpoint, token
                ).run_pending(binding_id)
            if not progress._commit_progress(1):
                return

    def _record_controlled_dispatch_configuration(self, failed):
        self.ensure_one()
        if not failed and not self.controlled_dispatch_error:
            return
        self.check_access("read")
        return super(UaeEdiBinding, self.sudo()).write(
            {
                "controlled_dispatch_error": "configuration" if failed else False,
                "controlled_dispatch_error_at": fields.Datetime.now()
                if failed
                else False,
            }
        )

    _one_production_asp = models.UniqueIndex(
        "(company_id) WHERE active IS TRUE AND environment = 'production'",
        "A legal entity can have only one active production ASP.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        review_defaults = self.default_get(sorted(CAPABILITY_REVIEW_FIELDS))
        if any(
            CAPABILITY_REVIEW_FIELDS.intersection(vals) for vals in vals_list
        ) or any(review_defaults.values()):
            raise UserError(self.env._("Use the contract capability review action."))
        checkpoint_defaults = self.default_get(sorted(SCHEDULED_DIAGNOSTIC_FIELDS))
        if any(
            any(
                vals.get(field, checkpoint_defaults.get(field))
                for field in SCHEDULED_DIAGNOSTIC_FIELDS
            )
            for vals in vals_list
        ):
            raise UserError(
                self.env._(
                    "Scheduled sending diagnostics are managed by the native scheduler."
                )
            )
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

    def write(self, vals):
        if CAPABILITY_REVIEW_FIELDS.intersection(vals):
            raise UserError(self.env._("Use the contract capability review action."))
        if vals.get("controlled_dispatch_enabled"):
            self.with_user(self.env.uid).check_access("write")
            if any(
                vals.get("environment", binding.environment) != "sandbox"
                for binding in self
            ):
                raise ValidationError(
                    self.env._("Automatic controlled dispatch requires the sandbox.")
                )
        if CAPABILITY_DECLARATION_FIELDS.intersection(vals) and vals.get(
            "controlled_dispatch_enabled"
        ):
            raise ValidationError(
                self.env._(
                    "Review the changed contract capabilities separately "
                    "before enabling sending."
                )
            )
        if CAPABILITY_DECLARATION_FIELDS.intersection(vals) or vals.get(
            "controlled_dispatch_enabled"
        ):
            self.with_user(self.env.uid).check_access("write")
            self._lock_capability_contract()
            self.with_user(self.env.uid).check_access("write")
        if "company_id" in vals:
            principal = self.env.user.with_user(self.env.uid)
            if (
                vals["company_id"] not in principal.company_ids.ids
                or vals["company_id"] not in self.env.companies.ids
            ):
                raise AccessError(
                    self.env._("Assign bindings only to an authorized active company.")
                )
        if CAPABILITY_DECLARATION_FIELDS.intersection(vals):
            vals = {
                **vals,
                "capability_reviewed_by": False,
                "capability_reviewed_at": False,
                "capability_review_hash": False,
                "controlled_dispatch_enabled": False,
            }
        if SCHEDULED_DIAGNOSTIC_FIELDS.intersection(vals):
            raise UserError(
                self.env._(
                    "Scheduled sending diagnostics are managed by the native scheduler."
                )
            )
        return super().write(vals)
