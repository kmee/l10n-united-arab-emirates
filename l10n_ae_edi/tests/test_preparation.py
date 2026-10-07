# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaeInvoicePreparation(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        cls.partner_a.country_id = cls.env.ref("base.ae")
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

    def test_missing_provider_rolls_back_posting_and_intent(self):
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": False,
            }
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        with (
            self.assertRaisesRegex(UserError, "Select an active ASP"),
            self.cr.savepoint(),
        ):
            invoice.action_post()
        self.assertEqual(invoice.state, "draft")
        self.assertFalse(invoice.l10n_ae_transmission_id)

    def test_explicit_invoice_provider_takes_precedence_over_company_default(self):
        override = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic override"}
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
