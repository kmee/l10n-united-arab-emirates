# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from datetime import timedelta
from typing import ClassVar

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class UaeEdiInboxTask(models.Model):
    _name = "l10n_ae.edi.inbox.task"
    _description = "UAE Received Document Internal Review"
    _inherit: ClassVar[list[str]] = ["mail.thread", "mail.activity.mixin"]
    _check_company_auto = True

    name = fields.Char(required=True, readonly=True)
    inbox_id = fields.Many2one(
        "l10n_ae.edi.inbox",
        string="Received Original",
        required=True,
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, index=True
    )
    xml_hash = fields.Char(string="Original XML SHA-256", required=True, readonly=True)
    requested_by_id = fields.Many2one(
        "res.users", required=True, readonly=True, ondelete="restrict"
    )
    requested_at = fields.Datetime(required=True, readonly=True)
    assigned_to_id = fields.Many2one("res.users", readonly=True, ondelete="restrict")
    assigned_by_id = fields.Many2one("res.users", readonly=True, ondelete="restrict")
    assigned_at = fields.Datetime(readonly=True)
    assigned_activity_id = fields.Many2one(
        "mail.activity", readonly=True, ondelete="set null"
    )
    state = fields.Selection(
        [("open", "Requires Attention"), ("resolved", "Resolved Internally")],
        required=True,
        readonly=True,
        default="open",
    )
    resolved_by_id = fields.Many2one("res.users", readonly=True, ondelete="restrict")
    resolved_at = fields.Datetime(readonly=True)
    next_check_at = fields.Datetime(required=True, readonly=True, index=True)
    last_checked_at = fields.Datetime(readonly=True)
    last_check_state = fields.Selection(
        [("checked", "Checked"), ("review_required", "Requires Configuration Review")],
        readonly=True,
    )
    check_review_note = fields.Text(readonly=True)
    source_state = fields.Selection(related="inbox_id.state", readonly=True)
    source_identity_conflict = fields.Boolean(
        related="inbox_id.identity_conflict", readonly=True
    )

    _one_original_review = models.UniqueIndex(
        "(inbox_id)", "This received original already has an internal review."
    )

    def _check_current_manager(self):
        actor = self.env.user.sudo(False)
        if (
            not actor.active
            or actor.share
            or not actor.has_group("account.group_account_manager")
        ):
            raise AccessError(
                self.env._("An active Accounting Manager must request internal review.")
            )
        current = self.with_user(self.env.uid).sudo(False)
        current.check_access("write")
        for task in current:
            task.inbox_id.check_access("read")
            task.inbox_id._check_receiving_company(task.company_id)
            if task.company_id != task.inbox_id.company_id:
                raise ValidationError(
                    self.env._("Review source company requires review.")
                )
        return current

    @api.model
    def _for_original(self, inbox):
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.env.uid)
            .sudo(False)
            .browse(inbox.id)
        )
        inbox.ensure_one()
        inbox._check_operator()
        inbox.check_access("read")
        inbox._check_receiving_company(inbox.company_id)
        inbox.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_binding WHERE id = %s FOR UPDATE",
            [inbox.binding_id.id],
        )
        inbox.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_inbox WHERE id = %s FOR UPDATE", [inbox.id]
        )
        inbox.invalidate_recordset()
        task = (
            self.with_user(self.env.uid)
            .sudo(False)
            .search([("inbox_id", "=", inbox.id)], limit=1)
        )
        if task:
            return task
        if inbox.state == "processed":
            raise UserError(
                self.env._(
                    "This processed original does not require a new internal review."
                )
            )
        # Notification facts cannot inherit caller defaults.
        # pylint: disable=context-overridden
        current = (
            self.with_user(self.env.uid)
            .sudo(False)
            .with_context({"allowed_company_ids": inbox.company_id.ids})
        )
        return super(UaeEdiInboxTask, current).create(
            {
                "name": inbox.remote_id,
                "inbox_id": inbox.id,
                "company_id": inbox.company_id.id,
                "xml_hash": inbox.xml_hash,
                "requested_by_id": self.env.uid,
                "requested_at": fields.Datetime.now(),
                "assigned_to_id": False,
                "assigned_by_id": False,
                "assigned_at": False,
                "assigned_activity_id": False,
                "state": "open",
                "resolved_by_id": False,
                "resolved_at": False,
                "next_check_at": fields.Datetime.now(),
                "last_checked_at": False,
                "last_check_state": False,
                "check_review_note": False,
            }
        )

    def _notify_reviewer(self):
        self.ensure_one()
        current = self._check_current_manager()
        if self.env["ir.config_parameter"].sudo().get_bool("database.is_neutralized"):
            raise UserError(self.env._("Neutralized copies cannot request review."))
        if current.inbox_id.state == "processed":
            return False
        owner = (
            current.assigned_to_id
            if current.assigned_at
            else current.company_id.l10n_ae_inbox_reviewer_id
        )
        if (
            not owner
            or not owner.active
            or owner.share
            or current.company_id not in owner.company_ids
            or not owner.has_group("account.group_account_manager")
        ):
            raise ValidationError(
                self.env._(
                    "Configure an active incoming document reviewer for the company."
                )
            )
        current.inbox_id.with_user(owner).with_context(
            allowed_company_ids=current.company_id.ids
        ).check_access("read")
        if current.assigned_at:
            return False
        activity = current.activity_schedule(
            "mail.mail_activity_data_todo",
            user_id=owner.id,
            summary=self.env._("Review received UAE document"),
            date_deadline=fields.Date.context_today(current),
            note=self.env._(
                "This internal review request does not post, transmit or acknowledge "
                "the received document."
            ),
        )
        if not activity:
            raise UserError(
                self.env._("The native internal review activity was not created.")
            )
        super(UaeEdiInboxTask, current).write(
            {
                "assigned_to_id": owner.id,
                "assigned_by_id": self.env.uid,
                "assigned_at": fields.Datetime.now(),
                "assigned_activity_id": activity.id,
            }
        )
        return True

    @api.model
    def _cron_incoming_review(self):
        if not self.env.context.get("cron_id"):
            raise UserError(
                self.env._("Use the native incoming review scheduled action.")
            )
        actor = self.env.user.sudo(False)
        if (
            not actor.active
            or actor.share
            or not actor.has_group("account.group_account_manager")
        ):
            raise AccessError(
                self.env._("An active Accounting Manager must request internal review.")
            )
        if self.env["ir.config_parameter"].sudo().get_bool("database.is_neutralized"):
            raise UserError(self.env._("Neutralized copies cannot request review."))
        model = (
            self.with_user(self.env.uid)
            .sudo(False)
            .with_context(allowed_company_ids=actor.company_ids.ids)
        )
        sources = model.env["l10n_ae.edi.inbox"].search(
            [
                "|",
                "&",
                ("state", "in", ["received", "review"]),
                ("internal_review_task_ids", "=", False),
                (
                    "internal_review_task_ids",
                    "any",
                    [
                        ("state", "=", "open"),
                        ("next_check_at", "<=", fields.Datetime.now()),
                    ],
                ),
            ],
            order="received_at, id",
            limit=100,
        )
        for source in sources:
            task = model._for_original(source)
            outcome, note = "checked", False
            try:
                with self.env.cr.savepoint():
                    if source.state == "processed":
                        task.action_resolve_review()
                    else:
                        task._notify_reviewer()
            except (AccessError, UserError):
                outcome = "review_required"
                note = self.env._(
                    "Review the current original, company, reviewer or access "
                    "before retrying."
                )
            checked = fields.Datetime.now()
            super(UaeEdiInboxTask, task).write(
                {
                    "last_checked_at": checked,
                    "next_check_at": checked + timedelta(hours=1),
                    "last_check_state": outcome,
                    "check_review_note": note,
                }
            )
        return len(sources)

    def action_resolve_review(self):
        self.ensure_one()
        current = self._check_current_manager()
        current.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_binding WHERE id = %s FOR UPDATE",
            [current.inbox_id.binding_id.id],
        )
        current.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_inbox WHERE id = %s FOR UPDATE",
            [current.inbox_id.id],
        )
        current.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_inbox_task WHERE id = %s FOR UPDATE",
            [current.id],
        )
        current.invalidate_recordset()
        source = current.inbox_id
        source.invalidate_recordset()
        if (
            source.state != "processed"
            or source.identity_review_pending
            or not source.move_id
        ):
            raise UserError(
                self.env._(
                    "Process the received document successfully "
                    "before resolving review."
                )
            )
        source.move_id.check_access("read")
        if current.state == "resolved":
            return True
        activity = current.assigned_activity_id.exists()
        if activity:
            activity.action_feedback(
                feedback=self.env._(
                    "The received document has been processed successfully. "
                    "This resolves internal attention only, "
                    "without fiscal acknowledgement."
                )
            )
        super(UaeEdiInboxTask, current).write(
            {
                "state": "resolved",
                "resolved_by_id": self.env.uid,
                "resolved_at": fields.Datetime.now(),
            }
        )
        return True

    @api.model_create_multi
    def create(self, vals_list):
        # pylint: disable=method-required-super
        raise UserError(
            self.env._("Internal received review is created by its request action.")
        )

    def write(self, vals):
        current = self._check_current_manager()
        protected = {
            "name",
            "inbox_id",
            "company_id",
            "xml_hash",
            "requested_by_id",
            "requested_at",
            "assigned_to_id",
            "assigned_by_id",
            "assigned_at",
            "assigned_activity_id",
            "state",
            "resolved_by_id",
            "resolved_at",
            "next_check_at",
            "last_checked_at",
            "last_check_state",
            "check_review_note",
        }
        if protected.intersection(vals):
            raise UserError(
                self.env._("Internal received review history is protected.")
            )
        return super(UaeEdiInboxTask, current).write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_review_history(self):
        raise UserError(
            self.env._("Internal received review history cannot be deleted.")
        )
