# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json

from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_ae_edi.tests.common import (
    CONTROLLED_CAPABILITIES,
    review_controlled_binding,
)


@tagged("post_install", "-at_install")
class TestUaeInvoicePreparation(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        cls.partner_a.write(
            {
                "country_id": cls.env.ref("base.ae").id,
                "state_id": cls.env.ref("base.state_ae_du").id,
            }
        )
        cls.env.company.partner_id.state_id = cls.env.ref("base.state_ae_du")
        if "routing_scheme" in cls.env["res.partner"]._fields:
            cls.env.company.partner_id.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000001"}
            )
            cls.partner_a.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000002"}
            )
        cls.binding = cls.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic sandbox ASP"}
        )
        review_controlled_binding(
            cls.binding,
            cls.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for invoice preparation tests.",
        )

    def test_unsupported_contract_document_keeps_native_post_until_reviewed(self):
        binding = (
            self.env["l10n_ae.edi.binding"]
            .create({"name": "Synthetic unreviewed unsupported contract"})
            .with_user(self.simple_accountman)
        )
        capabilities = binding.env["l10n_ae.edi.capability"].search(
            [("code", "in", ["submit", "lookup", "report", "billing_credit"])]
        )
        self.assertEqual(len(capabilities), 4)
        with Form(binding) as form:
            form.fiscal_uuid_policy = "controlled_local"
            form.capability_profile = "kmee-controlled-1"
            form.capability_review_note = (
                "Only billing credits contracted in this test."
            )
            for capability in capabilities:
                form.capability_ids.add(capability)
        binding.action_review_capabilities()
        self.env.company.l10n_ae_edi_auto_prepare = True
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.l10n_ae_binding_id = binding
        actor = invoice.with_user(self.simple_accountman)
        self.assertFalse(actor.env.su)
        actor.action_post()
        self.assertEqual(actor.state, "posted")
        self.assertEqual(actor.amount_total, 105)
        self.assertEqual(actor.l10n_ae_preparation_state, "review")
        self.assertIn("billing_invoice", actor.l10n_ae_preparation_error)
        self.assertFalse(actor.l10n_ae_transmission_ids)
        self.assertFalse(actor.l10n_ae_fiscal_uuid)
        ledger_fields = [
            "balance",
            "amount_currency",
            "account_id",
            "tax_ids",
            "write_uid",
            "write_date",
        ]
        ledger = actor.line_ids.read(ledger_fields)
        with (
            self.assertRaisesRegex(ValidationError, "billing_invoice"),
            self.cr.savepoint(),
        ):
            actor.action_l10n_ae_prepare()
        self.assertFalse(actor.l10n_ae_transmission_ids)
        self.assertFalse(actor.l10n_ae_fiscal_uuid)
        with Form(binding) as form:
            form.capability_ids.add(
                binding.env.ref("l10n_ae_edi.capability_billing_invoice")
            )
        binding.action_review_capabilities()
        actor.action_l10n_ae_prepare()
        self.assertEqual(actor.l10n_ae_preparation_state, "ready")
        self.assertTrue(actor.l10n_ae_fiscal_uuid)
        self.assertEqual(len(actor.l10n_ae_transmission_ids), 1)
        self.assertEqual(actor.line_ids.read(ledger_fields), ledger)

    def test_native_batch_frequency_defaults_preserve_explicit_values_and_clears(self):
        term = self.env["account.payment.term"].create(
            {
                "name": "Synthetic monthly term for API defaults",
                "company_id": self.env.company.id,
                "l10n_ae_billing_frequency_code": "MTH",
                "line_ids": [Command.create({"value": "percent", "value_amount": 100})],
            }
        )
        manager_moves = self.env["account.move"].with_user(self.simple_accountman)
        self.assertFalse(manager_moves.env.su)
        purchase_journal = self.company_data["default_journal_purchase"]
        purchase_journal.is_self_billing = True
        rows = []
        expected = ["MTH", "WKY", False, False] * 4
        for move_type in ("out_invoice", "out_refund", "in_invoice", "in_refund"):
            for explicit in (
                {},
                {"l10n_ae_billing_frequency_code": "WKY"},
                {"l10n_ae_billing_frequency_code": False},
                {"l10n_ae_billing_frequency_code": None},
            ):
                rows.append(
                    {
                        "move_type": move_type,
                        "partner_id": self.partner_a.id,
                        "invoice_payment_term_id": term.id,
                        "journal_id": (
                            purchase_journal.id
                            if move_type.startswith("in_")
                            else self.company_data["default_journal_sale"].id
                        ),
                        **explicit,
                    }
                )
        batch = manager_moves.create(rows)
        self.assertEqual(
            [move.l10n_ae_billing_frequency_code for move in batch], expected
        )
        self.assertTrue(
            all(
                move.is_self_billing
                for move in batch
                if move.move_type.startswith("in_")
            )
        )
        self.assertFalse(batch.mapped("l10n_ae_transmission_ids"))
        self.assertFalse(any(move.l10n_ae_invoicing_period_start for move in batch))
        self.assertFalse(any(move.l10n_ae_invoicing_period_end for move in batch))
        for default in ("WKY", False, None):
            move = manager_moves.with_context(
                default_move_type="out_invoice",
                default_invoice_payment_term_id=term.id,
                default_l10n_ae_billing_frequency_code=default,
            ).create({"partner_id": self.partner_a.id})
            self.assertEqual(move.l10n_ae_billing_frequency_code, default or False)
        for move_type in ("out_receipt", "in_receipt", "entry"):
            move = manager_moves.create(
                {
                    "move_type": move_type,
                    "partner_id": self.partner_a.id,
                    "invoice_payment_term_id": term.id,
                }
            )
            self.assertFalse(move.l10n_ae_billing_frequency_code)
        self.partner_a.property_payment_term_id = term
        inherited = manager_moves.with_context(default_move_type="out_invoice").create(
            {"partner_id": self.partner_a.id}
        )
        self.assertEqual(inherited.invoice_payment_term_id, term)
        self.assertEqual(inherited.l10n_ae_billing_frequency_code, "MTH")
        term.l10n_ae_billing_frequency_code = "DLY"
        self.assertEqual(
            [move.l10n_ae_billing_frequency_code for move in batch], expected
        )
        self.assertEqual(inherited.l10n_ae_billing_frequency_code, "MTH")

    def test_frozen_issue_preserves_contract_review_after_binding_changes(self):
        binding = self.binding.with_user(self.simple_accountman)
        binding.fiscal_uuid_policy = "controlled_local"
        review_controlled_binding(
            binding,
            self.simple_accountman,
            capabilities=("submit", "lookup", "report", "billing_invoice"),
            note="Original standard-invoice simulation contract.",
        )
        reviewed_at = binding.capability_reviewed_at
        reviewed_hash = binding.capability_review_hash
        actor = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "invoice_date": "2019-01-01",
                    "l10n_ae_binding_id": binding.id,
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Synthetic reviewed-contract service",
                                "quantity": 1,
                                "price_unit": 100,
                                "account_id": self.company_data[
                                    "default_account_revenue"
                                ].id,
                                "tax_ids": [Command.set(self.tax_sale_a.ids)],
                            }
                        )
                    ],
                }
            )
        )
        actor.action_post()
        # Observe durable native posting before UAE actions. A mixed-user helper's
        # cached write_uid can differ from the ORM's later flushed audit value.
        actor.env.flush_all()
        actor.line_ids.invalidate_recordset()
        ledger_fields = [
            "balance",
            "amount_currency",
            "account_id",
            "tax_ids",
            "write_uid",
            "write_date",
        ]
        ledger = actor.line_ids.read(ledger_fields)
        actor.action_l10n_ae_prepare()
        issue = actor.l10n_ae_transmission_id
        contract = json.loads(issue.payload).get("binding_contract")
        self.assertIsNotNone(contract, "Frozen issue must retain its reviewed contract")
        self.assertEqual(contract["profile"], "kmee-controlled-1")
        self.assertEqual(
            contract["capabilities"],
            ["billing_invoice", "lookup", "report", "submit"],
        )
        self.assertEqual(contract["company_id"], actor.company_id.id)
        self.assertEqual(contract["provider"], "controlled")
        self.assertEqual(contract["environment"], "sandbox")
        self.assertEqual(contract["reviewed_by"], self.simple_accountman.id)
        self.assertEqual(contract["reviewed_at"], str(reviewed_at))
        self.assertEqual(contract["review_hash"], reviewed_hash)
        evidence_fields = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
        ]
        original = issue.read(evidence_fields)
        actor.env.flush_all()
        actor.line_ids.invalidate_recordset()
        self.assertEqual(
            actor.line_ids.read(ledger_fields),
            ledger,
            "Flushing the posted invoice must preserve its observed ledger",
        )
        with Form(binding) as form:
            form.capability_review_note = "Proposed different provider contract."
        self.assertEqual(binding.capability_review_state, "review")
        self.assertFalse(binding.capability_reviewed_at)
        self.assertEqual(
            actor.line_ids.read(ledger_fields),
            ledger,
            "Reviewing a different contract must preserve the posted ledger",
        )
        actor.action_l10n_ae_prepare()
        self.assertEqual(actor.l10n_ae_transmission_id, issue)
        self.assertEqual(issue.read(evidence_fields), original)
        self.assertEqual(actor.line_ids.read(ledger_fields), ledger)

    def test_native_journal_line_edit_preserves_parent_audit_metadata(self):
        entry = self.env["account.move"].create(
            {
                "move_type": "entry",
                "journal_id": self.company_data["default_journal_misc"].id,
                "line_ids": [
                    Command.create(
                        {
                            "name": "Synthetic native journal line",
                            "account_id": self.company_data[
                                "default_account_expense"
                            ].id,
                            "debit": 100,
                        }
                    ),
                    Command.create(
                        {
                            "name": "Synthetic native journal counterpart",
                            "account_id": self.company_data[
                                "default_account_revenue"
                            ].id,
                            "credit": 100,
                        }
                    ),
                ],
            }
        )
        self.assertFalse(entry.is_invoice(include_receipts=True))
        self.assertFalse(entry.l10n_ae_transmission_id)
        parent_audit = entry.read(["write_uid", "write_date"])
        line = entry.line_ids[0].with_user(self.simple_accountman)
        self.assertFalse(line.env.su)
        self.assertNotEqual(entry.write_uid, self.simple_accountman)
        line.write({"name": "Synthetic reviewed journal description"})
        self.assertEqual(line.name, "Synthetic reviewed journal description")
        self.assertEqual(line.write_uid, self.simple_accountman)
        self.assertEqual(
            entry.read(["write_uid", "write_date"]),
            parent_audit,
            "Editing a native journal description must not rewrite the parent "
            "through the UAE invoice guard",
        )
        self.assertEqual(sum(entry.line_ids.mapped("balance")), 0)
        self.assertFalse(entry.l10n_ae_transmission_id)

    def test_frozen_invoice_line_price_cannot_be_rewritten(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        payload = invoice.l10n_ae_transmission_id.payload
        with self.assertRaisesRegex(UserError, "frozen UAE.*line"), self.cr.savepoint():
            invoice.invoice_line_ids.write({"price_unit": 120})
        self.assertEqual(invoice.invoice_line_ids.price_unit, 100)
        self.assertEqual(invoice.amount_total, 105)
        self.assertEqual(invoice.l10n_ae_transmission_id.payload, payload)

    def test_frozen_invoice_line_fiscal_facts_cannot_be_rewritten(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        payload = invoice.l10n_ae_transmission_id.payload
        for vals in (
            {"quantity": 2},
            {"discount": 10},
            {"name": "Rewritten fiscal description"},
            {"product_id": self.product_b.id},
            {"product_uom_id": self.env.ref("uom.product_uom_dozen").id},
            {"tax_ids": [Command.clear()]},
            {"currency_id": self.env.ref("base.USD").id},
            {"display_type": "cogs"},
            {"balance": -120, "amount_currency": -120},
            {"debit": 0, "credit": 120},
        ):
            with (
                self.subTest(field=next(iter(vals))),
                self.cr.savepoint(),
                self.assertRaisesRegex(UserError, "frozen UAE.*line"),
            ):
                invoice.invoice_line_ids.write(vals)
        self.assertEqual(invoice.invoice_line_ids.quantity, 1)
        self.assertEqual(invoice.invoice_line_ids.discount, 0)
        self.assertEqual(invoice.amount_total, 105)
        self.assertEqual(invoice.l10n_ae_transmission_id.payload, payload)

    def test_frozen_invoice_line_identical_fiscal_write_remains_native(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        payload = invoice.l10n_ae_transmission_id.payload
        line = invoice.invoice_line_ids
        self.assertTrue(
            line.write(
                {
                    "quantity": 1,
                    "price_unit": 100,
                    "discount": 0,
                    "name": line.name,
                    "product_id": line.product_id.id,
                    "product_uom_id": line.product_uom_id.id,
                    "tax_ids": [Command.set(self.tax_sale_a.ids)],
                    "currency_id": invoice.currency_id.id,
                    "display_type": "product",
                    "balance": -100,
                    "amount_currency": -100,
                }
            )
        )
        self.assertEqual(invoice.amount_total, 105)
        self.assertEqual(invoice.l10n_ae_transmission_id.payload, payload)

    def test_frozen_invoice_cannot_gain_an_extra_zero_value_line(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        original_ids = invoice.invoice_line_ids.ids
        payload = invoice.l10n_ae_transmission_id.payload
        with self.assertRaisesRegex(UserError, "frozen UAE.*line"), self.cr.savepoint():
            self.env["account.move.line"].create(
                {
                    "move_id": invoice.id,
                    "account_id": invoice.invoice_line_ids.account_id.id,
                    "name": "Unapproved extra fiscal line",
                    "quantity": 1,
                    "price_unit": 0,
                    "tax_ids": [Command.set(self.tax_sale_a.ids)],
                }
            )
        self.assertEqual(invoice.invoice_line_ids.ids, original_ids)
        self.assertEqual(invoice.amount_total, 105)
        self.assertEqual(invoice.l10n_ae_transmission_id.payload, payload)

    def test_frozen_invoice_cannot_lose_a_zero_value_line(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        zero_line = self.env["account.move.line"].create(
            {
                "move_id": invoice.id,
                "account_id": invoice.invoice_line_ids.account_id.id,
                "name": "Reviewed zero-value fiscal line",
                "quantity": 1,
                "price_unit": 0,
                "tax_ids": [Command.set(self.tax_sale_a.ids)],
            }
        )
        invoice.action_post()
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        original_ids = invoice.invoice_line_ids.ids
        payload = invoice.l10n_ae_transmission_id.payload
        with self.assertRaisesRegex(UserError, "frozen UAE.*line"), self.cr.savepoint():
            zero_line.unlink()
        self.assertEqual(invoice.invoice_line_ids.ids, original_ids)
        self.assertTrue(zero_line.exists())
        self.assertEqual(invoice.amount_total, 105)
        self.assertEqual(invoice.l10n_ae_transmission_id.payload, payload)

    def test_frozen_invoice_lines_cannot_be_transferred_in_either_direction(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        other = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        lines = {}
        for move in (invoice, other):
            lines[move.id] = self.env["account.move.line"].create(
                {
                    "move_id": move.id,
                    "account_id": move.invoice_line_ids.account_id.id,
                    "name": "Reviewed zero-value fiscal line",
                    "quantity": 1,
                    "price_unit": 0,
                    "tax_ids": [Command.set(self.tax_sale_a.ids)],
                }
            )
        invoice.action_post()
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        payload = invoice.l10n_ae_transmission_id.payload
        for source, target in ((invoice, other), (other, invoice)):
            with (
                self.subTest(source=source.id),
                self.cr.savepoint(),
                self.assertRaisesRegex(UserError, "frozen UAE.*line"),
            ):
                lines[source.id].write({"move_id": target.id})
        self.assertEqual(lines[invoice.id].move_id, invoice)
        self.assertEqual(lines[other.id].move_id, other)
        self.assertEqual((invoice.amount_total, other.amount_total), (105, 105))
        self.assertEqual(invoice.l10n_ae_transmission_id.payload, payload)

    def test_native_posting_batch_isolates_invalid_uae_data_and_allows_repair(self):
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": self.binding.id,
            }
        )
        invoices = self.env["account.move"]
        for amount in (100, 200, 300):
            invoices |= self.init_invoice(
                "out_invoice", amounts=[amount], taxes=self.tax_sale_a
            )
        invalid = invoices[1]
        invalid.l10n_ae_free_zone = True
        actor = invoices.with_user(self.simple_accountman)
        actor.action_post()

        rows = actor.read(
            [
                "state",
                "amount_total",
                "l10n_ae_transmission_id",
                "l10n_ae_preparation_state",
                "l10n_ae_preparation_error",
                "l10n_ae_preparation_review_at",
            ]
        )
        self.assertEqual([row["state"] for row in rows], ["posted"] * 3)
        self.assertEqual([row["amount_total"] for row in rows], [105, 210, 315])
        self.assertEqual(
            [row["l10n_ae_preparation_state"] for row in rows],
            ["ready", "review", "ready"],
        )
        self.assertFalse(rows[1]["l10n_ae_transmission_id"])
        self.assertIn("IBR-007-AE", rows[1]["l10n_ae_preparation_error"])
        self.assertTrue(rows[1]["l10n_ae_preparation_review_at"])
        self.assertFalse(rows[0]["l10n_ae_preparation_error"])
        self.assertFalse(rows[2]["l10n_ae_preparation_error"])
        issues = actor.mapped("l10n_ae_transmission_ids")
        self.assertEqual(len(issues), 2)
        self.assertEqual(issues.mapped("submission_state"), ["queued", "queued"])
        self.assertFalse(issues.mapped("attempt_ids"))
        self.assertFalse(invalid.l10n_ae_transmission_ids)
        ledger = actor.line_ids.read(
            ["balance", "amount_currency", "account_id", "tax_ids"]
        )

        repaired = actor[1]
        repaired.l10n_ae_beneficiary_id = "SYNTHETIC-BENEFICIARY"
        repaired.action_l10n_ae_prepare()
        repaired.action_l10n_ae_prepare()
        self.assertEqual(repaired.l10n_ae_preparation_state, "ready")
        self.assertFalse(repaired.l10n_ae_preparation_error)
        self.assertFalse(repaired.l10n_ae_preparation_review_at)
        self.assertEqual(len(repaired.l10n_ae_transmission_ids), 1)
        self.assertEqual(repaired.l10n_ae_transmission_id.submission_state, "queued")
        self.assertFalse(repaired.l10n_ae_transmission_id.attempt_ids)
        self.assertEqual(
            actor.line_ids.read(
                ["balance", "amount_currency", "account_id", "tax_ids"]
            ),
            ledger,
        )

    def test_native_accounting_failure_is_not_converted_to_uae_review(self):
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": self.binding.id,
            }
        )
        valid = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        empty = self.init_invoice("out_invoice", amounts=[])
        batch = (valid | empty).with_user(self.simple_accountman)
        with self.assertRaisesRegex(UserError, "magicians"):
            batch.action_post()
        self.assertEqual(batch.mapped("state"), ["draft", "draft"])
        self.assertEqual(
            batch.mapped("l10n_ae_preparation_state"), ["pending", "pending"]
        )
        self.assertEqual(batch.mapped("l10n_ae_preparation_error"), [False, False])
        self.assertEqual(batch.mapped("l10n_ae_preparation_review_at"), [False, False])
        self.assertFalse(batch.mapped("l10n_ae_transmission_ids"))
        self.assertEqual(batch.mapped("l10n_ae_fiscal_uuid"), [False, False])
        self.assertEqual((valid.amount_total, empty.amount_total), (105, 0))

    def test_public_orm_cannot_spoof_or_clear_uae_preparation_review(self):
        self.env.company.l10n_ae_edi_auto_prepare = True
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.l10n_ae_free_zone = True
        actor = invoice.with_user(self.simple_accountman)
        actor.action_post()
        fields = [
            "state",
            "amount_total",
            "l10n_ae_preparation_state",
            "l10n_ae_preparation_error",
            "l10n_ae_preparation_review_at",
            "l10n_ae_transmission_ids",
            "write_date",
        ]
        original = actor.read(fields)
        self.assertEqual(actor.l10n_ae_preparation_state, "review")
        for values in (
            {"l10n_ae_preparation_state": "ready"},
            {"l10n_ae_preparation_error": False},
            {"l10n_ae_preparation_error": "Invented acceptance"},
            {"l10n_ae_preparation_review_at": False},
        ):
            with self.subTest(values=values), self.assertRaises(UserError):
                actor.write(values)
            self.assertEqual(actor.read(fields), original)
        for values in (
            {"l10n_ae_preparation_state": "ready"},
            {"l10n_ae_preparation_error": "Invented acceptance"},
            {"l10n_ae_preparation_review_at": "2026-01-01 00:00:00"},
        ):
            with self.subTest(values=values), self.assertRaises(UserError):
                actor.copy(values)
            self.assertEqual(actor.read(fields), original)

    def test_posting_with_automatic_queue_creates_one_frozen_intent(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.l10n_ae_binding_id = self.binding
        self.env.company.l10n_ae_edi_auto_prepare = True
        invoice.action_post()
        transmission = invoice.l10n_ae_transmission_id
        self.assertEqual(invoice.state, "posted")
        self.assertTrue(transmission)
        self.assertEqual(transmission.submission_state, "queued")
        self.assertEqual(
            json.loads(transmission.payload)["totals"]["payable"], "105.00"
        )
        self.assertFalse(transmission.attempt_ids)

    def test_posting_uses_the_company_provider_when_invoice_has_no_override(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": self.binding.id,
            }
        )
        invoice.action_post()
        self.assertEqual(invoice.l10n_ae_binding_id, self.binding)
        self.assertEqual(invoice.l10n_ae_transmission_id.binding_id, self.binding)

    def test_company_cannot_select_another_legal_entity_provider(self):
        other = self.env["res.company"].create({"name": "Another synthetic UAE entity"})
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Another entity's provider",
                "company_id": other.id,
            }
        )
        with self.assertRaises(UserError), self.cr.savepoint():
            self.env.company.l10n_ae_edi_binding_id = binding

    def test_disabled_automatic_queue_preserves_standard_posting(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertFalse(invoice.l10n_ae_transmission_id)

    def test_ordinary_purchase_is_not_queued_by_company_policy(self):
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": self.binding.id,
            }
        )
        bill = self.init_invoice("in_invoice", amounts=[100], taxes=self.tax_purchase_a)
        self.assertFalse(bill.is_self_billing)
        bill.action_post()
        self.assertEqual(bill.state, "posted")
        self.assertFalse(bill.l10n_ae_transmission_id)

    def test_missing_provider_preserves_posting_with_preparation_review(self):
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": False,
            }
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.l10n_ae_preparation_state, "review")
        self.assertIn("Select an active ASP", invoice.l10n_ae_preparation_error)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        with self.assertRaisesRegex(UserError, "Select an active ASP"):
            invoice.action_l10n_ae_prepare()

    def test_explicit_invoice_provider_takes_precedence_over_company_default(self):
        override = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic override"}
        )
        review_controlled_binding(
            override,
            self.simple_accountman,
            capabilities=("submit", "lookup", "report", "billing_invoice"),
            note="Synthetic controlled invoice-provider override fixture.",
        )
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": self.binding.id,
            }
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.l10n_ae_binding_id = override
        invoice.action_post()
        self.assertEqual(invoice.l10n_ae_transmission_id.binding_id, override)

    def test_preparation_freezes_customer_address_and_reuses_original_payload(self):
        self.partner_a.street = "Original synthetic address"
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        transmission = invoice.l10n_ae_transmission_id
        payload = transmission.payload
        self.assertEqual(
            json.loads(payload)["counterparty"]["street"], "Original synthetic address"
        )
        self.assertEqual(json.loads(payload)["totals"]["payable"], "105.00")
        self.partner_a.street = "Changed synthetic address"
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.l10n_ae_transmission_id, transmission)
        self.assertEqual(transmission.payload, payload)

    def test_batch_created_manual_invoices_receive_distinct_business_identities(self):
        template = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        )
        invoices = self.env["account.move"].create(
            [template.copy_data()[0], template.copy_data()[0]]
        )
        self.assertNotEqual(
            invoices[0].l10n_ae_origin_document_id,
            invoices[1].l10n_ae_origin_document_id,
        )

    def test_frozen_invoice_payload_cannot_be_edited_by_an_administrator(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        transmission = invoice.l10n_ae_transmission_id.sudo()
        with self.assertRaisesRegex(UserError, "immutable"):
            transmission.write({"payload": '{"totals":{"payable":"1.00"}}'})

    def test_frozen_invoice_reference_cannot_be_cleared_to_prepare_again(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        with self.assertRaisesRegex(UserError, "frozen UAE"):
            invoice.write({"l10n_ae_transmission_id": False})

    def test_frozen_evidence_cannot_be_deleted_by_an_administrator(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        with self.assertRaisesRegex(UserError, "immutable"):
            invoice.l10n_ae_transmission_id.sudo().unlink()

    def test_prepared_invoice_audit_matches_installed_format_capability(self):
        pint_installed = bool(
            self.env["ir.module.module"].search(
                [("name", "=", "l10n_ae_edi_pint"), ("state", "=", "installed")]
            )
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        # Exercise both supported installations through the native preparation action.
        self.assertEqual(bool(issue.xml_content), pint_installed)
        records = issue.read(["evidence_archive", "payload_hash", "submission_state"])
        self.assertEqual(bool(records[0]["evidence_archive"]), pint_installed)
        self.assertEqual(records[0]["submission_state"], "queued")
        self.assertEqual(invoice.amount_total, 105)
