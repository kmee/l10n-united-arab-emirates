# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from datetime import date

from freezegun import freeze_time

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaeIncident(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()

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
