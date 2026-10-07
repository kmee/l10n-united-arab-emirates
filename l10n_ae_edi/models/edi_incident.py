# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import re
from datetime import timedelta
from typing import ClassVar

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class UaeEdiIncident(models.Model):
    _name = "l10n_ae.edi.incident"
    _description = "UAE Compliance Incident"
    _inherit: ClassVar[list[str]] = ["mail.thread", "mail.activity.mixin"]
    _check_company_auto = True

    name = fields.Char(required=True)
    incident_kind = fields.Selection(
        [
            ("system_failure", "System Failure"),
            ("registration_change", "Registration Amendment"),
        ],
        required=True,
        default="system_failure",
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        readonly=True,
        index=True,
        default=lambda self: self.env.company,
    )
    affected_roles = fields.Selection(
        [
            ("issuer", "Issuer"),
            ("recipient", "Recipient"),
            ("both", "Issuer and Recipient"),
        ]
    )
    occurrence_at = fields.Datetime(string="Failure Occurred At")
    detected_at = fields.Datetime(string="Failure Detected At")
    fta_confirmation_received_at = fields.Datetime(
        string="FTA Confirmation Received At"
    )
    fta_confirmation_reference = fields.Text(string="FTA Confirmation Reference")
    reason = fields.Text()
    responsible_id = fields.Many2one("res.users", ondelete="restrict")
    timezone = fields.Char(default="Asia/Dubai", required=True, readonly=True)
    calendar_start = fields.Date(string="Calendar Coverage Starts")
    calendar_end = fields.Date(string="Calendar Coverage Ends")
    calendar_holidays = fields.Json(default=list)
    calendar_holiday_dates = fields.Text(
        string="Federal Holiday Dates",
        compute="_compute_calendar_holiday_dates",
        inverse="_inverse_calendar_holiday_dates",
        help="Enter one reviewed federal holiday per line in YYYY-MM-DD format.",
    )
    calendar_source = fields.Text(string="Federal Calendar Source")
    calendar_version = fields.Char()
    source_reference = fields.Text(string="Reviewed Legal Basis")
    review_note = fields.Text()
    notification_due_date = fields.Date(readonly=True, copy=False, index=True)
    state = fields.Selection(
        [("draft", "Requires Review"), ("open", "Open")],
        default="draft",
        required=True,
        readonly=True,
        copy=False,
    )
    opened_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False, ondelete="restrict"
    )
    opened_at = fields.Datetime(readonly=True, copy=False)
    notice_ids = fields.One2many(
        "l10n_ae.edi.incident.notice", "incident_id", readonly=True
    )
    impact_ids = fields.One2many(
        "l10n_ae.edi.incident.impact", "incident_id", readonly=True, copy=False
    )
    restoration_ids = fields.One2many(
        "l10n_ae.edi.incident.restoration", "incident_id", readonly=True, copy=False
    )
    affected_transmission_ids = fields.Many2many(
        "l10n_ae.edi.transmission",
        string="Affected Documents",
        compute="_compute_recovery_overview",
        compute_sudo=False,
    )
    affected_document_count = fields.Integer(
        string="Reviewed Affected Documents",
        compute="_compute_recovery_overview",
        compute_sudo=False,
    )
    pending_submission_count = fields.Integer(
        string="Pending Submissions",
        compute="_compute_recovery_overview",
        compute_sudo=False,
    )
    pending_confirmation_count = fields.Integer(
        string="Pending Confirmations",
        compute="_compute_recovery_overview",
        compute_sudo=False,
    )
    restoration_reviewed = fields.Boolean(
        compute="_compute_recovery_overview",
        compute_sudo=False,
    )

    _protected_fields: ClassVar[set[str]] = {
        "state",
        "notification_due_date",
        "opened_by_id",
        "opened_at",
        "timezone",
        "notice_ids",
        "impact_ids",
        "restoration_ids",
        "affected_transmission_ids",
        "affected_document_count",
        "pending_submission_count",
        "pending_confirmation_count",
        "restoration_reviewed",
    }

    @api.depends("calendar_holidays")
    def _compute_calendar_holiday_dates(self):
        for record in self:
            dates = record.calendar_holidays
            record.calendar_holiday_dates = (
                "\n".join(dates)
                if isinstance(dates, list)
                and all(isinstance(value, str) for value in dates)
                else False
            )

    def _inverse_calendar_holiday_dates(self):
        for record in self:
            record.calendar_holidays = [
                line.strip()
                for line in (record.calendar_holiday_dates or "").splitlines()
                if line.strip()
            ]

    @api.depends_context("uid", "allowed_company_ids")
    @api.depends(
        "impact_ids.transmission_ids",
        "impact_ids.transmission_ids.submission_state",
        "impact_ids.transmission_ids.confirmation_state",
        "restoration_ids",
    )
    def _compute_recovery_overview(self):
        for record in self:
            # Native onchange forms use a NewId with the existing incident as origin.
            origin = record._origin
            facts = (
                origin.get_recovery_backlog()
                if origin.id
                else {
                    "affected_count": 0,
                    "submission_pending_count": 0,
                    "confirmation_pending_count": 0,
                    "restoration_reviewed": False,
                    "transmissions": [],
                }
            )
            record.affected_document_count = facts["affected_count"]
            record.pending_submission_count = facts["submission_pending_count"]
            record.pending_confirmation_count = facts["confirmation_pending_count"]
            record.restoration_reviewed = facts["restoration_reviewed"]
            record.affected_transmission_ids = self.env[
                "l10n_ae.edi.transmission"
            ].browse([row["id"] for row in facts["transmissions"]])

    def _recovery_review_action(self, model_name, view_ref, title):
        self.ensure_one()
        self._check_actor("write")
        if self.state != "open" or self.incident_kind != "system_failure":
            raise UserError(self.env._("Select an open reviewed system failure."))
        if self.env["ir.config_parameter"].sudo().get_bool("database.is_neutralized"):
            raise UserError(
                self.env._("Recovery review is disabled on a neutralized copy.")
            )
        return {
            "type": "ir.actions.act_window",
            "name": title,
            "res_model": model_name,
            "view_mode": "form",
            "view_id": self.env.ref(view_ref).id,
            "target": "new",
            "context": {"default_incident_id": self.id},
        }

    def action_review_impact(self):
        return self._recovery_review_action(
            "l10n_ae.edi.incident.impact.wizard",
            "l10n_ae_edi.edi_incident_impact_wizard_form",
            self.env._("Review Affected Documents"),
        )

    def action_review_restoration(self):
        return self._recovery_review_action(
            "l10n_ae.edi.incident.restoration.wizard",
            "l10n_ae_edi.edi_incident_restoration_wizard_form",
            self.env._("Review Service Restoration"),
        )

    def _check_actor(self, operation):
        principal = self.env.user.with_user(self.env.uid)
        if not principal.active or (
            operation != "read"
            and not principal.has_group("account.group_account_manager")
        ):
            raise AccessError(self.env._("An active incident reviewer is required."))
        self.with_user(self.env.uid).check_access(operation)
        for record in self:
            record._check_incident_company(record.company_id)

    def _check_incident_company(self, company):
        principal = self.env.user.with_user(self.env.uid)
        if (
            len(company) != 1
            or company.id not in principal.company_ids.ids
            or company.id not in self.env.companies.ids
        ):
            raise AccessError(self.env._("This incident company is not accessible."))
        if company.country_id.code != "AE":
            raise ValidationError(self.env._("Select a UAE incident company."))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_actor("create")
        prepared = []
        for values in vals_list:
            vals = dict(values)
            if self._protected_fields.intersection(vals):
                raise UserError(self.env._("Use the reviewed incident opening action."))
            company_id = vals.get("company_id", self.env.company.id)
            if type(company_id) is not int:
                raise ValidationError(
                    self.env._("Select one explicit incident company.")
                )
            company = self.env["res.company"].browse(company_id).exists()
            self._check_incident_company(company)
            vals.update(
                company_id=company.id,
                state="draft",
                timezone="Asia/Dubai",
                notification_due_date=False,
                opened_by_id=False,
                opened_at=False,
                notice_ids=[],
                impact_ids=[],
                restoration_ids=[],
            )
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        self._check_actor("write")
        if self._protected_fields.union({"company_id"}).intersection(vals):
            raise UserError(
                self.env._("Incident identity and opening history are protected.")
            )
        if self.filtered(lambda record: record.state != "draft"):
            raise UserError(
                self.env._("An opened incident's reviewed basis is immutable.")
            )
        return super().write(vals)

    def _reviewed_notification_due_date(self):
        self.ensure_one()
        if not all(
            (
                self.calendar_start,
                self.calendar_end,
            )
        ) or not all(
            (value or "").strip()
            for value in (
                self.reason,
                self.calendar_source,
                self.calendar_version,
                self.source_reference,
                self.review_note,
            )
        ):
            raise ValidationError(
                self.env._("Review the incident, source and calendar coverage.")
            )
        now = fields.Datetime.now()
        if self.incident_kind == "registration_change":
            anchor_at = self.fta_confirmation_received_at
            remaining = 5
            if not anchor_at or not (self.fta_confirmation_reference or "").strip():
                raise ValidationError(
                    self.env._("Review the FTA confirmation receipt and reference.")
                )
            if anchor_at > now:
                raise ValidationError(
                    self.env._("FTA confirmation receipt cannot be in the future.")
                )
        else:
            anchor_at = self.occurrence_at
            remaining = 2
            if not all((self.affected_roles, anchor_at, self.detected_at)) or (
                self.detected_at < anchor_at or self.detected_at > now
            ):
                raise ValidationError(
                    self.env._("Review affected roles, occurrence and detection.")
                )
        anchor = fields.Datetime.context_timestamp(
            self.with_context(tz="Asia/Dubai"), anchor_at
        ).date()
        if not self.calendar_start <= anchor <= self.calendar_end:
            raise ValidationError(
                self.env._("The reviewed calendar must cover the legal event date.")
            )
        values = self.calendar_holidays
        # Odoo JSON fields store an empty list as NULL and return False.
        if values is False:
            values = []
        if not isinstance(values, list):
            raise ValidationError(
                self.env._("Federal holidays must be a list of ISO dates.")
            )
        holidays = set()
        for value in values:
            if not isinstance(value, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", value
            ):
                raise ValidationError(
                    self.env._("Federal holidays must use ISO dates.")
                )
            try:
                day = fields.Date.to_date(value)
            except ValueError as exc:
                raise ValidationError(
                    self.env._("Review the federal holiday date.")
                ) from exc
            if not self.calendar_start <= day <= self.calendar_end or day in holidays:
                raise ValidationError(
                    self.env._("Review holiday coverage and duplicate dates.")
                )
            holidays.add(day)
        day = anchor
        while remaining:
            day += timedelta(days=1)
            if day > self.calendar_end:
                raise ValidationError(
                    self.env._("Extend reviewed calendar coverage before opening.")
                )
            if day.weekday() < 5 and day not in holidays:
                remaining -= 1
        return day

    def action_open(self):
        self.ensure_one()
        self._check_actor("write")
        if self.env["ir.config_parameter"].sudo().get_bool("database.is_neutralized"):
            raise UserError(
                self.env._("Incident activities are disabled on a neutralized copy.")
            )
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_incident WHERE id = %s FOR UPDATE", (self.id,)
        )
        self.invalidate_recordset()
        if self.state == "open":
            return True
        due = self._reviewed_notification_due_date()
        owner = self.responsible_id.with_user(self.env.uid)
        if (
            not owner
            or not owner.active
            or owner.share
            or self.company_id not in owner.company_ids
        ):
            raise ValidationError(
                self.env._("Review an active internal owner in this company.")
            )
        self.with_user(owner).check_access("read")
        registration = self.incident_kind == "registration_change"
        self.activity_schedule(
            "mail.mail_activity_data_todo",
            date_deadline=due,
            summary=(
                self.env._("Review UAE registration amendment notice")
                if registration
                else self.env._("Review UAE system failure notification")
            ),
            note=(
                self.env._(
                    "Review the written notice to the appointed ASP. "
                    "This internal reminder does not send or acknowledge a notice."
                )
                if registration
                else self.env._(
                    "Review the taxpayer notice and contact the appointed ASP. "
                    "This internal reminder does not send or acknowledge a notice."
                )
            ),
            user_id=owner.id,
        )
        super().write(
            {
                "state": "open",
                "notification_due_date": due,
                "opened_by_id": self.env.uid,
                "opened_at": fields.Datetime.now(),
            }
        )
        return True

    def action_record_notice(self):
        self.ensure_one()
        self._check_actor("write")
        if self.state != "open":
            raise UserError(self.env._("Open the reviewed incident first."))
        if self.env["ir.config_parameter"].sudo().get_bool("database.is_neutralized"):
            raise UserError(
                self.env._("Notice recording is disabled on a neutralized copy.")
            )
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Record Supplied Notice Evidence"),
            "res_model": "l10n_ae.edi.incident.notice.wizard",
            "view_mode": "form",
            "view_id": self.env.ref("l10n_ae_edi.edi_incident_notice_wizard_form").id,
            "target": "new",
            "context": {
                "default_incident_id": self.id,
                "default_recipient_role": (
                    "asp" if self.incident_kind == "registration_change" else "fta"
                ),
                "default_taxpayer_roles": self.affected_roles or False,
            },
        }

    def record_notice(self, evidence):
        self.ensure_one()
        self._check_actor("write")
        if self.env["ir.config_parameter"].sudo().get_bool("database.is_neutralized"):
            raise UserError(
                self.env._(
                    "Operational notice recording is disabled on a neutralized copy."
                )
            )
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_incident WHERE id = %s FOR UPDATE", (self.id,)
        )
        self.invalidate_recordset()
        notice = self.env["l10n_ae.edi.incident.notice"]._record(self, evidence)
        notice.check_access("read")
        return {
            "id": notice.id,
            "document_sha256": notice.document_sha256,
            "notice_purpose": notice.notice_purpose,
            "evidence_origin": notice.evidence_origin,
        }

    def record_affected_transmissions(self, evidence):
        self.ensure_one()
        self._check_actor("write")
        if self.env["ir.config_parameter"].sudo().get_bool("database.is_neutralized"):
            raise UserError(
                self.env._("Impact recording is disabled on a neutralized copy.")
            )
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_incident WHERE id = %s FOR UPDATE", (self.id,)
        )
        self.invalidate_recordset()
        batch = self.env["l10n_ae.edi.incident.impact"]._record(self, evidence)
        batch.check_access("read")
        self.invalidate_recordset(["impact_ids"])
        return {"id": batch.id, "transmission_ids": batch.transmission_ids.ids}

    def record_restoration(self, evidence):
        self.ensure_one()
        self._check_actor("write")
        if self.env["ir.config_parameter"].sudo().get_bool("database.is_neutralized"):
            raise UserError(
                self.env._("Restoration review is disabled on a neutralized copy.")
            )
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_incident WHERE id = %s FOR UPDATE", (self.id,)
        )
        self.invalidate_recordset()
        review = self.env["l10n_ae.edi.incident.restoration"]._record(self, evidence)
        review.check_access("read")
        self.invalidate_recordset(["restoration_ids"])
        return {"id": review.id}

    def get_recovery_backlog(self):
        """Readonly local facts; transport remains in the post-commit executor."""
        self.ensure_one()
        self._check_actor("read")
        incident = self.with_user(self.env.uid)
        issues = incident.impact_ids.mapped("transmission_ids").sorted("id")
        issues.check_access("read")
        rows = issues.read(
            [
                "move_id",
                "binding_id",
                "process",
                "revision",
                "idempotency_key",
                "payload_hash",
                "xml_hash",
                "submission_state",
                "confirmation_state",
                "validation_c2_state",
                "delivery_c3_state",
                "reporting_c2_state",
                "reporting_c3_state",
                "next_attempt_at",
                "dispatch_review_required",
                "event_review_required",
                "local_correction_id",
                "attempt_ids",
            ]
        )
        return {
            "incident_id": incident.id,
            "restoration_reviewed": bool(incident.restoration_ids),
            "restoration_ids": incident.restoration_ids.ids,
            "affected_count": len(rows),
            "submission_pending_count": sum(
                row["submission_state"] != "accepted" for row in rows
            ),
            "confirmation_pending_count": sum(
                row["confirmation_state"] != "confirmed" for row in rows
            ),
            "transmissions": rows,
        }

    @api.ondelete(at_uninstall=False)
    def _unlink_except_opened_incident(self):
        self._check_actor("unlink")
        if self.filtered(lambda record: record.state != "draft"):
            raise UserError(self.env._("Opened incident history cannot be deleted."))
