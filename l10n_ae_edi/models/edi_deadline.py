# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from datetime import timedelta
from typing import ClassVar

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class UaeEdiDeadline(models.Model):
    _name = "l10n_ae.edi.deadline"
    _description = "UAE Reviewed Document Deadline"
    _inherit: ClassVar[list[str]] = ["mail.thread", "mail.activity.mixin"]
    _check_company_auto = True

    name = fields.Char(required=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        readonly=True,
        index=True,
        default=lambda self: self.env.company,
    )
    move_id = fields.Many2one(
        "account.move",
        string="Accounting Document",
        required=True,
        check_company=True,
        ondelete="restrict",
        index=True,
    )
    registration_status = fields.Selection(
        [
            ("vat_registered", "VAT Registered"),
            ("not_vat_registered", "Not VAT Registered"),
        ]
    )
    rule_basis = fields.Selection(
        [
            ("vat", "Reviewed VAT Rule"),
            ("residual", "Reviewed Non-VAT Rule"),
            ("exception", "Other Reviewed Rule or Exception"),
        ]
    )
    anchor_kind = fields.Selection(
        [
            ("supply", "Reviewed Date of Supply"),
            ("business_transaction", "Reviewed Business Transaction Date"),
            ("credit_adjustment", "Reviewed Credit Adjustment Event"),
            ("other", "Other Reviewed Event"),
        ]
    )
    anchor_date = fields.Date()
    due_date = fields.Date(string="Inclusive Due Date", index=True)
    reminder_date = fields.Date()
    timezone = fields.Char(default="Asia/Dubai", readonly=True, required=True)
    calendar_basis = fields.Text()
    source_reference = fields.Text()
    policy_version = fields.Char()
    review_note = fields.Text()
    responsible_id = fields.Many2one("res.users", ondelete="restrict")
    requested_fact = fields.Selection(
        [
            ("submission", "API Submission Accepted"),
            ("confirmations", "Independent Confirmations Complete"),
        ]
    )
    state = fields.Selection(
        [("draft", "Requires Review"), ("approved", "Reviewed")],
        default="draft",
        readonly=True,
        required=True,
        copy=False,
    )
    approved_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False, ondelete="restrict"
    )
    approved_at = fields.Datetime(readonly=True, copy=False)
    alerted_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False, ondelete="restrict"
    )
    alerted_at = fields.Datetime(readonly=True, copy=False, index=True)
    next_check_at = fields.Datetime(readonly=True, copy=False, index=True)
    last_checked_at = fields.Datetime(readonly=True, copy=False)
    last_check_state = fields.Selection(
        [("assessed", "Assessed"), ("review_required", "Requires Review")],
        readonly=True,
        copy=False,
    )
    check_review_note = fields.Char(readonly=True, copy=False)

    def _check_actor(self, operation):
        principal = self.env.user.with_user(self.env.uid)
        if not principal.active or (
            operation != "read"
            and not principal.has_group("account.group_account_manager")
        ):
            raise AccessError(
                self.env._("An active authorized deadline reviewer is required.")
            )
        self.with_user(self.env.uid).check_access(operation)
        for record in self:
            record._check_document(record.move_id)
            if record.company_id != record.move_id.company_id:
                raise ValidationError(
                    self.env._(
                        "Review the deadline after the accounting company changes."
                    )
                )

    def _check_document(self, document):
        document.ensure_one()
        principal = self.env.user.with_user(self.env.uid)
        if (
            not principal.active
            or document.company_id.id not in principal.company_ids.ids
            or document.company_id.id not in self.env.companies.ids
        ):
            raise AccessError(
                self.env._("The current user cannot access this deadline company.")
            )
        document.with_user(self.env.uid).check_access("read")
        if document.company_id.country_id.code != "AE" or not document.is_invoice():
            raise ValidationError(
                self.env._("Select a UAE accounting invoice or credit note.")
            )

    @api.model_create_multi
    def create(self, vals_list):
        self._check_actor("create")
        prepared = []
        for values in vals_list:
            vals = dict(values)
            if {
                "state",
                "approved_by_id",
                "approved_at",
                "alerted_by_id",
                "alerted_at",
                "next_check_at",
                "last_checked_at",
                "last_check_state",
                "check_review_note",
            }.intersection(vals):
                raise UserError(
                    self.env._("Use the deadline review action to record approval.")
                )
            document = self.env["account.move"].browse(vals.get("move_id")).exists()
            if len(document) != 1:
                raise ValidationError(
                    self.env._("Select one explicit accounting document.")
                )
            self._check_document(document)
            if vals.get("company_id", document.company_id.id) != document.company_id.id:
                raise ValidationError(
                    self.env._("Deadline and document must have the same company.")
                )
            if vals.get("timezone", "Asia/Dubai") != "Asia/Dubai":
                raise ValidationError(
                    self.env._("UAE deadline dates use the Asia/Dubai calendar.")
                )
            vals.update(
                company_id=document.company_id.id,
                timezone="Asia/Dubai",
                state="draft",
                approved_by_id=False,
                approved_at=False,
                alerted_by_id=False,
                alerted_at=False,
                next_check_at=fields.Datetime.now(),
                last_checked_at=False,
                last_check_state=False,
                check_review_note=False,
            )
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        self._check_actor("write")
        if {
            "state",
            "approved_by_id",
            "approved_at",
            "company_id",
            "timezone",
            "alerted_by_id",
            "alerted_at",
            "next_check_at",
            "last_checked_at",
            "last_check_state",
            "check_review_note",
        }.intersection(vals):
            raise UserError(
                self.env._("Reviewed deadline identity and approval are protected.")
            )
        if self.filtered(lambda record: record.state == "approved"):
            raise UserError(self.env._("Reviewed deadline bases are immutable."))
        if "move_id" in vals:
            document = self.env["account.move"].browse(vals["move_id"]).exists()
            if len(document) != 1:
                raise ValidationError(
                    self.env._("Select one explicit accounting document.")
                )
            self._check_document(document)
            if any(document.company_id != record.company_id for record in self):
                raise ValidationError(
                    self.env._("Deadline and document must have the same company.")
                )
        return super().write(vals)

    def action_approve(self):
        self._check_actor("write")
        pending = self.filtered(lambda record: record.state == "draft")
        for record in pending:
            if not all(
                (
                    record.registration_status,
                    record.rule_basis,
                    record.anchor_kind,
                    record.anchor_date,
                    record.due_date,
                    record.reminder_date,
                    record.responsible_id,
                    record.requested_fact,
                )
            ):
                raise ValidationError(
                    self.env._(
                        "Review the applicable rule, dates, owner and requested fact."
                    )
                )
            if not all(
                (value or "").strip()
                for value in (
                    record.calendar_basis,
                    record.source_reference,
                    record.policy_version,
                    record.review_note,
                )
            ):
                raise ValidationError(
                    self.env._(
                        "Review the calendar, source, version and explanatory note."
                    )
                )
            if (
                record.due_date < record.anchor_date
                or record.reminder_date > record.due_date
            ):
                raise ValidationError(
                    self.env._(
                        "The deadline must be on or after its anchor, "
                        "and the reminder no later than the deadline."
                    )
                )
            owner = record.responsible_id.with_user(self.env.uid)
            if (
                not owner.active
                or owner.share
                or record.company_id not in owner.company_ids
            ):
                raise ValidationError(
                    self.env._(
                        "The responsible internal user must be active in this company."
                    )
                )
            if (
                record.rule_basis == "vat"
                and record.registration_status != "vat_registered"
            ) or (
                record.rule_basis == "residual"
                and record.registration_status != "not_vat_registered"
            ):
                raise ValidationError(
                    self.env._(
                        "Review a rule consistent with the declared VAT registration."
                    )
                )
        for record in pending:
            super(UaeEdiDeadline, record).write(
                {
                    "state": "approved",
                    "approved_by_id": self.env.uid,
                    "approved_at": fields.Datetime.now(),
                }
            )
        return True

    @api.readonly
    def get_deadline_status(self, as_of=None):
        self.ensure_one()
        self._check_actor("read")
        try:
            day = (
                fields.Date.to_date(as_of)
                if as_of
                else fields.Date.context_today(self.with_context(tz="Asia/Dubai"))
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                self.env._("Supply a valid deadline assessment date.")
            ) from exc
        issue = self.move_id.l10n_ae_transmission_id.with_user(self.env.uid)
        observed = False
        if issue:
            issue.check_access("read")
            review = issue.dispatch_review_required or issue.event_review_required
            observed = (
                bool(self.requested_fact)
                and not review
                and (
                    issue.submission_state == "accepted"
                    if self.requested_fact == "submission"
                    else issue.confirmation_state == "confirmed"
                )
            )
        state = "review"
        if self.state == "approved":
            if observed:
                state = "observed"
            elif day > self.due_date:
                state = "overdue"
            elif day == self.due_date:
                state = "due_today"
            elif day >= self.reminder_date:
                state = "due_soon"
            else:
                state = "pending"
        return {
            "state": state,
            "as_of": fields.Date.to_string(day),
            "due_date": fields.Date.to_string(self.due_date)
            if self.due_date
            else False,
            "observed": bool(observed),
            "requested_fact": self.requested_fact,
            "transmission_id": issue.id or False,
            "timeliness": "not_assessed",
        }

    @api.readonly
    def action_assess_deadline(self, as_of=None):
        status = self.get_deadline_status(as_of)
        labels = {
            "review": self.env._("Requires Review"),
            "pending": self.env._("Pending"),
            "due_soon": self.env._("Due Soon"),
            "due_today": self.env._("Due Today"),
            "overdue": self.env._("Overdue"),
            "observed": self.env._("Requested Fact Observed"),
        }
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("UAE Document Deadline Assessment"),
                "message": self.env._(
                    "Status: %(state)s. Assessed on: %(date)s (Asia/Dubai). "
                    "Inclusive due date: %(deadline)s. "
                    "An API receipt is separate from complete confirmations. "
                    "This assessment does not determine legal timeliness.",
                    state=labels[status["state"]],
                    date=status["as_of"],
                    deadline=status["due_date"] or self.env._("Not Reviewed"),
                ),
                "type": "info",
                "sticky": True,
            },
        }

    @api.ondelete(at_uninstall=False)
    def _unlink_except_reviewed_deadline(self):
        self._check_actor("unlink")
        if self.filtered(lambda record: record.state == "approved"):
            raise UserError(self.env._("Reviewed deadline history cannot be deleted."))

    def action_schedule_reminder(self, as_of=None):
        self.ensure_one()
        self._check_actor("write")
        if as_of is not None:
            raise UserError(
                self.env._(
                    "Reminder creation uses today's UAE date. "
                    "Use deadline assessment to consult another date."
                )
            )
        if self.env["ir.config_parameter"].sudo().get_bool("database.is_neutralized"):
            raise UserError(
                self.env._("Deadline reminders are disabled on a neutralized copy.")
            )
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_deadline WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        self.invalidate_recordset(["state", "alerted_at"])
        status = self.get_deadline_status()
        if self.alerted_at or status["state"] not in {
            "due_soon",
            "due_today",
            "overdue",
        }:
            return False
        owner = self.responsible_id.with_user(self.env.uid)
        if not owner.active or owner.share or self.company_id not in owner.company_ids:
            raise ValidationError(
                self.env._("Review an active responsible user in this company.")
            )
        self.with_user(owner).check_access("read")
        self.move_id.with_user(owner).check_access("read")
        activity = self.activity_schedule(
            "mail.mail_activity_data_todo",
            date_deadline=self.due_date,
            summary=self.env._("Review UAE document deadline"),
            note=self.env._(
                "The reviewed document deadline requires attention. "
                "An internal reminder does not dispatch or confirm a fiscal document."
            ),
            user_id=owner.id,
        )
        if not activity:
            return False
        super().write(
            {
                "alerted_at": fields.Datetime.now(),
                "alerted_by_id": self.env.uid,
            }
        )
        return True

    @api.model
    def _cron_deadline_reminders(self):
        if not self.env.context.get("cron_id"):
            raise UserError(self.env._("Use the native UAE deadline scheduled action."))
        self._check_actor("write")
        if self.env["ir.config_parameter"].sudo().get_bool("database.is_neutralized"):
            raise UserError(
                self.env._("Deadline reminders are disabled on a neutralized copy.")
            )
        principal = self.env.user.with_user(self.env.uid)
        model = self.sudo(False).with_context(
            allowed_company_ids=principal.company_ids.ids
        )
        today = fields.Date.context_today(model.with_context(tz="Asia/Dubai"))
        now = fields.Datetime.now()
        records = model.search(
            [
                ("state", "=", "approved"),
                ("alerted_at", "=", False),
                ("reminder_date", "<=", today),
                ("next_check_at", "<=", now),
            ],
            order="next_check_at, id",
            limit=100,
        )
        created = 0
        for record in records:
            outcome, note = "assessed", False
            try:
                with self.env.cr.savepoint():
                    scheduled = record.action_schedule_reminder()
                created += bool(scheduled)
            except (AccessError, UserError):
                outcome = "review_required"
                note = self.env._(
                    "Review the current document, company, owner or access "
                    "before retrying."
                )
            checked = fields.Datetime.now()
            super(UaeEdiDeadline, record).write(
                {
                    "next_check_at": checked + timedelta(hours=1),
                    "last_checked_at": checked,
                    "last_check_state": outcome,
                    "check_review_note": note,
                }
            )
        return created
