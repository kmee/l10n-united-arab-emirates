# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from datetime import date

from freezegun import freeze_time

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaeDeadline(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()

    def reviewed_deadline(self, invoice, **extra):
        return self.env["l10n_ae.edi.deadline"].create(
            {
                "name": "Synthetic reviewed security example",
                "move_id": invoice.id,
                "registration_status": "vat_registered",
                "rule_basis": "vat",
                "anchor_kind": "supply",
                "anchor_date": "2026-09-02",
                "due_date": "2026-09-16",
                "reminder_date": "2026-09-13",
                "calendar_basis": "Synthetic explicit calendar days",
                "source_reference": "Synthetic source, not production legal policy",
                "policy_version": "synthetic-security-1",
                "review_note": "Explicit independent dates for guard regression",
                "responsible_id": self.env.uid,
                "requested_fact": "confirmations",
                **extra,
            }
        )

    def test_deadline_review_uses_fiscal_country_not_company_address(self):
        ae = self.env.ref("base.ae")
        fr = self.env.ref("base.fr")
        company_a = self.env.company
        company_a.write(
            {"country_id": fr.id, "account_fiscal_country_id": ae.id, "state_id": False}
        )
        data_b = self.setup_other_company(name="Synthetic foreign fiscal deadline")
        company_b = data_b["company"]
        company_b.write({"country_id": ae.id, "account_fiscal_country_id": fr.id})
        self.simple_accountman.company_ids = [Command.set([company_a.id, company_b.id])]
        for data, address, fiscal, eligible in (
            (self.company_data, "FR", "AE", True),
            (data_b, "AE", "FR", False),
        ):
            with self.subTest(address=address, fiscal=fiscal):
                company = data["company"]
                moves = (
                    self.env["account.move"]
                    .with_user(self.simple_accountman)
                    .with_company(company)
                    .with_context(allowed_company_ids=[company.id])
                )
                invoice = moves.create(
                    {
                        "company_id": company.id,
                        "move_type": "out_invoice",
                        "partner_id": self.partner_a.id,
                        "invoice_date": "2026-10-01",
                        "journal_id": data["default_journal_sale"].id,
                        "invoice_line_ids": [
                            Command.create(
                                {
                                    "name": "Synthetic untaxed deadline service",
                                    "price_unit": 100,
                                    "account_id": data["default_account_revenue"].id,
                                    "tax_ids": [Command.clear()],
                                }
                            )
                        ],
                    }
                )
                self.assertFalse(invoice.env.su)
                self.assertEqual(company.country_code, address)
                self.assertEqual(company.account_fiscal_country_id.code, fiscal)
                self.assertEqual(invoice.country_code, fiscal)
                invoice.action_post()
                invoice.env.flush_all()
                ledger = invoice.line_ids.read(
                    ["balance", "amount_currency", "account_id", "write_date"]
                )
                deadlines = invoice.env["l10n_ae.edi.deadline"]
                values = {
                    "name": "Synthetic fiscal-country reviewed deadline",
                    "move_id": invoice.id,
                    "registration_status": "vat_registered",
                    "rule_basis": "vat",
                    "anchor_kind": "supply",
                    "anchor_date": "2026-09-02",
                    "due_date": "2026-09-16",
                    "reminder_date": "2026-09-13",
                    "calendar_basis": "Synthetic explicit calendar dates",
                    "source_reference": "Synthetic fixture, not a legal policy",
                    "policy_version": "synthetic-fiscal-country-1",
                    "review_note": "Supply date remains independent of invoice date",
                    "responsible_id": self.simple_accountman.id,
                    "requested_fact": "confirmations",
                }
                if eligible:
                    deadline = deadlines.create(values)
                    self.assertEqual(deadline.state, "draft")
                    self.assertEqual(deadline.company_id, company)
                    self.assertEqual(deadline.move_id, invoice)
                    self.assertTrue(deadline.action_approve())
                    self.assertEqual(deadline.approved_by_id, self.simple_accountman)
                    status = deadline.get_deadline_status("2026-09-17")
                    self.assertEqual(status["state"], "overdue")
                    self.assertEqual(status["due_date"], "2026-09-16")
                    self.assertFalse(status["observed"])
                    action = deadline.action_assess_deadline("2026-09-17")
                    self.assertEqual(action["tag"], "display_notification")
                    self.assertFalse(deadline.activity_ids)
                else:
                    with (
                        self.cr.savepoint(),
                        self.assertRaisesRegex(
                            ValidationError, "Select a UAE accounting invoice"
                        ),
                    ):
                        deadlines.create(values)
                    self.assertFalse(
                        deadlines.search_count([("move_id", "=", invoice.id)])
                    )
                self.assertEqual((invoice.state, invoice.amount_total), ("posted", 100))
                self.assertEqual(
                    invoice.line_ids.read(
                        ["balance", "amount_currency", "account_id", "write_date"]
                    ),
                    ledger,
                )
                self.assertFalse(invoice.l10n_ae_transmission_ids)

    def test_readonly_and_inactive_actors_cannot_elevate_deadline_mutations(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        deadline = self.reviewed_deadline(invoice)
        reader = new_test_user(
            self.env,
            login="synthetic-deadline-reader",
            groups="account.group_account_readonly",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        view = deadline.with_user(reader)
        self.assertEqual(view.get_deadline_status("2026-09-17")["state"], "review")
        for elevated in (False, True):
            actor = view.sudo(elevated)
            with self.subTest(elevated=elevated), self.cr.savepoint():
                with self.assertRaises(AccessError):
                    actor.action_approve()
                with self.assertRaises(AccessError):
                    actor.action_schedule_reminder()
                with self.assertRaises(AccessError):
                    actor.write({"review_note": "Unauthorized changed review"})
                with self.assertRaises(AccessError):
                    actor.unlink()
        reader.active = False
        with self.assertRaises(AccessError):
            view.sudo().get_deadline_status("2026-09-17")
        self.assertEqual(deadline.state, "draft")
        self.assertFalse(deadline.approved_at)
        self.assertFalse(deadline.activity_ids)

    def test_defaults_and_direct_writes_cannot_forge_deadline_history(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        now = fields.Datetime.now()
        defaults = {
            "default_state": "approved",
            "default_approved_by_id": self.env.uid,
            "default_approved_at": "2000-01-01 00:00:00",
            "default_alerted_by_id": self.env.uid,
            "default_alerted_at": "2000-01-01 00:00:00",
            "default_next_check_at": "2099-01-01 00:00:00",
        }
        deadline = (
            self.env["l10n_ae.edi.deadline"]
            .with_context(**defaults)
            .create(
                {"name": "Synthetic forged defaults refused", "move_id": invoice.id}
            )
        )
        self.assertEqual(deadline.state, "draft")
        self.assertFalse(deadline.approved_at)
        self.assertFalse(deadline.approved_by_id)
        self.assertFalse(deadline.alerted_at)
        self.assertFalse(deadline.alerted_by_id)
        self.assertGreaterEqual(deadline.next_check_at, now)
        self.assertLess(
            deadline.next_check_at, fields.Datetime.to_datetime("2099-01-01")
        )
        for values in (
            {"state": "approved"},
            {"approved_at": "2000-01-01 00:00:00"},
            {"alerted_at": "2000-01-01 00:00:00"},
            {"next_check_at": "2099-01-01 00:00:00"},
        ):
            with self.subTest(values=values), self.cr.savepoint():
                with self.assertRaises(UserError):
                    deadline.write(values)
                with self.assertRaises(UserError):
                    self.reviewed_deadline(invoice, **values)
        reviewed = self.reviewed_deadline(invoice)
        reviewed.action_approve()
        actor, when = reviewed.approved_by_id, reviewed.approved_at
        with self.assertRaises(UserError):
            reviewed.write({"due_date": "2027-01-01"})
        with self.assertRaises(UserError):
            reviewed.unlink()
        self.assertEqual((reviewed.approved_by_id, reviewed.approved_at), (actor, when))

    def test_review_rejects_incomplete_and_inconsistent_basis_without_approval(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        for values in (
            {"requested_fact": False},
            {"source_reference": "   "},
            {"calendar_basis": ""},
            {"due_date": "2026-09-01"},
            {"reminder_date": "2026-09-17"},
            {"rule_basis": "residual"},
        ):
            with self.subTest(values=values), self.cr.savepoint():
                deadline = self.reviewed_deadline(invoice, **values)
                with self.assertRaises(ValidationError):
                    deadline.action_approve()
                self.assertEqual(deadline.state, "draft")
                self.assertFalse(deadline.approved_at)
        self.assertFalse(invoice.l10n_ae_transmission_ids)

    def test_reminder_rechecks_current_owner_and_neutralized_copy(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        owner = new_test_user(
            self.env,
            login="synthetic-deadline-owner",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        deadline = self.reviewed_deadline(invoice, responsible_id=owner.id)
        deadline.action_approve()
        owner.active = False
        with self.assertRaises(ValidationError):
            deadline.action_schedule_reminder()
        self.assertFalse(deadline.activity_ids)
        self.assertFalse(deadline.alerted_at)
        owner.active = True
        self.env["ir.config_parameter"].sudo().set_bool("database.is_neutralized", True)
        with self.assertRaises(UserError):
            deadline.action_schedule_reminder()
        self.assertFalse(deadline.activity_ids)
        self.assertFalse(deadline.alerted_at)

    def test_manager_reviews_deadline_before_fiscal_preparation(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.invoice_date = date(2026, 10, 1)
        invoice.action_post()
        with Form(
            self.env["l10n_ae.edi.deadline"],
            view="l10n_ae_edi.edi_deadline_form",
        ) as form:
            form.name = "Synthetic reviewed deadline"
            form.move_id = invoice
            form.registration_status = "vat_registered"
            form.rule_basis = "vat"
            form.anchor_kind = "supply"
            form.anchor_date = date(2026, 9, 2)
            form.due_date = date(2026, 9, 16)
            form.reminder_date = date(2026, 9, 13)
            form.calendar_basis = "Synthetic calendar-day example, not ratified"
            form.source_reference = "Synthetic reviewed rule with explicit supply date"
            form.policy_version = "synthetic-deadline-1"
            form.review_note = "Invoice date does not replace the reviewed supply date"
            form.responsible_id = self.env.user
            form.requested_fact = "confirmations"
        deadline = form.record
        self.assertEqual(deadline.state, "draft")
        self.assertEqual(deadline.get_deadline_status("2026-09-17")["state"], "review")
        deadline.action_approve()
        self.assertEqual(deadline.approved_by_id, self.env.user)
        self.assertTrue(deadline.approved_at)
        self.assertEqual(deadline.company_id, invoice.company_id)
        self.assertEqual(deadline.timezone, "Asia/Dubai")
        for day, expected in [
            ("2026-09-10", "pending"),
            ("2026-09-13", "due_soon"),
            ("2026-09-16", "due_today"),
            ("2026-09-17", "overdue"),
        ]:
            status = deadline.get_deadline_status(day)
            self.assertEqual(status["state"], expected)
            self.assertFalse(status["observed"])
            self.assertEqual(status["due_date"], "2026-09-16")
        self.assertEqual(invoice.invoice_date, date(2026, 10, 1))
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.amount_total, 105)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(deadline.activity_ids)

    def test_reviewed_summary_vat_deadline_precedes_residual_clock(
        self,
    ):
        # Independent worked dates: docs/research/vat-summary-deadlines.md.
        # These are reviewer-entered dates, not an automatic legal calculator.
        manager = self.simple_accountman
        self.assertFalse(manager.has_group("base.group_system"))
        self.assertEqual(manager.company_ids, self.env.company)
        moves = self.env["account.move"].with_user(manager)
        payments = self.env["account.payment"].with_user(manager)
        deadlines = self.env["l10n_ae.edi.deadline"].with_user(manager)
        bank = self.company_data["default_journal_bank"]
        payment_method = self.inbound_payment_method_line
        for summary, issue_day, payment_day, anchor, due, reminder, amount in (
            (
                True,
                "2026-10-01",
                "2026-10-05",
                "2026-09-30",
                "2026-10-14",
                "2026-10-12",
                210,
            ),
            (
                False,
                "2026-09-03",
                "2026-09-12",
                "2026-09-02",
                "2026-09-16",
                "2026-09-14",
                105,
            ),
        ):
            with self.subTest(summary=summary):
                supply_days = (
                    ("2026-09-02", "2026-09-30") if summary else ("2026-09-02",)
                )
                invoice = moves.create(
                    {
                        "move_type": "out_invoice",
                        "partner_id": self.partner_a.id,
                        "invoice_date": issue_day,
                        "taxable_supply_date": anchor,
                        "l10n_ae_summary": summary,
                        "l10n_ae_invoicing_period_start": "2026-09-01"
                        if summary
                        else False,
                        "l10n_ae_invoicing_period_end": "2026-09-30"
                        if summary
                        else False,
                        "invoice_line_ids": [
                            Command.create(
                                {
                                    "name": f"Synthetic service completed {day}",
                                    "quantity": 1,
                                    "price_unit": 100,
                                    "account_id": self.company_data[
                                        "default_account_revenue"
                                    ].id,
                                    "tax_ids": [Command.set(self.tax_sale_a.ids)],
                                }
                            )
                            for day in supply_days
                        ],
                    }
                )
                invoice.action_post()
                # A real native payment records the distinct, later receipt date.
                register = (
                    self.env["account.payment.register"]
                    .with_user(manager)
                    .with_context(active_model="account.move", active_ids=invoice.ids)
                    .create(
                        {
                            "payment_date": payment_day,
                            "amount": amount,
                            "journal_id": bank.id,
                            "payment_method_line_id": payment_method.id,
                        }
                    )
                )
                payment_action = register.action_create_payments()
                self.assertEqual(payment_action["res_model"], "account.payment")
                payment = payments.browse(payment_action["res_id"])
                self.assertEqual(payment.invoice_ids, invoice)
                self.assertNotEqual(payment.state, "draft")
                self.assertEqual(payment.date, fields.Date.to_date(payment_day))
                self.assertGreater(
                    payment.date, max(map(fields.Date.to_date, supply_days))
                )
                self.assertEqual(invoice.amount_total, amount)
                self.assertEqual(
                    invoice.taxable_supply_date, fields.Date.to_date(anchor)
                )
                self.assertEqual(invoice.l10n_ae_summary, summary)
                invoice.env.flush_all()
                ledger = invoice.line_ids.read(
                    ["balance", "amount_currency", "account_id", "write_date"]
                )
                with Form(deadlines, view="l10n_ae_edi.edi_deadline_form") as form:
                    form.name = (
                        "Synthetic September summary"
                        if summary
                        else "Synthetic single supply"
                    )
                    form.move_id = invoice
                    form.registration_status = "vat_registered"
                    form.rule_basis = "residual"
                    form.anchor_kind = "business_transaction"
                    form.anchor_date = date(2026, 9, 2)
                    form.due_date = date(2026, 9, 16)
                    form.reminder_date = date(2026, 9, 14)
                    form.calendar_basis = "Calendar days; no business-day adjustment"
                    form.source_reference = (
                        "MD243/2025 art6(4)-(5); VAT art25/67; ER art59(6)/(13)(b)/(16)"
                        if summary
                        else "MD243/2025 art6(4)-(5); VAT art25/67(1)"
                    )
                    form.policy_version = "reviewed-example-2026-10-19"
                    form.review_note = (
                        "Same recipient; services completed 2026-09-02 and 2026-09-30; "
                        "payment 2026-10-05 after both services; no earlier invoice, "
                        "advance, periodic contract or separate invoices. Month-end "
                        "summary exception reviewed; not a production legal approval."
                        if summary
                        else "Service completed 2026-09-02; payment 2026-09-12; "
                        "no advance or periodic contract; separate full tax invoice. "
                        "Not eligible for the monthly summary deadline."
                    )
                    form.responsible_id = manager
                    form.requested_fact = "confirmations"
                deadline = form.record
                # The residual clock is not an approvable basis for this registrant,
                # even though its dates satisfy the generic chronological checks.
                with (
                    self.cr.savepoint(),
                    self.assertRaisesRegex(
                        ValidationError, "consistent with the declared VAT registration"
                    ),
                ):
                    deadline.action_approve()
                self.assertEqual(deadline.state, "draft")
                self.assertFalse(deadline.approved_at)
                self.assertFalse(deadline.activity_ids)
                with Form(deadline, view="l10n_ae_edi.edi_deadline_form") as form:
                    form.rule_basis = "vat"
                    form.anchor_kind = "other" if summary else "supply"
                    form.anchor_date = fields.Date.to_date(anchor)
                    form.due_date = fields.Date.to_date(due)
                    form.reminder_date = fields.Date.to_date(reminder)
                deadline.action_approve()
                self.assertEqual(deadline.approved_by_id, manager)
                self.assertEqual(deadline.rule_basis, "vat")
                self.assertEqual(deadline.anchor_date, fields.Date.to_date(anchor))
                self.assertEqual(deadline.timezone, "Asia/Dubai")
                self.assertNotEqual(deadline.due_date, invoice.invoice_date)
                for day, expected in (
                    (reminder, "due_soon"),
                    (due, "due_today"),
                    ("2026-10-15" if summary else "2026-09-17", "overdue"),
                ):
                    status = deadline.get_deadline_status(day)
                    self.assertEqual(status["state"], expected)
                    self.assertEqual(status["due_date"], due)
                    self.assertFalse(status["observed"])
                    self.assertEqual(status["timeliness"], "not_assessed")
                with freeze_time(f"{reminder} 08:00:00"):
                    deadline.action_schedule_reminder()
                    deadline.action_schedule_reminder()
                self.assertEqual(len(deadline.activity_ids), 1)
                self.assertEqual(deadline.activity_ids.user_id, manager)
                self.assertEqual(
                    deadline.activity_ids.date_deadline, fields.Date.to_date(due)
                )
                self.assertEqual(invoice.state, "posted")
                self.assertEqual(invoice.amount_total, amount)
                self.assertEqual(
                    invoice.line_ids.read(
                        ["balance", "amount_currency", "account_id", "write_date"]
                    ),
                    ledger,
                )
                self.assertFalse(invoice.l10n_ae_transmission_ids)

    def test_transferred_draft_requires_review_before_deadline_assessment(self):
        company_a = self.env.company
        data_b = self.setup_other_company(name="Synthetic deadline second company")
        company_b = data_b["company"]
        company_b.country_id = self.env.ref("base.ae")
        context = {"allowed_company_ids": [company_a.id, company_b.id]}
        invoice = (
            self.env["account.move"]
            .with_context(**context)
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "journal_id": self.company_data["default_journal_sale"].id,
                }
            )
        )
        deadline = (
            self.env["l10n_ae.edi.deadline"]
            .with_context(**context)
            .create(
                {
                    "name": "Synthetic deadline before company transfer",
                    "move_id": invoice.id,
                }
            )
        )
        invoice.write(
            {
                "journal_id": data_b["default_journal_sale"].id,
                "company_id": company_b.id,
            }
        )
        self.assertEqual(invoice.company_id, company_b)
        self.assertEqual(deadline.company_id, company_a)
        with self.assertRaises(ValidationError):
            deadline.get_deadline_status("2026-09-17")
        self.assertFalse(deadline.activity_ids)
        self.assertFalse(invoice.l10n_ae_transmission_ids)

    @freeze_time("2026-09-12 19:59:59")
    def test_reviewed_reminder_has_one_native_activity_without_preparing_invoice(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        deadline = self.env["l10n_ae.edi.deadline"].create(
            {
                "name": "Synthetic internal deadline reminder",
                "move_id": invoice.id,
                "registration_status": "vat_registered",
                "rule_basis": "vat",
                "anchor_kind": "supply",
                "anchor_date": "2026-09-02",
                "due_date": "2026-09-16",
                "reminder_date": "2026-09-13",
                "calendar_basis": "Synthetic reviewed calendar-day interval",
                "source_reference": "Synthetic source, not a production legal policy",
                "policy_version": "synthetic-reminder-1",
                "review_note": "No automatic dispatch or fiscal notification",
                "responsible_id": self.env.uid,
                "requested_fact": "confirmations",
            }
        )
        deadline.action_approve()
        self.assertFalse(
            deadline.with_context(tz="America/Sao_Paulo").action_schedule_reminder()
        )
        self.assertFalse(deadline.activity_ids)
        with freeze_time("2026-09-12 20:00:00"):
            self.assertTrue(
                deadline.with_context(tz="America/Sao_Paulo").action_schedule_reminder()
            )
        activity = deadline.activity_ids
        self.assertEqual(len(activity), 1)
        self.assertEqual(
            activity.activity_type_id, self.env.ref("mail.mail_activity_data_todo")
        )
        self.assertEqual(activity.user_id, self.env.user)
        self.assertEqual(activity.date_deadline, date(2026, 9, 16))
        self.assertTrue(deadline.alerted_at)
        self.assertEqual(deadline.alerted_by_id, self.env.user)
        with freeze_time("2026-09-17 08:00:00"):
            self.assertFalse(deadline.action_schedule_reminder())
        self.assertEqual(deadline.activity_ids, activity)
        activity.action_feedback(feedback="Synthetic responsible review recorded")
        self.assertFalse(deadline.activity_ids)
        with freeze_time("2026-09-18 08:00:00"):
            self.assertFalse(deadline.action_schedule_reminder())
        self.assertTrue(deadline.alerted_at)
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.amount_total, 105)
        self.assertFalse(invoice.l10n_ae_transmission_ids)

    @freeze_time("2026-09-01 08:00:00")
    def test_future_assessment_date_cannot_create_a_reminder(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        deadline = self.env["l10n_ae.edi.deadline"].create(
            {
                "name": "Synthetic future deadline",
                "move_id": invoice.id,
                "registration_status": "vat_registered",
                "rule_basis": "vat",
                "anchor_kind": "supply",
                "anchor_date": "2026-09-02",
                "due_date": "2026-09-16",
                "reminder_date": "2026-09-13",
                "calendar_basis": "Synthetic explicit calendar days",
                "source_reference": "Synthetic review, not a production legal policy",
                "policy_version": "synthetic-clock-1",
                "review_note": "A future assessment is not an activity clock",
                "responsible_id": self.env.uid,
                "requested_fact": "confirmations",
            }
        )
        deadline.action_approve()
        self.assertEqual(deadline.get_deadline_status("2026-09-17")["state"], "overdue")
        with self.assertRaises(UserError):
            deadline.action_schedule_reminder("2026-09-17")
        self.assertFalse(deadline.action_schedule_reminder())
        self.assertFalse(deadline.activity_ids)
        self.assertFalse(deadline.alerted_at)
        self.assertFalse(invoice.l10n_ae_transmission_ids)

    def test_native_assessment_action_does_not_create_approval_or_activity(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        deadline = self.env["l10n_ae.edi.deadline"].create(
            {"name": "Synthetic unreviewed UI assessment", "move_id": invoice.id}
        )
        action = deadline.action_assess_deadline("2026-09-17")
        self.assertEqual(action["type"], "ir.actions.client")
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["title"], "UAE Document Deadline Assessment")
        self.assertIn("Requires Review", action["params"]["message"])
        self.assertIn("2026-09-17", action["params"]["message"])
        self.assertEqual(deadline.state, "draft")
        self.assertFalse(deadline.approved_at)
        self.assertFalse(deadline.activity_ids)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
