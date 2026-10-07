# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import re
from typing import ClassVar

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class UaeEdiCalendar(models.Model):
    _name = "l10n_ae.edi.calendar"
    _description = "UAE Reviewed Federal Calendar Version"
    _check_company_auto = True

    name = fields.Char(required=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        readonly=True,
        default=lambda self: self.env.company,
        ondelete="restrict",
    )
    version = fields.Char(required=True)
    timezone = fields.Char(default="Asia/Dubai", required=True, readonly=True)
    calendar_start = fields.Date(string="Calendar Coverage Starts")
    calendar_end = fields.Date(string="Calendar Coverage Ends")
    calendar_holidays = fields.Json(default=list)
    calendar_holiday_dates = fields.Text(
        string="Federal Holiday Dates",
        compute="_compute_holiday_dates",
        inverse="_inverse_holiday_dates",
        help=(
            "One reviewed ISO date per line. Forecast dates are not observed holidays."
        ),
    )
    source_reference = fields.Text(string="Calendar Source Reference")
    source_snapshot = fields.Text(
        string="Retained Source Snapshot",
        help=(
            "Preserve the consulted source text and publication details. "
            "Approval does not authenticate it."
        ),
    )
    source_sha256 = fields.Char(readonly=True, copy=False)
    review_note = fields.Text()
    state = fields.Selection(
        [("draft", "Requires Review"), ("approved", "Reviewed")],
        default="draft",
        required=True,
        readonly=True,
        copy=False,
    )
    approved_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False, ondelete="restrict"
    )
    approved_at = fields.Datetime(readonly=True, copy=False)

    _version_unique = models.UniqueIndex(
        "(company_id, version)", "Use a distinct calendar version for this company."
    )
    _protected_fields: ClassVar[set[str]] = {
        "state",
        "approved_by_id",
        "approved_at",
        "source_sha256",
        "timezone",
    }

    @api.depends("calendar_holidays")
    def _compute_holiday_dates(self):
        for record in self:
            dates = record.calendar_holidays
            record.calendar_holiday_dates = (
                "\n".join(dates)
                if isinstance(dates, list)
                and all(isinstance(day, str) for day in dates)
                else False
            )

    def _inverse_holiday_dates(self):
        for record in self:
            record.calendar_holidays = [
                line.strip()
                for line in (record.calendar_holiday_dates or "").splitlines()
                if line.strip()
            ]

    def _check_actor(self, operation):
        principal = self.env.user.with_user(self.env.uid)
        if (
            not principal.active
            or principal.share
            or (
                operation != "read"
                and not principal.has_group("account.group_account_manager")
            )
        ):
            raise AccessError(self.env._("An active calendar reviewer is required."))
        self.with_user(self.env.uid).check_access(operation)
        for record in self:
            self.env["l10n_ae.edi.incident"]._check_incident_company(record.company_id)

    @api.model_create_multi
    def create(self, vals_list):
        self._check_actor("create")
        prepared = []
        for values in vals_list:
            vals = dict(values)
            if self._protected_fields.intersection(vals):
                raise UserError(self.env._("Use the calendar approval action."))
            company_id = vals.get("company_id", self.env.company.id)
            if type(company_id) is not int:
                raise ValidationError(
                    self.env._("Select one explicit calendar company.")
                )
            company = self.env["res.company"].browse(company_id).exists()
            self.env["l10n_ae.edi.incident"]._check_incident_company(company)
            vals.update(
                company_id=company.id,
                timezone="Asia/Dubai",
                state="draft",
                approved_by_id=False,
                approved_at=False,
                source_sha256=False,
            )
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        self._check_actor("write")
        if self._protected_fields.union({"company_id"}).intersection(vals):
            raise UserError(self.env._("Calendar identity and approval are protected."))
        if self.filtered(lambda record: record.state == "approved"):
            raise UserError(self.env._("Reviewed calendar versions are immutable."))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_reviewed_calendar(self):
        self._check_actor("unlink")
        if self.filtered(lambda record: record.state == "approved"):
            raise UserError(self.env._("Reviewed calendar versions cannot be deleted."))

    def _validate_basis(self):
        self.ensure_one()
        if (
            not self.calendar_start
            or not self.calendar_end
            or self.calendar_start > self.calendar_end
        ):
            raise ValidationError(self.env._("Review calendar coverage."))
        if not all(
            (text or "").strip()
            for text in (
                self.version,
                self.source_reference,
                self.source_snapshot,
                self.review_note,
            )
        ):
            raise ValidationError(
                self.env._("Review calendar version, source snapshot and note.")
            )
        dates = self.calendar_holidays or []
        if not isinstance(dates, list):
            raise ValidationError(
                self.env._("Federal holidays must be a list of ISO dates.")
            )
        seen = set()
        for value in dates:
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
            if not self.calendar_start <= day <= self.calendar_end or day in seen:
                raise ValidationError(
                    self.env._("Review holiday coverage and duplicate dates.")
                )
            seen.add(day)

    def action_approve(self):
        self.ensure_one()
        self._check_actor("write")
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_calendar WHERE id = %s FOR UPDATE", (self.id,)
        )
        self.invalidate_recordset()
        if self.state == "approved":
            return True
        self._validate_basis()
        super().write(
            {
                "state": "approved",
                "approved_by_id": self.env.uid,
                "approved_at": fields.Datetime.now(),
                "source_sha256": hashlib.sha256(
                    self.source_snapshot.encode()
                ).hexdigest(),
            }
        )
        return True

    def _incident_snapshot(self, company):
        self.ensure_one()
        self._check_actor("read")
        if self.company_id != company or self.state != "approved":
            raise ValidationError(
                self.env._("Select a reviewed calendar in this company.")
            )
        if (
            hashlib.sha256(self.source_snapshot.encode()).hexdigest()
            != self.source_sha256
        ):
            raise UserError(
                self.env._("The retained calendar source failed integrity review.")
            )
        return {
            "calendar_start": self.calendar_start,
            "calendar_end": self.calendar_end,
            "calendar_holidays": self.calendar_holidays or [],
            "calendar_source": self.source_reference,
            "calendar_version": self.version,
            "calendar_source_sha256": self.source_sha256,
        }
