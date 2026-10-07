# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import base64
from datetime import date

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaeIncident(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()

    @freeze_time("2026-12-01 12:00:00")
    def test_manager_enters_reviewed_holidays_in_native_incident_form(self):
        manager = new_test_user(
            self.env,
            login="synthetic-incident-form-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        view = self.env.ref("l10n_ae_edi.edi_incident_form", raise_if_not_found=False)
        self.assertTrue(view, "The native incident review form must be installed")
        with Form(
            self.env["l10n_ae.edi.incident"].with_user(manager), view=view
        ) as form:
            form.name = "Synthetic incident entered through the native form"
            form.affected_roles = "both"
            form.occurrence_at = fields.Datetime.to_datetime("2026-11-30 21:30:00")
            form.detected_at = fields.Datetime.to_datetime("2026-12-01 08:00:00")
            form.reason = "Synthetic electronic issue and receipt failure"
            form.responsible_id = manager
            form.calendar_start = date(2026, 12, 1)
            form.calendar_end = date(2026, 12, 10)
            form.calendar_holiday_dates = "2026-12-02\n2026-12-03"
            form.calendar_source = "Reviewed synthetic federal holidays"
            form.calendar_version = "synthetic-federal-2026"
            form.source_reference = "MD243 Art12; reviewed Art49 day exclusion"
            form.review_note = "Dubai Dec1 anchor; Dec4 day1 and Dec7 day2"
        incident = form.record
        self.assertEqual(incident.company_id, self.env.company)
        self.assertEqual(incident.calendar_holidays, ["2026-12-02", "2026-12-03"])
        self.assertEqual(
            incident.occurrence_at, fields.Datetime.to_datetime("2026-11-30 21:30:00")
        )
        self.assertEqual(
            incident.detected_at, fields.Datetime.to_datetime("2026-12-01 08:00:00")
        )
        self.assertFalse(incident.notification_due_date)
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.notification_due_date, date(2026, 12, 7))
        self.assertEqual(len(incident.activity_ids), 1)
        self.assertEqual(incident.activity_ids.user_id, manager)
        self.assertEqual(incident.activity_ids.date_deadline, date(2026, 12, 7))
        reviewed = Form(incident, view=view)
        self.assertEqual(reviewed.calendar_holiday_dates, "2026-12-02\n2026-12-03")
        with self.assertRaises(AssertionError):
            reviewed.calendar_holiday_dates = "2026-12-04"
        self.assertEqual(incident.calendar_holidays, ["2026-12-02", "2026-12-03"])

    @freeze_time("2026-12-08 12:00:00")
    def test_registration_notice_due_from_receiving_fta_confirmation(self):
        manager = new_test_user(
            self.env,
            login="synthetic-registration-notice-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        incident = (
            self.env["l10n_ae.edi.incident"]
            .with_user(manager)
            .create(
                {
                    "name": "Synthetic FTA registration amendment confirmation",
                    "incident_kind": "registration_change",
                    "company_id": self.env.company.id,
                    "fta_confirmation_received_at": "2026-11-30 21:30:00",
                    "reason": "Synthetic confirmation of changed business address",
                    "responsible_id": manager.id,
                    "calendar_start": "2026-12-01",
                    "calendar_end": "2026-12-10",
                    "calendar_holidays": ["2026-12-02", "2026-12-03"],
                    "calendar_source": "Reviewed synthetic federal holidays",
                    "calendar_version": "synthetic-federal-2026",
                    "source_reference": "MD243 Art5.3; reviewed Art49 day exclusion",
                    "review_note": "Dubai Dec1 receipt; Dec4,7,8,9,10 are days1-5",
                }
            )
        )
        with self.cr.savepoint(), self.assertRaises(ValidationError):
            incident.action_open()
        self.assertEqual(incident.state, "draft")
        self.assertFalse(incident.notification_due_date)
        self.assertFalse(incident.activity_ids)
        incident.fta_confirmation_reference = "Synthetic FTA confirmation receipt A"
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.notification_due_date, date(2026, 12, 10))
        self.assertFalse(incident.occurrence_at)
        self.assertFalse(incident.detected_at)
        self.assertEqual(
            incident.fta_confirmation_received_at,
            fields.Datetime.to_datetime("2026-11-30 21:30:00"),
        )
        activities = incident.activity_ids
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities.user_id, manager)
        self.assertEqual(activities.date_deadline, date(2026, 12, 10))
        self.assertEqual(activities.summary, "Review UAE registration amendment notice")
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.activity_ids.ids, activities.ids)
        with self.cr.savepoint(), self.assertRaises(UserError):
            incident.write({"fta_confirmation_received_at": "2026-12-08 08:00:00"})
        self.assertEqual(incident.notification_due_date, date(2026, 12, 10))

    @freeze_time("2026-12-08 12:00:00")
    def test_notice_artifacts_keep_asp_and_taxpayer_evidence_separate(self):
        manager = new_test_user(
            self.env,
            login="synthetic-incident-notice-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        reader = new_test_user(
            self.env,
            login="synthetic-incident-notice-reader",
            groups="account.group_account_readonly",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        incident = (
            self.env["l10n_ae.edi.incident"]
            .with_user(manager)
            .create(
                {
                    "name": "Synthetic incident notice evidence",
                    "company_id": self.env.company.id,
                    "affected_roles": "both",
                    "occurrence_at": "2026-11-30 21:30:00",
                    "detected_at": "2026-12-01 08:00:00",
                    "reason": "Synthetic electronic issue and receipt outage",
                    "responsible_id": manager.id,
                    "calendar_start": "2026-12-01",
                    "calendar_end": "2026-12-10",
                    "calendar_holidays": ["2026-12-02", "2026-12-03"],
                    "calendar_source": "Reviewed synthetic federal holidays",
                    "calendar_version": "synthetic-federal-2026",
                    "source_reference": "MD243 Art12; reviewed Art49 day exclusion",
                    "review_note": "Dubai Dec1 anchor; Dec4 day1, Dec7 day2",
                }
            )
        )
        incident.action_open()
        basis = incident.read(
            [
                "state",
                "notification_due_date",
                "opened_by_id",
                "opened_at",
                "write_date",
            ]
        )
        activity_ids = incident.activity_ids.ids
        asp_content = b"Controlled ASP service interruption notice\n"
        asp_digest = "62a1082c4b3b708df91fd7185e220e2cf7efa52ae97e549e9bc29d9ce63e6d8d"
        asp_evidence = {
            "evidence_key": "synthetic-asp-fta-1",
            "evidence_origin": "simulated",
            "sender_role": "asp",
            "sender_name": "Controlled ASP",
            "recipient_role": "fta",
            "recipient_name": "Controlled FTA recipient",
            "recipient_address": "fta-recipient@example.invalid",
            "channel": "email",
            "sent_at": "2026-12-04 08:00:00",
            "external_reference": "synthetic-asp-notice-1",
            "review_note": "Controlled artifact; not proof of taxpayer duty discharge",
            "document_filename": "synthetic-asp-notice.txt",
            "document": base64.b64encode(asp_content).decode(),
        }
        for elevated in (False, True):
            with (
                self.subTest(elevated=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                incident.with_user(reader).sudo(elevated).record_notice(asp_evidence)
        asp_result = incident.record_notice(asp_evidence)
        asp_notice = (
            self.env["l10n_ae.edi.incident.notice"]
            .with_user(manager)
            .browse(asp_result["id"])
        )
        self.assertEqual(asp_result["document_sha256"], asp_digest)
        self.assertEqual(asp_notice.document.content, asp_content)
        self.assertEqual(asp_notice.sender_role, "asp")
        self.assertEqual(asp_notice.notice_purpose, "asp_service_disruption")
        self.assertFalse(asp_notice.taxpayer_roles)
        self.assertEqual(asp_notice.evidence_origin, "simulated")
        self.assertEqual(asp_notice.recorded_by_id, manager)
        self.assertEqual(
            asp_notice.recorded_at, fields.Datetime.to_datetime("2026-12-08 12:00:00")
        )
        self.assertEqual(incident.record_notice(asp_evidence), asp_result)
        with self.cr.savepoint(), self.assertRaises(UserError):
            incident.record_notice({**asp_evidence, "external_reference": "Changed"})
        taxpayer_content = b"Controlled taxpayer issuer and recipient notice\n"
        taxpayer_digest = (
            "0f31a00f2ba51212cd9edf0a806bacae1b466cc13b2bf2fbb3bb71e2d3b07e49"
        )
        taxpayer_evidence = {
            **asp_evidence,
            "evidence_key": "synthetic-taxpayer-fta-1",
            "sender_role": "taxpayer",
            "sender_name": "Controlled UAE taxpayer",
            "taxpayer_roles": "both",
            "sent_at": "2026-12-07 10:00:00",
            "external_reference": "synthetic-taxpayer-notice-1",
            "review_note": "Controlled artifact reviewed for both taxpayer roles",
            "document_filename": "synthetic-taxpayer-notice.txt",
            "document": base64.b64encode(taxpayer_content).decode(),
        }
        taxpayer_result = incident.record_notice(taxpayer_evidence)
        taxpayer_notice = asp_notice.browse(taxpayer_result["id"])
        self.assertNotEqual(asp_notice, taxpayer_notice)
        self.assertEqual(taxpayer_notice.document.content, taxpayer_content)
        self.assertEqual(taxpayer_notice.document_sha256, taxpayer_digest)
        self.assertEqual(taxpayer_notice.sender_role, "taxpayer")
        self.assertEqual(taxpayer_notice.notice_purpose, "taxpayer_system_failure")
        self.assertEqual(taxpayer_notice.taxpayer_roles, "both")
        self.assertEqual(incident.record_notice(taxpayer_evidence), taxpayer_result)
        self.assertEqual(
            set(incident.notice_ids.ids), {asp_notice.id, taxpayer_notice.id}
        )
        for notice in (asp_notice, taxpayer_notice):
            self.assertEqual(notice.company_id, incident.company_id)
            self.assertEqual(
                notice.with_user(reader).document.content, notice.document.content
            )
            with self.cr.savepoint(), self.assertRaises(UserError):
                notice.write({"document_filename": "changed.txt"})
            with self.cr.savepoint(), self.assertRaises(UserError):
                notice.unlink()
        with self.cr.savepoint(), self.assertRaises(UserError):
            self.env["l10n_ae.edi.incident.notice"].with_user(manager).create(
                {"incident_id": incident.id, "document": taxpayer_evidence["document"]}
            )
        self.assertEqual(
            incident.read(
                [
                    "state",
                    "notification_due_date",
                    "opened_by_id",
                    "opened_at",
                    "write_date",
                ]
            ),
            basis,
        )
        self.assertEqual(incident.activity_ids.ids, activity_ids)

    @freeze_time("2026-12-01 12:00:00")
    def test_manager_opens_failure_with_reviewed_calendar_and_one_internal_alert(self):
        self.assertIn("l10n_ae.edi.incident", self.env.registry)
        manager = new_test_user(
            self.env,
            login="synthetic-incident-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        reader = new_test_user(
            self.env,
            login="synthetic-incident-reader",
            groups="account.group_account_readonly",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.action_post()
        ledger = invoice.line_ids.sorted("id").read(
            ["debit", "credit", "balance", "amount_currency", "write_date"]
        )
        move_before = invoice.read(["state", "amount_total", "write_date"])
        models = (
            "l10n_ae.edi.transmission",
            "l10n_ae.edi.attempt",
            "l10n_ae.edi.event",
        )
        baseline = {
            model: self.env[model].search([], order="id").ids for model in models
        }
        incident = (
            self.env["l10n_ae.edi.incident"]
            .with_user(manager)
            .with_context(
                default_state="open", default_notification_due_date="2000-01-01"
            )
            .create(
                {
                    "name": "Synthetic system outage over reviewed federal holidays",
                    "company_id": self.env.company.id,
                    "affected_roles": "both",
                    "occurrence_at": "2026-11-30 21:30:00",
                    "detected_at": "2026-12-01 08:00:00",
                    "reason": "Synthetic outage prevents electronic issue and receipt",
                    "responsible_id": manager.id,
                    "calendar_start": "2026-12-01",
                    "calendar_end": "2026-12-10",
                    "calendar_holidays": ["2026-12-02", "2026-12-03"],
                    "calendar_source": "Reviewed synthetic federal holidays",
                    "calendar_version": "synthetic-federal-2026",
                    "source_reference": "MD243 Art12; reviewed Art49 day exclusion",
                    "review_note": "Dubai anchor Dec1; Dec4 day1, Dec7 day2",
                }
            )
        )
        self.assertEqual(incident.state, "draft")
        self.assertFalse(incident.notification_due_date)
        for elevated in (False, True):
            with (
                self.subTest(elevated=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                incident.with_user(reader).sudo(elevated).action_open()
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.state, "open")
        self.assertEqual(incident.notification_due_date, date(2026, 12, 7))
        self.assertEqual(incident.timezone, "Asia/Dubai")
        self.assertEqual(incident.affected_roles, "both")
        self.assertEqual(incident.opened_by_id, manager)
        self.assertTrue(incident.opened_at)
        activities = incident.activity_ids.sorted("id")
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities.user_id, manager)
        self.assertEqual(activities.date_deadline, date(2026, 12, 7))
        self.assertEqual(activities.summary, "Review UAE system failure notification")
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.activity_ids.ids, activities.ids)
        for values in (
            {"occurrence_at": "2026-12-01 08:00:00"},
            {"calendar_holidays": []},
            {"notification_due_date": "2026-12-04"},
            {"opened_by_id": reader.id},
        ):
            with (
                self.subTest(values=values),
                self.cr.savepoint(),
                self.assertRaises(UserError),
            ):
                incident.write(values)
        self.assertEqual(
            invoice.read(["state", "amount_total", "write_date"]), move_before
        )
        self.assertEqual(
            invoice.line_ids.sorted("id").read(
                ["debit", "credit", "balance", "amount_currency", "write_date"]
            ),
            ledger,
        )
        self.assertEqual(
            {model: self.env[model].search([], order="id").ids for model in models},
            baseline,
        )
