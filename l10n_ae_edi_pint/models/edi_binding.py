# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import logging
import os

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.l10n_ae_edi.tools.controlled_client import (
    ControlledAspClient,
    ControlledAspError,
)

from ..tools.controlled_inbox import ControlledInboxWorker

_logger = logging.getLogger(__name__)

INBOUND_CHECKPOINT_FIELDS = {
    "controlled_inbound_cursor",
    "controlled_inbound_route",
    "controlled_receive_error",
    "controlled_receive_error_at",
}


class UaeEdiBinding(models.Model):
    _inherit = "l10n_ae.edi.binding"

    controlled_inbound_cursor = fields.Char(
        string="Controlled Receiving Cursor", readonly=True, copy=False
    )
    controlled_inbound_route = fields.Char(
        string="Controlled Receiving Route", readonly=True, copy=False
    )
    controlled_receive_enabled = fields.Boolean(
        string="Automatic Controlled Receiving",
        default=False,
        copy=False,
        help="Preserve one controlled sandbox inbox page per scheduled invocation. "
        "Accounting review remains a separate action.",
    )
    controlled_receive_error = fields.Selection(
        [
            ("configuration", "Controlled Receiving Configuration Required"),
            ("transport", "Controlled Retrieval Requires Review"),
            ("route", "Receiving Route Requires Review"),
        ],
        string="Scheduled Receiving Review",
        readonly=True,
        copy=False,
    )
    controlled_receive_error_at = fields.Datetime(
        string="Scheduled Receiving Review Time", readonly=True, copy=False
    )

    @api.constrains("controlled_receive_enabled", "environment")
    def _check_controlled_receive_environment(self):
        if any(
            binding.controlled_receive_enabled and binding.environment != "sandbox"
            for binding in self
        ):
            raise ValidationError(
                self.env._("Automatic controlled receiving requires the sandbox.")
            )

    def _record_controlled_receive_review(self, code=False):
        self.ensure_one()
        self.check_access("read")
        if self.controlled_receive_error == code and bool(
            self.controlled_receive_error_at
        ) == bool(code):
            # Keep the time of the diagnostic transition. Repeated polls must
            # not rewrite an unchanged binding and conflict with review locks.
            return True
        return super(UaeEdiBinding, self.sudo()).write(
            {
                "controlled_receive_error": code,
                "controlled_receive_error_at": fields.Datetime.now() if code else False,
            }
        )

    def _finish_controlled_receive_review(self, binding_id, company_id, code=False):
        """Write route diagnostics after worker commits, in a fresh transaction."""
        with self.env.registry.cursor() as cursor:
            env = api.Environment(
                cursor, self.env.uid, {"allowed_company_ids": [company_id]}
            )
            principal = env.user.exists()
            if (
                not principal
                or not principal.active
                or company_id not in principal.company_ids.ids
                or not (
                    principal.has_group("l10n_ae_edi_pint.group_document_receiver")
                    or principal.has_group("account.group_account_manager")
                )
            ):
                return
            binding = env["l10n_ae.edi.binding"].browse(binding_id).exists()
            if not binding or binding.company_id.id != company_id:
                return
            binding._record_controlled_receive_review(code)
            cursor.commit()

    @api.model
    def _cron_receive_controlled(self):
        if not self.env.context.get("cron_id"):
            raise UserError(
                self.env._("Use the native UAE receiving scheduled action.")
            )
        principal = self.env.user.exists()
        if (
            not principal
            or not principal.active
            or not (
                principal.has_group("l10n_ae_edi_pint.group_document_receiver")
                or principal.has_group("account.group_account_manager")
            )
        ):
            raise AccessError(
                self.env._(
                    "An active authorized UAE document receiving account is required."
                )
            )
        self._check_transport_allowed()
        bindings = self.search(
            [
                ("controlled_receive_enabled", "=", True),
                ("environment", "=", "sandbox"),
            ],
            order="id",
        )
        routes = []
        for binding in bindings:
            prefix = f"KMEE_UAE_CONTROLLED_{binding.id}"
            routes.append(
                (
                    binding.id,
                    binding.company_id.id,
                    binding.provider,
                    os.environ.get(prefix + "_ENDPOINT"),
                    os.environ.get(prefix + "_TOKEN"),
                )
            )
        registry, uid = self.env.registry, self.env.uid
        progress = self.env["ir.cron"]
        if not progress._commit_progress(remaining=len(routes)):
            return
        for binding_id, company_id, provider, endpoint, token in routes:
            configured = bool(endpoint and token)
            if configured:
                try:
                    ControlledAspClient(endpoint, token, provider)
                except (ControlledAspError, ValueError):
                    configured = False
            if not configured:
                self._finish_controlled_receive_review(
                    binding_id, company_id, "configuration"
                )
            else:
                try:
                    ControlledInboxWorker(
                        registry, uid, company_id, endpoint, token
                    ).run_page(binding_id)
                except ControlledAspError:
                    self._finish_controlled_receive_review(
                        binding_id, company_id, "transport"
                    )
                except AccessError:
                    # Do not write diagnostics for a principal revoked during HTTP.
                    _logger.info(
                        "Controlled receiving authorization is no longer available."
                    )
                except UserError:
                    self._finish_controlled_receive_review(
                        binding_id, company_id, "route"
                    )
                else:
                    self._finish_controlled_receive_review(binding_id, company_id)
            if not progress._commit_progress(1):
                return

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get(sorted(INBOUND_CHECKPOINT_FIELDS))
        if any(
            any(
                vals.get(field, defaults.get(field))
                for field in INBOUND_CHECKPOINT_FIELDS
            )
            for vals in vals_list
        ):
            raise UserError(
                self.env._("Receiving checkpoints are managed by the receiver.")
            )
        return super().create(vals_list)

    def write(self, vals):
        if INBOUND_CHECKPOINT_FIELDS.intersection(vals):
            raise UserError(
                self.env._("Receiving checkpoints are managed by the receiver.")
            )
        for binding in self:
            if binding.controlled_inbound_route and (
                vals.get("provider", binding.provider) != binding.provider
                or vals.get("company_id", binding.company_id.id)
                != binding.company_id.id
            ):
                raise UserError(
                    self.env._(
                        "Create a new binding to change a retained receiving route."
                    )
                )
        return super().write(vals)

    def _store_controlled_inbound_checkpoint(self, cursor, route):
        """Called only after authorized atomic preservation of a fetched page."""
        self.ensure_one()
        self.check_access("read")
        return super(UaeEdiBinding, self.sudo()).write(
            {"controlled_inbound_cursor": cursor, "controlled_inbound_route": route}
        )
