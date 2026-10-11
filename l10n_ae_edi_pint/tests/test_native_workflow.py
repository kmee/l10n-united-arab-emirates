# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json
import os
from unittest.mock import patch

from lxml import etree

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.l10n_ae_edi.tests.common import (
    CONTROLLED_CAPABILITIES,
    review_controlled_binding,
)

from .common import NS, UaePintTestCommon, requires_normative_resources


@tagged("post_install", "-at_install")
class TestUaeNativeWorkflow(UaePintTestCommon):
    @requires_normative_resources("1.0.4")
    def test_native_post_reviews_a_blank_item_name_without_changing_accounting(self):
        self.env.company.l10n_ae_edi_auto_prepare = True
        binding = self._reviewed_binding(
            {
                "name": "Synthetic blank-description posting contract",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=("submit", "lookup", "report", "billing_invoice"),
            note="Synthetic outbound contract for blank item-name review.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        invoice.l10n_ae_binding_id = binding
        line = invoice.invoice_line_ids
        # Native Sales can leave the extra description blank; the actual product
        # name remains the mandatory item name, without inventing a fallback.
        line.write({"product_id": self.product_a.id, "name": False, "price_unit": 100})
        self.assertFalse(line.name)
        invoice.action_l10n_ae_validate()
        named_xml = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        self.assertEqual(
            named_xml.findtext("cac:InvoiceLine/cac:Item/cbc:Name", namespaces=NS),
            self.product_a.name,
        )
        # A service line without a product is valid when its item name is present.
        line.write(
            {
                "product_id": False,
                "label": "Synthetic described service",
                "price_unit": 100,
            }
        )
        invoice.action_l10n_ae_validate()
        self.assertFalse(line.product_id)
        self.assertEqual(line.name, "Synthetic described service")
        self.assertFalse(
            invoice.l10n_ae_fiscal_uuid or invoice.l10n_ae_transmission_ids
        )
        # Replay the actual saved source facts from the failed browser journey.
        # The Odoo 20 label inverse stores name=False when label is False.
        line.write({"product_id": False, "label": False})
        self.assertFalse(invoice.env.su)
        self.assertFalse(line.product_id or line.label or line.name)
        self.assertEqual((line.quantity, line.price_unit), (1, 100))
        ledger_fields = [
            "id",
            "account_id",
            "partner_id",
            "currency_id",
            "date_maturity",
            "debit",
            "credit",
            "amount_currency",
            "tax_ids",
        ]
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)

        invoice.action_post()

        self.assertEqual(invoice.state, "posted")
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        self.assertEqual(invoice.l10n_ae_preparation_state, "review")
        self.assertIn("IBT-153", invoice.l10n_ae_preparation_error)
        self.assertIn("nonblank item name", invoice.l10n_ae_preparation_error)
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.ubl_cii_xml_id or invoice.invoice_pdf_report_id)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        with self.assertRaisesRegex(UserError, "IBT-153:.*nonblank item name") as error:
            invoice.action_l10n_ae_validate()
        self.assertIs(type(error.exception), UserError)
        line.label = " \t\n "
        with self.assertRaisesRegex(UserError, "IBT-153:.*nonblank item name"):
            invoice.action_l10n_ae_validate()
        self.assertFalse(
            invoice.l10n_ae_fiscal_uuid or invoice.l10n_ae_transmission_ids
        )
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)

        line.label = "Synthetic repaired service description"
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.l10n_ae_preparation_state, "ready")
        self.assertEqual(len(invoice.l10n_ae_transmission_ids), 1)
        self.assertTrue(invoice.l10n_ae_fiscal_uuid)
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        xml = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        self.assertEqual(
            xml.findtext("cac:InvoiceLine/cac:Item/cbc:Name", namespaces=NS),
            "Synthetic repaired service description",
        )
        self.assertFalse(line.product_id)
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)

    @requires_normative_resources("1.0.4")
    def test_native_partial_credit_freezes_selected_customer_payment_account(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        journal = self.company_data["default_journal_bank"]
        method = journal.inbound_payment_method_line_ids.filtered(
            lambda line: line.code == "manual"
        )[:1]
        self.assertTrue(method)
        company_account = self.env["res.partner.bank"].create(
            {
                "partner_id": self.env.company.partner_id.id,
                "company_id": self.env.company.id,
                "account_number": "SYNTH COMPANY 01",
            }
        )
        journal.bank_account_id = company_account
        customer_account = self.env["res.partner.bank"].create(
            {
                "partner_id": self.partner_a.commercial_partner_id.id,
                "company_id": self.env.company.id,
                "account_number": "SYNTH CUSTOMER 01",
                "allow_out_payment": True,
            }
        )
        binding = self._reviewed_binding(
            {
                "name": "Synthetic credit recipient review",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_native_partial_credit_freezes"
            "_selected_customer_payment_account.",
        )
        self.env.company.l10n_ae_edi_binding_id = binding
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(original.env.su)
        with Form(original) as form:
            form.preferred_payment_method_line_id = method
            form.partner_bank_id = company_account
            form.payment_reference = "SYNTH-COMPANY-INVOICE-2026"
            form.l10n_ae_binding_id = binding
        original.action_post()
        original.action_l10n_ae_prepare()
        self.assertTrue(original.validate_l10n_ae_pint()["valid"])
        immutable = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
        ]
        original_issue = original.l10n_ae_transmission_id
        original_frozen = original_issue.read(immutable)
        wizard = (
            self.env["account.move.reversal"]
            .with_user(self.simple_accountman)
            .with_context(active_model="account.move", active_ids=original.ids)
            .create(
                {
                    "move_ids": [(6, 0, original.ids)],
                    "journal_id": original.journal_id.id,
                    "reason": "Synthetic payment instruction partial credit",
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                }
            )
        )
        wizard.refund_moves()
        credit = wizard.new_move_ids
        self.assertEqual(credit.move_type, "out_refund")
        self.assertEqual(credit.reversed_entry_id, original)
        self.assertEqual(credit.bank_partner_id, self.partner_a.commercial_partner_id)
        self.assertFalse(credit.l10n_ae_transmission_id)
        with Form(credit) as form:
            with form.invoice_line_ids.edit(0) as line:
                line.price_unit = 40
            # This is the native sales-form preference, not refund execution.
            form.preferred_payment_method_line_id = method
            form.payment_reference = "SYNTH-CUSTOMER-CREDIT-2026"
            form.partner_bank_id = self.env["res.partner.bank"]
            form.l10n_ae_binding_id = binding
        self.assertFalse(credit.partner_bank_id)
        self.env.company.l10n_ae_edi_auto_prepare = True
        with self.assertRaisesRegex(UserError, "IBR-192-AE"):
            credit.action_l10n_ae_validate()
        credit.action_post()
        self.assertEqual(credit.state, "posted")
        self.assertEqual(credit.l10n_ae_preparation_state, "review")
        self.assertIn("IBR-192-AE", credit.l10n_ae_preparation_error)
        self.assertFalse(credit.l10n_ae_fiscal_uuid)
        self.assertFalse(credit.l10n_ae_transmission_id)
        self.assertEqual(
            (credit.amount_untaxed, credit.amount_tax, credit.amount_total), (40, 2, 42)
        )
        self.assertEqual((original.amount_residual, credit.amount_residual), (63, 0))
        with self.assertRaisesRegex(UserError, "IBR-192-AE"):
            credit.action_l10n_ae_prepare()
        with Form(credit) as form:
            form.partner_bank_id = customer_account
        # Native Post reconciles the partial credit, and Form can refresh line
        # metadata. Observe both complete ledgers after the reviewed Form save.
        ledger_fields = [
            "account_id",
            "partner_id",
            "currency_id",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "create_date",
            "write_date",
        ]
        ledgers = [
            move.line_ids.sorted("id").read(ledger_fields)
            for move in (original, credit)
        ]
        credit.action_l10n_ae_validate()
        credit.action_l10n_ae_prepare()
        issue = credit.l10n_ae_transmission_id
        self.assertNotEqual(issue, original_issue)
        self.assertNotEqual(credit.l10n_ae_fiscal_uuid, original.l10n_ae_fiscal_uuid)
        snapshot = json.loads(issue.payload)
        self.assertEqual(snapshot["payment_means"]["code"], "30")
        self.assertEqual(snapshot["payment_means"]["method_line_id"], method.id)
        self.assertEqual(snapshot["payment_means"]["account_id"], customer_account.id)
        self.assertEqual(
            snapshot["payment_means"]["account_identifier"], "SYNTHCUSTOMER01"
        )
        tree = etree.fromstring(issue.xml_content.encode())
        expected = {
            "cbc:CreditNoteTypeCode": "381",
            "cac:PaymentMeans/cbc:PaymentMeansCode": "30",
            "cac:PaymentMeans/cbc:PaymentID": "SYNTH-CUSTOMER-CREDIT-2026",
            "cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID": "SYNTHCUSTOMER01",
            "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID": original.name,
            "cac:DiscrepancyResponse/cbc:ResponseCode": "DL8.61.1.C",
            "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount": "40.00",
            "cac:TaxTotal/cbc:TaxAmount": "2.00",
            "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount": "42.00",
            "cac:LegalMonetaryTotal/cbc:PrepaidAmount": "42.00",
            "cac:LegalMonetaryTotal/cbc:PayableAmount": "0.00",
        }
        for path, value in expected.items():
            self.assertEqual(tree.findtext(path, namespaces=NS), value, path)
        self.assertNotIn("SYNTHCOMPANY01", issue.xml_content)
        self.assertEqual(original_issue.read(immutable), original_frozen)
        self.assertTrue(credit.validate_l10n_ae_pint()["valid"])
        frozen = issue.read(immutable)
        self.assertTrue(customer_account.allow_out_payment)
        customer_account.holder_name = "Synthetic reviewed customer bank holder"
        journal.name = "Synthetic renamed payment preference journal"
        for move in (original, credit):
            move.action_l10n_ae_prepare()
            self.assertTrue(move.validate_l10n_ae_pint()["valid"])
        self.assertEqual(credit.preview_l10n_ae_pint(), frozen[0]["xml_content"])
        self.assertEqual(issue.read(immutable), frozen)
        self.assertEqual(original_issue.read(immutable), original_frozen)
        self.assertEqual(
            [
                move.line_ids.sorted("id").read(ledger_fields)
                for move in (original, credit)
            ],
            ledgers,
        )
        self.assertFalse(issue.attempt_ids or original_issue.attempt_ids)
        self.assertFalse(issue.event_ids or original_issue.event_ids)

    @requires_normative_resources("1.0.4")
    def test_native_transfer_requires_recipient_account_and_freezes_review(self):
        journal = self.company_data["default_journal_bank"]
        journal.bank_account_id = False
        method = journal.inbound_payment_method_line_ids.filtered(
            lambda line: line.code == "manual"
        )[:1]
        self.assertTrue(method)
        binding = self._reviewed_binding(
            {
                "name": "Synthetic native transfer instruction",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_native_transfer_requires_reci"
            "pient_account_and_freezes_review.",
        )
        self.env.company.write(
            {"l10n_ae_edi_auto_prepare": True, "l10n_ae_edi_binding_id": binding.id}
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        with Form(invoice) as form:
            form.preferred_payment_method_line_id = method
            form.payment_reference = "SYNTH-TRANSFER-2026"
            form.partner_bank_id = self.env["res.partner.bank"]
        self.assertFalse(invoice.partner_bank_id)
        with self.assertRaisesRegex(UserError, "IBR-192-AE"):
            invoice.action_l10n_ae_validate()
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.l10n_ae_preparation_state, "review")
        self.assertIn("IBR-192-AE", invoice.l10n_ae_preparation_error)
        self.assertFalse(invoice.l10n_ae_transmission_id)
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        ledger_fields = [
            "account_id",
            "partner_id",
            "currency_id",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "create_date",
            "write_date",
        ]
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        with self.assertRaisesRegex(UserError, "IBR-192-AE"):
            invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        account = self.env["res.partner.bank"].create(
            {
                "partner_id": self.env.company.partner_id.id,
                "company_id": self.env.company.id,
                "account_number": "SYNTH BANK 01",
            }
        )
        journal.bank_account_id = account
        with Form(invoice) as form:
            form.partner_bank_id = account
        # Native form saving can update line timestamps. Freeze after this review.
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        invoice.action_l10n_ae_validate()
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        tree = etree.fromstring(issue.xml_content.encode())
        means = tree.find("cac:PaymentMeans", namespaces=NS)
        self.assertEqual(means.findtext("cbc:PaymentMeansCode", namespaces=NS), "30")
        self.assertEqual(
            means.findtext("cbc:PaymentID", namespaces=NS), "SYNTH-TRANSFER-2026"
        )
        self.assertEqual(
            means.findtext("cac:PayeeFinancialAccount/cbc:ID", namespaces=NS),
            "SYNTHBANK01",
        )
        self.assertEqual(
            json.loads(issue.payload)["payment_means"],
            {
                "code": "30",
                "name": "Credit transfer",
                "payment_reference": "SYNTH-TRANSFER-2026",
                "method_line_id": method.id,
                "method_code": "manual",
                "journal_id": journal.id,
                "journal_type": "bank",
                "account_id": account.id,
                "account_identifier": "SYNTHBANK01",
            },
        )
        immutable = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
        ]
        frozen = issue.read(immutable)
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        self.assertTrue(account.allow_out_payment)
        with self.assertRaisesRegex(UserError, "trusted"):
            account.account_number = "SYNTH BANK 02"
        account.holder_name = "Synthetic reviewed bank holder"
        self.assertEqual(account.account_number, "SYNTH BANK 01")
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), frozen[0]["xml_content"])
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        self.assertEqual(issue.read(immutable), frozen)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.event_ids)

    @requires_normative_resources("1.0.4")
    def test_native_cash_method_freezes_instruction_and_preserves_ledger(
        self,
    ):
        self.env.company.l10n_ae_edi_auto_prepare = False
        cash = self.company_data["default_journal_cash"]
        method = cash.inbound_payment_method_line_ids.filtered(
            lambda line: line.code == "manual"
        )[:1]
        self.assertTrue(method)
        binding = self._reviewed_binding(
            {
                "name": "Synthetic native cash instruction",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_native_cash_method_freezes_in"
            "struction_and_preserves_ledger.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        with Form(invoice) as form:
            form.preferred_payment_method_line_id = method
            form.payment_reference = "SYNTH-CASH-2026"
            form.l10n_ae_binding_id = binding
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        ledger_fields = [
            "account_id",
            "partner_id",
            "currency_id",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "create_date",
            "write_date",
        ]
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        invoice.action_l10n_ae_validate()
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        tree = etree.fromstring(issue.xml_content.encode())
        means = tree.findall("cac:PaymentMeans", namespaces=NS)
        self.assertEqual(len(means), 1)
        self.assertEqual(means[0].findtext("cbc:PaymentMeansCode", namespaces=NS), "10")
        self.assertEqual(
            means[0].findtext("cbc:PaymentID", namespaces=NS), "SYNTH-CASH-2026"
        )
        self.assertIsNone(means[0].find("cac:PayeeFinancialAccount", namespaces=NS))
        self.assertEqual(
            json.loads(issue.payload)["payment_means"],
            {
                "code": "10",
                "name": "In cash",
                "payment_reference": "SYNTH-CASH-2026",
                "method_line_id": method.id,
                "method_code": "manual",
                "journal_id": cash.id,
                "journal_type": "cash",
                "account_id": None,
                "account_identifier": None,
            },
        )
        immutable = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
        ]
        frozen = issue.read(immutable)
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        cash.name = "Synthetic renamed cash journal"
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), frozen[0]["xml_content"])
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        self.assertEqual(invoice.l10n_ae_transmission_id, issue)
        self.assertEqual(issue.read(immutable), frozen)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.event_ids)

    def test_native_ubl_contract_returns_tax_category_errors_without_freezing(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=False)
        invoice = invoice.with_user(self.simple_accountman)
        invoice.l10n_ae_document_category = "tax"
        self.assertFalse(invoice.env.su)
        ledger_fields = ["account_id", "debit", "credit", "write_date"]
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        # This is the native UBL extension protocol used by Send/download,
        # resolved by the public core's registered format selection.
        builder = invoice.partner_id._get_edi_builder("kmee_ae_pint")
        content, errors = builder._export_invoice(invoice)
        self.assertIsInstance(content, bytes)
        self.assertIsInstance(errors, set)
        self.assertTrue(any("IBR-151-AE" in error for error in errors), errors)
        tree = etree.fromstring(content)
        self.assertEqual(tree.findtext("cbc:InvoiceTypeCode", namespaces=NS), "380")
        self.assertEqual(
            tree.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=NS),
            "100.00",
        )
        with self.assertRaisesRegex(UserError, "IBR-151-AE"):
            invoice.action_l10n_ae_validate()
        with self.assertRaisesRegex(UserError, "IBR-151-AE"):
            invoice.preview_l10n_ae_pint()
        self.assertEqual(invoice.state, "draft")
        self.assertFalse(
            invoice.l10n_ae_transmission_ids or invoice.l10n_ae_fiscal_uuid
        )
        self.assertFalse(invoice.ubl_cii_xml_id or invoice.invoice_pdf_report_id)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)

    def test_native_ubl_contract_returns_reverse_charge_nature_error(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic export contract reverse charge",
                "amount": 0,
                "ubl_cii_tax_category_code": "AE",
            }
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=tax)
        invoice = invoice.with_user(self.simple_accountman)
        invoice.invoice_line_ids.product_id = False
        self.assertFalse(invoice.env.su)
        builder = invoice.partner_id._get_edi_builder("kmee_ae_pint")
        content, errors = builder._export_invoice(invoice)
        self.assertIsInstance(content, bytes)
        self.assertIsInstance(errors, set)
        self.assertTrue(any("IBR-166-AE" in error for error in errors), errors)
        with self.assertRaisesRegex(UserError, "IBR-166-AE"):
            invoice.action_l10n_ae_validate()
        with self.assertRaisesRegex(UserError, "IBR-166-AE"):
            invoice.preview_l10n_ae_pint()
        self.assertEqual(invoice.state, "draft")
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 0, 100),
        )
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        invoice.invoice_line_ids.l10n_ae_edi_rcm_nature_code = "DL8.48.8.2"
        content, errors = builder._export_invoice(invoice)
        self.assertFalse(any("IBR-166-AE" in error for error in errors), errors)
        tree = etree.fromstring(content)
        self.assertEqual(
            tree.findtext(
                "cac:InvoiceLine/cac:Item/cac:CommodityClassification/cbc:NatureCode",
                namespaces=NS,
            ),
            "DL8.48.8.2",
        )
        invoice.action_l10n_ae_validate()
        self.assertFalse(invoice.l10n_ae_transmission_ids)

    def test_native_ubl_contract_returns_payment_review_error(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        # Repairing a bank account requires the native Contact / Creation role.
        self.simple_accountman.group_ids |= self.env.ref("base.group_partner_manager")
        journal = self.company_data["default_journal_bank"]
        journal.bank_account_id = False
        method = journal.inbound_payment_method_line_ids.filtered(
            lambda line: line.code == "manual"
        )[:1]
        self.assertTrue(method)
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        invoice.write(
            {"preferred_payment_method_line_id": method.id, "partner_bank_id": False}
        )
        self.assertFalse(invoice.env.su)
        builder = invoice.partner_id._get_edi_builder("kmee_ae_pint")
        content, errors = builder._export_invoice(invoice)
        self.assertIsInstance(content, bytes)
        self.assertIsInstance(errors, set)
        self.assertTrue(any("IBR-192-AE" in error for error in errors), errors)
        with self.assertRaisesRegex(UserError, "IBR-192-AE"):
            invoice.action_l10n_ae_validate()
        with self.assertRaisesRegex(UserError, "IBR-192-AE"):
            invoice.preview_l10n_ae_pint()
        self.assertEqual(invoice.state, "draft")
        self.assertEqual(invoice.amount_total, 105)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        account = (
            self.env["res.partner.bank"]
            .with_user(self.simple_accountman)
            .create(
                {
                    "partner_id": self.env.company.partner_id.id,
                    "company_id": self.env.company.id,
                    "account_number": "SYNTH EXPORT BANK 01",
                }
            )
        )
        invoice.partner_bank_id = account
        content, errors = builder._export_invoice(invoice)
        self.assertFalse(errors)
        means = etree.fromstring(content).find("cac:PaymentMeans", namespaces=NS)
        self.assertEqual(means.findtext("cbc:PaymentMeansCode", namespaces=NS), "30")
        self.assertEqual(
            means.findtext("cac:PayeeFinancialAccount/cbc:ID", namespaces=NS),
            "SYNTHEXPORTBANK01",
        )
        invoice.action_l10n_ae_validate()
        self.assertFalse(invoice.l10n_ae_transmission_ids)

    def test_native_ubl_contract_returns_aed_accounting_mismatch(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        usd = self.env.ref("base.USD")
        usd.active = True
        self.env["res.currency.rate"].create(
            {
                "currency_id": usd.id,
                "company_id": self.env.company.id,
                "name": "2019-01-01",
                "rate": 1 / 3.6725,
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd
        ).with_user(self.simple_accountman)
        invoice.write(
            {
                "l10n_ae_tax_exchange_rate": "0.272294",
                "l10n_ae_tax_exchange_date": invoice.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-WRONG-DIRECTION",
            }
        )
        self.assertFalse(invoice.env.su)
        ledger_fields = ["account_id", "debit", "credit", "amount_currency", "tax_ids"]
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        builder = invoice.partner_id._get_edi_builder("kmee_ae_pint")
        for reviewed_rate in ("0.272294", "3.672501"):
            with self.subTest(reviewed_rate=reviewed_rate):
                invoice.l10n_ae_tax_exchange_rate = reviewed_rate
                content, errors = builder._export_invoice(invoice)
                self.assertIsInstance(content, bytes)
                self.assertIsInstance(errors, set)
                self.assertTrue(
                    any(
                        "AED exchange rate does not match accounting" in e
                        for e in errors
                    )
                )
                self.assertTrue(
                    any("posted invoice rate: 3.672500" in e for e in errors)
                )
                for action in (
                    invoice.action_l10n_ae_validate,
                    invoice.preview_l10n_ae_pint,
                ):
                    with self.assertRaisesRegex(UserError, "AED.*accounting"):
                        action()
                self.assertFalse(invoice.l10n_ae_transmission_ids)
                self.assertEqual(
                    invoice.line_ids.sorted("id").read(ledger_fields), ledger
                )
        self.assertEqual(invoice.amount_total, 105)
        self.assertEqual(invoice.amount_total_signed, 385.61)
        invoice.l10n_ae_tax_exchange_rate = "3.672500"
        content, errors = builder._export_invoice(invoice)
        self.assertFalse(errors)
        document = etree.fromstring(content)
        self.assertEqual(
            document.xpath(
                "./cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']/text()", namespaces=NS
            ),
            ["18.36"],
        )
        self.assertEqual(
            document.xpath(
                "./cac:AdditionalDocumentReference[cbc:ID='AED']/"
                "cbc:DocumentDescription/text()",
                namespaces=NS,
            ),
            ["385.61"],
        )
        invoice.action_l10n_ae_validate()
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertEqual(invoice.state, "draft")
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.ubl_cii_xml_id or invoice.invoice_pdf_report_id)

    def test_native_send_cron_isolates_invalid_invoice_across_companies(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        other = self.setup_other_company(name="Synthetic independent Send company")
        other_company = other["company"]
        other_company.l10n_ae_edi_auto_prepare = False
        self.simple_accountman.company_ids |= other_company
        self.partner_a.with_company(self.env.company).write(
            {"invoice_sending_method": "manual", "invoice_edi_format": "kmee_ae_pint"}
        )
        self.partner_a.with_company(other_company).write(
            {"invoice_sending_method": "manual", "invoice_edi_format": False}
        )
        invalid = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        invalid.l10n_ae_free_zone = True
        valid = self.init_invoice(
            "out_invoice",
            amounts=[100],
            taxes=other["default_tax_sale"],
            company=other_company,
        ).with_user(self.simple_accountman)
        invoices = invalid | valid
        self.assertFalse(invoices.env.su)
        self.assertNotEqual(invalid.company_id, valid.company_id)
        invoices.action_post()
        with self.assertRaisesRegex(UserError, "IBR-007-AE"):
            invalid.action_l10n_ae_validate()
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "tax_ids",
        ]
        ledger = invoices.line_ids.sorted("id").read(ledger_fields)
        action = invoices.action_send_and_print()
        wizard = (
            self.env[action["res_model"]]
            .with_user(self.simple_accountman)
            .with_context(**action["context"])
            .create({})
        )
        wizard.action_send_and_print()
        self.assertTrue(all(invoices.mapped("sending_data")))
        self.assertFalse(invoices.invoice_pdf_report_id)
        with self.enter_registry_test_mode():
            result = self.env.ref(
                "account.ir_cron_account_move_send"
            ).method_direct_trigger()
        self.assertIs(result, True)
        self.assertTrue(valid.invoice_pdf_report_id)
        self.assertFalse(invalid.invoice_pdf_report_id)
        self.assertFalse(invalid.ubl_cii_xml_id)
        self.assertFalse(invoices.l10n_ae_transmission_ids)
        self.assertFalse(any(invoices.mapped("sending_data")))
        self.assertTrue(
            any("IBR-007-AE" in (m.body or "") for m in invalid.message_ids)
        )
        self.assertEqual(invoices.line_ids.sorted("id").read(ledger_fields), ledger)

    def test_native_batch_send_groups_category_review_without_emitting(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        self.partner_a.write(
            {"invoice_sending_method": "manual", "invoice_edi_format": "kmee_ae_pint"}
        )
        exempt = self.env.ref(f"account.{self.env.company.id}_uae_sale_tax_exempted")
        exempt.ubl_cii_tax_exemption_reason_code = "DL8.46.2"
        invoices = self.env["account.move"]
        for tax in (exempt, exempt, self.tax_sale_a):
            invoice = self.init_invoice("out_invoice", amounts=[25], taxes=tax)
            invoices |= invoice
        invoices = invoices.with_user(self.simple_accountman)
        self.assertFalse(invoices.env.su)
        invoices.action_post()
        self.assertEqual(invoices.mapped("state"), ["posted"] * 3)
        self.assertEqual(
            [(m.amount_untaxed, m.amount_tax, m.amount_total) for m in invoices],
            [(25, 0, 25), (25, 0, 25), (25, 1.25, 26.25)],
        )
        move_fields = [
            "write_date",
            "l10n_ae_fiscal_uuid",
            "l10n_ae_transmission_ids",
            "l10n_ae_preparation_state",
            "l10n_ae_preparation_error",
            "ubl_cii_xml_id",
            "invoice_pdf_report_id",
            "sending_data",
        ]
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "currency_id",
            "tax_ids",
            "create_date",
            "write_date",
        ]
        before = invoices.read(move_fields)
        ledger = invoices.line_ids.read(ledger_fields)
        artifacts = (
            self.env["ir.attachment"]
            .search(
                [("res_model", "=", "account.move"), ("res_id", "in", invoices.ids)]
            )
            .ids
        )
        action = invoices.action_send_and_print()
        self.assertEqual(action["res_model"], "account.move.send.batch.wizard")
        wizard = (
            self.env[action["res_model"]]
            .with_user(self.simple_accountman)
            .with_context(**action["context"])
            .create({})
        )
        grouped = {
            key: alert
            for key, alert in (wizard.alerts or {}).items()
            if key.startswith("l10n_ae_edi_")
        }
        self.assertEqual(len(grouped), 1)
        alert = next(iter(grouped.values()))
        self.assertEqual(alert["level"], "warning")
        self.assertIn("IBR-151-AE", alert["message"])
        self.assertEqual(alert["action_text"], "View Invoices")
        self.assertEqual(alert["action"]["res_model"], "account.move")
        self.assertCountEqual(
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .search(alert["action"]["domain"])
            .ids,
            invoices[:2].ids,
        )
        self.assertEqual(invoices.read(move_fields), before)
        self.assertEqual(invoices.line_ids.read(ledger_fields), ledger)
        self.assertEqual(
            self.env["ir.attachment"]
            .search(
                [("res_model", "=", "account.move"), ("res_id", "in", invoices.ids)]
            )
            .ids,
            artifacts,
        )

    def test_native_mixed_format_batch_reviews_only_the_selected_pint_invoice(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        for partner, selected_format in (
            (self.partner_a, "kmee_ae_pint"),
            (self.partner_b, False),
        ):
            partner.write(
                {
                    "invoice_sending_method": "email",
                    "invoice_edi_format": selected_format,
                    "email": False,
                }
            )
        pint = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, partner=self.partner_a
        )
        pdf = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, partner=self.partner_b
        )
        invoices = (pint | pdf).with_user(self.simple_accountman)
        invoices.l10n_ae_free_zone = True
        invoices.action_post()
        self.assertFalse(invoices.env.su)
        self.assertFalse(invoices.env.user.has_group("base.group_system"))
        self.assertEqual(invoices.mapped("amount_total"), [105, 105])
        fields_before = [
            "write_date",
            "l10n_ae_fiscal_uuid",
            "l10n_ae_transmission_ids",
            "ubl_cii_xml_id",
            "invoice_pdf_report_id",
        ]
        ledger_fields = [
            "id",
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "currency_id",
            "write_date",
        ]
        original = invoices.read(fields_before)
        ledger = invoices.line_ids.read(ledger_fields)
        action = invoices.action_send_and_print()
        wizard = (
            invoices.env[action["res_model"]]
            .with_context(**action["context"])
            .create({})
        )
        uae = [
            value
            for key, value in (wizard.alerts or {}).items()
            if key.startswith("l10n_ae_edi_")
        ]
        self.assertEqual(len(uae), 1)
        self.assertIn("IBR-007-AE", uae[0]["message"])
        self.assertEqual(uae[0]["action"]["res_id"], pint.id)
        self.assertEqual(uae[0]["action"]["res_model"], "account.move")
        core = wizard.alerts["account_missing_email"]
        self.assertEqual(core["level"], "warning")
        self.assertEqual(core["action"]["res_model"], "res.partner")
        self.assertCountEqual(
            invoices.env["res.partner"].search(core["action"]["domain"]).ids,
            [self.partner_a.id, self.partner_b.id],
        )
        self.assertEqual(invoices.read(fields_before), original)
        self.assertEqual(invoices.line_ids.read(ledger_fields), ledger)

    def test_native_send_preserves_core_alerts_and_distinct_review_groups(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        self.partner_a.write(
            {
                "invoice_sending_method": "email",
                "invoice_edi_format": "kmee_ae_pint",
                "email": False,
            }
        )
        zone = self.init_invoice("out_invoice", amounts=[25], taxes=self.tax_sale_a)
        zone.l10n_ae_free_zone = True
        agent = self.init_invoice("out_invoice", amounts=[25], taxes=self.tax_sale_a)
        agent.l10n_ae_disclosed_agent = True
        credit = self.init_invoice("out_refund", amounts=[25], taxes=self.tax_sale_a)
        credit.l10n_ae_credit_reason = False
        invoices = (zone | agent | credit).with_user(self.simple_accountman)
        invoices.action_post()
        action = invoices.action_send_and_print()
        wizard = (
            self.env[action["res_model"]]
            .with_user(self.simple_accountman)
            .with_context(**action["context"])
            .create({})
        )
        core = wizard.alerts["account_missing_email"]
        self.assertEqual(core["level"], "warning")
        self.assertEqual(core["action"]["res_model"], "res.partner")
        self.assertEqual(core["action"]["res_id"], self.partner_a.id)
        uae = [v for k, v in wizard.alerts.items() if k.startswith("l10n_ae_edi_")]
        self.assertEqual(len(uae), 3)
        for invoice, diagnostic in (
            (zone, "IBR-007-AE"),
            (agent, "IBR-137-AE"),
            (credit, "Select the official UAE credit note reason"),
        ):
            alert = next(a for a in uae if diagnostic in a["message"])
            self.assertEqual(alert["level"], "warning")
            self.assertEqual(alert["action"]["res_id"], invoice.id)
            self.assertEqual(alert["action"]["res_model"], "account.move")
        self.assertFalse(invoices.l10n_ae_transmission_ids)
        self.assertFalse(any(invoices.mapped("l10n_ae_fiscal_uuid")))
        self.assertFalse(any(invoices.mapped("sending_data")))

    def test_native_pdf_only_generate_skips_pint_alerts_without_fiscal_emission(
        self,
    ):
        self.env.company.l10n_ae_edi_auto_prepare = False
        self.partner_a.invoice_sending_method = "manual"
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        invoice.l10n_ae_free_zone = True
        invoice.action_post()
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "currency_id",
            "create_date",
            "write_date",
        ]
        ledger = invoice.line_ids.read(ledger_fields)
        action = invoice.action_send_and_print()
        wizard = (
            self.env[action["res_model"]]
            .with_user(self.simple_accountman)
            .with_context(**action["context"])
            .create({"move_id": invoice.id, "invoice_edi_format": False})
        )
        self.assertFalse(wizard.env.su)
        self.assertFalse(wizard.sending_methods)
        self.assertFalse(wizard.invoice_edi_format)
        self.assertFalse(
            {
                k: v
                for k, v in (wizard.alerts or {}).items()
                if k.startswith("l10n_ae_edi_")
            },
            "PDF-only Generate must not display a PINT-AE review warning.",
        )
        wizard.action_send_and_print()
        self.assertTrue(invoice.invoice_pdf_report_id)
        # Native no-HTTP test mode renders HTML into the report attachment;
        # actual PDF bytes require the separate browser runtime.
        self.assertTrue(invoice.invoice_pdf_report_id.raw.content)
        self.assertFalse(invoice.ubl_cii_xml_id)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        # Choosing PINT-AE after printing still refuses the fiscal diagnostic.
        pint = (
            self.env[action["res_model"]]
            .with_user(self.simple_accountman)
            .create({"move_id": invoice.id})
        )
        pint.write({"invoice_edi_format": "kmee_ae_pint"})
        self.assertEqual(pint.invoice_edi_format, "kmee_ae_pint")
        with self.cr.savepoint(), self.assertRaisesRegex(UserError, "IBR-007-AE"):
            pint.action_send_and_print()
        self.assertFalse(invoice.ubl_cii_xml_id)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)

    def test_send_format_choice_follows_fiscal_country_not_address(self):
        ae = self.env.ref("base.ae")
        fr = self.env.ref("base.fr")
        company_a = self.env.company
        company_a.partner_id.vat = False
        company_a.write(
            {"country_id": fr.id, "account_fiscal_country_id": ae.id, "state_id": False}
        )
        data_b = self.setup_other_company(name="Synthetic foreign fiscal Send")
        data_b["company"].write(
            {"country_id": ae.id, "account_fiscal_country_id": fr.id}
        )
        self.simple_accountman.company_ids = [
            Command.set([company_a.id, data_b["company"].id])
        ]
        self.partner_a.invoice_edi_format = False
        customer = self.partner_a.read(["write_date", "invoice_edi_format"])
        for data, address, fiscal, can_choose in (
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
                        "invoice_date": "2019-01-01",
                        "journal_id": data["default_journal_sale"].id,
                        "l10n_ae_document_category": "commercial",
                        "invoice_line_ids": [
                            Command.create(
                                {
                                    "name": "Synthetic service for Send choice",
                                    "quantity": 1,
                                    "price_unit": 100,
                                    "account_id": data["default_account_revenue"].id,
                                    "tax_ids": [Command.clear()],
                                }
                            )
                        ],
                    }
                )
                invoice.action_post()
                invoice.env.flush_all()
                invoice.line_ids.invalidate_recordset()
                ledger_fields = [
                    "account_id",
                    "debit",
                    "credit",
                    "balance",
                    "amount_currency",
                    "write_uid",
                    "write_date",
                ]
                ledger = invoice.line_ids.read(ledger_fields)
                action = invoice.action_send_and_print()
                wizard = (
                    invoice.env[action["res_model"]]
                    .with_context(**action["context"])
                    .create({"move_id": invoice.id})
                )
                self.assertFalse(wizard.env.su)
                self.assertEqual(invoice.company_id.country_code, address)
                self.assertEqual(invoice.country_code, fiscal)
                self.assertEqual(wizard.l10n_ae_can_choose_send_format, can_choose)
                if can_choose:
                    with Form(wizard) as form:
                        form.l10n_ae_send_format = "pint"
                    self.assertEqual(wizard.invoice_edi_format, "kmee_ae_pint")
                    with Form(wizard) as form:
                        form.l10n_ae_send_format = "pdf_only"
                else:
                    wizard.write({"l10n_ae_send_format": "pint"})
                self.assertFalse(wizard.invoice_edi_format)
                self.assertEqual(invoice.state, "posted")
                self.assertEqual(invoice.amount_total, 100)
                self.assertFalse(invoice.l10n_ae_transmission_ids)
                self.assertFalse(invoice.l10n_ae_fiscal_uuid)
                self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertEqual(
            self.partner_a.read(["write_date", "invoice_edi_format"]), customer
        )

    def test_send_review_alerts_follow_fiscal_country_preserving_core_warnings(self):
        ae = self.env.ref("base.ae")
        fr = self.env.ref("base.fr")
        company_a = self.env.company
        company_a.partner_id.vat = False
        company_a.write(
            {"country_id": fr.id, "account_fiscal_country_id": ae.id, "state_id": False}
        )
        data_b = self.setup_other_company(name="Synthetic foreign fiscal Send alerts")
        data_b["company"].write(
            {
                "country_id": ae.id,
                "account_fiscal_country_id": fr.id,
                "l10n_ae_tin": "1000000003",
                "l10n_ae_tin_confirmed": True,
            }
        )
        self.simple_accountman.company_ids = [
            Command.set([company_a.id, data_b["company"].id])
        ]
        for data, address, fiscal, reviewed in (
            (self.company_data, "FR", "AE", True),
            (data_b, "AE", "FR", False),
        ):
            with self.subTest(address=address, fiscal=fiscal):
                company = data["company"]
                self.partner_a.with_company(company).write(
                    {
                        "invoice_edi_format": "kmee_ae_pint",
                        "invoice_sending_method": "email",
                        "email": False,
                    }
                )
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
                        "invoice_date": "2019-01-01",
                        "journal_id": data["default_journal_sale"].id,
                        "l10n_ae_document_category": "commercial",
                        "l10n_ae_free_zone": True,
                        "invoice_line_ids": [
                            Command.create(
                                {
                                    "name": "Synthetic beneficiary review service",
                                    "quantity": 1,
                                    "price_unit": 100,
                                    "account_id": data["default_account_revenue"].id,
                                    "tax_ids": [Command.clear()],
                                }
                            )
                        ],
                    }
                )
                invoice.action_post()
                invoice.env.flush_all()
                invoice.line_ids.invalidate_recordset()
                ledger_fields = [
                    "account_id",
                    "debit",
                    "credit",
                    "balance",
                    "amount_currency",
                    "write_uid",
                    "write_date",
                ]
                ledger = invoice.line_ids.read(ledger_fields)
                original = invoice.read(
                    [
                        "state",
                        "write_date",
                        "l10n_ae_fiscal_uuid",
                        "l10n_ae_transmission_ids",
                        "ubl_cii_xml_id",
                    ]
                )
                action = invoice.action_send_and_print()
                wizard = (
                    invoice.env[action["res_model"]]
                    .with_context(**action["context"])
                    .create({"move_id": invoice.id})
                )
                self.assertFalse(wizard.env.su)
                self.assertEqual(invoice.company_id.country_code, address)
                self.assertEqual(invoice.country_code, fiscal)
                self.assertEqual(wizard.invoice_edi_format, "kmee_ae_pint")
                wizard.write(
                    {
                        "sending_methods": ["email"],
                        "mail_partner_ids": [Command.set([self.partner_a.id])],
                    }
                )
                self.assertEqual(
                    wizard.alerts["account_missing_email"]["level"], "danger"
                )
                uae = {
                    k: v
                    for k, v in wizard.alerts.items()
                    if k.startswith("l10n_ae_edi_")
                }
                if reviewed:
                    self.assertEqual(set(uae), {"l10n_ae_edi_ibr_007_ae"})
                    alert = uae["l10n_ae_edi_ibr_007_ae"]
                    self.assertIn("IBR-007-AE", alert["message"])
                    self.assertEqual(alert["action"]["res_model"], "account.move")
                    self.assertEqual(alert["action"]["res_id"], invoice.id)
                else:
                    self.assertFalse(uae)
                self.assertEqual(invoice.read(list(original[0].keys())), original)
                self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
                self.assertEqual(invoice.amount_total, 100)

    def test_native_send_format_choice_survives_reopen_and_method_change(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        self.partner_a.write(
            {"invoice_sending_method": "email", "invoice_edi_format": False}
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        invoice.l10n_ae_free_zone = True
        invoice.action_post()
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "currency_id",
            "create_date",
            "write_date",
        ]
        ledger = invoice.line_ids.read(ledger_fields)
        contact = invoice.partner_id.read(["write_date", "invoice_edi_format"])
        action = invoice.action_send_and_print()
        wizard = (
            self.env[action["res_model"]]
            .with_user(self.simple_accountman)
            .with_context(**action["context"])
            .create({"move_id": invoice.id})
        )
        self.assertFalse(wizard.env.su)
        with Form(wizard) as form:
            form.l10n_ae_send_format = "pint"
        wizard_id = wizard.id
        self.env.flush_all()
        self.env.invalidate_all()
        wizard = wizard.browse(wizard_id)
        self.assertEqual(wizard.invoice_edi_format, "kmee_ae_pint")
        self.assertIn("l10n_ae_edi_ibr_007_ae", wizard.alerts)
        with self.cr.savepoint(), self.assertRaisesRegex(UserError, "IBR-007-AE"):
            wizard.action_send_and_print()
        with Form(wizard) as form:
            form.l10n_ae_send_format = "pdf_only"
        wizard.sending_methods = []
        self.env.flush_all()
        self.env.invalidate_all()
        wizard = wizard.browse(wizard_id)
        self.assertEqual(wizard.l10n_ae_send_format, "pdf_only")
        self.assertFalse(wizard.invoice_edi_format)
        self.assertFalse(
            {
                k: v
                for k, v in (wizard.alerts or {}).items()
                if k.startswith("l10n_ae_edi_")
            }
        )
        wizard.action_send_and_print()
        self.assertTrue(invoice.invoice_pdf_report_id.raw.content)
        self.assertFalse(invoice.ubl_cii_xml_id or invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertEqual(
            invoice.partner_id.read(["write_date", "invoice_edi_format"]), contact
        )

    def test_native_form_preserves_readonly_prepared_package_identity(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        unprepared = Form(invoice)
        self.assertFalse(unprepared.l10n_ae_normative_ruleset)
        self.assertFalse(unprepared.l10n_ae_normative_manifest_sha256)
        invoice.l10n_ae_fiscal_uuid = "15151515-1515-4515-8515-151515151515"
        invoice.l10n_ae_binding_id = self._reviewed_binding(
            {"name": "Native pinned package review"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_native_form_preserves_readonl"
            "y_prepared_package_identity.",
        )
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        frozen = (issue.payload, issue.xml_content, issue.idempotency_key)
        with patch.dict(
            os.environ,
            {"KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.3-ubl-2.1"},
        ):
            prepared = Form(invoice)
            self.assertEqual(
                prepared.l10n_ae_normative_ruleset, "kmee-pint-ae-1.0.4-ubl-2.1"
            )
            self.assertEqual(
                prepared.l10n_ae_normative_manifest_sha256,
                "4f225c8095a2f487c5813b4d0e6515c9e7f986ba1a36e34dca7ae305b7899d76",
            )
            with self.assertRaisesRegex(AssertionError, "readonly field"):
                prepared.l10n_ae_normative_ruleset = "kmee-pint-ae-1.0.3-ubl-2.1"
            with self.assertRaisesRegex(AssertionError, "readonly field"):
                prepared.l10n_ae_normative_manifest_sha256 = "0" * 64
        self.assertEqual(
            (issue.payload, issue.xml_content, issue.idempotency_key), frozen
        )
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.validation_receipt)
        self.assertEqual(invoice.amount_total, 105)

    def test_native_form_can_record_the_reviewed_non_peppol_export_buyer(self):
        self.partner_a.routing_endpoint = False
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=False
        )
        with Form(invoice) as form:
            form.l10n_ae_exports = True
            form.l10n_ae_export_buyer_no_peppol = True
            form.l10n_ae_buyer_onboarding_review_date = "2019-01-01"
            form.l10n_ae_buyer_onboarding_review_reference = (
                "SYNTH-NATIVE-EXPORT-REVIEW"
            )
        invoice.action_l10n_ae_validate()
        self.assertTrue(invoice.l10n_ae_exports)
        self.assertTrue(invoice.l10n_ae_export_buyer_no_peppol)
        self.assertEqual(
            invoice.l10n_ae_buyer_onboarding_review_reference,
            "SYNTH-NATIVE-EXPORT-REVIEW",
        )
        self.assertEqual((invoice.state, invoice.amount_total), ("draft", 105))
        self.assertFalse(invoice.l10n_ae_transmission_id)

    def test_native_invoice_form_can_review_agent_and_free_zone_identifiers(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        with Form(invoice) as form:
            form.l10n_ae_disclosed_agent = True
            form.l10n_ae_principal_id = "100000000300003"
            form.l10n_ae_free_zone = True
            form.l10n_ae_beneficiary_id = "1000000004"
        invoice.action_l10n_ae_validate()
        self.assertEqual(invoice.l10n_ae_principal_id, "100000000300003")
        xml = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        self.assertEqual(
            xml.findtext("cbc:ProfileExecutionID", namespaces=NS), "10000100"
        )
        self.assertEqual(
            xml.findtext(
                "cac:SellerSupplierParty/cac:Party/cac:PartyIdentification/cbc:ID",
                namespaces=NS,
            ),
            "100000000300003",
        )
        self.assertEqual(
            xml.findtext(
                "cac:BuyerCustomerParty/cac:Party/cac:PartyIdentification/cbc:ID",
                namespaces=NS,
            ),
            "1000000004",
        )

    @requires_normative_resources("1.0.4")
    def test_native_agent_credit_retains_original_identifiers_as_reviewable_defaults(
        self,
    ):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        original.write(
            {
                "l10n_ae_disclosed_agent": True,
                "l10n_ae_principal_id": "100000000300003",
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000004",
                "l10n_ae_fiscal_uuid": "aaaaaaaf-7777-4777-8777-777777777777",
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Prepared agent source for native reversal"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_native_agent_credit_r"
                    "etains_original_identifiers_as_reviewable_defaults.",
                ).id,
            }
        )
        original.action_l10n_ae_prepare()
        original_issue = original.l10n_ae_transmission_id
        original_xml = original_issue.xml_content
        self.assertTrue(original_issue)
        self.assertTrue(original.l10n_ae_fiscal_uuid)
        duplicate = original.copy()
        self.assertFalse(duplicate.l10n_ae_disclosed_agent)
        self.assertFalse(duplicate.l10n_ae_principal_id)
        self.assertFalse(duplicate.l10n_ae_free_zone)
        self.assertFalse(duplicate.l10n_ae_beneficiary_id)
        wizard = (
            self.env["account.move.reversal"]
            .with_context(active_model="account.move", active_ids=original.ids)
            .create(
                {
                    "reason": "Synthetic agent consideration reduction",
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                    "journal_id": original.journal_id.id,
                    "date": original.invoice_date,
                }
            )
        )
        wizard.refund_moves()
        credit = wizard.new_move_ids
        self.assertEqual((credit.state, credit.move_type), ("draft", "out_refund"))
        self.assertTrue(credit.l10n_ae_disclosed_agent)
        self.assertTrue(credit.l10n_ae_free_zone)
        self.assertEqual(credit.l10n_ae_principal_id, "100000000300003")
        self.assertEqual(credit.l10n_ae_beneficiary_id, "1000000004")
        self.assertFalse(credit.l10n_ae_fiscal_uuid)
        self.assertFalse(credit.l10n_ae_transmission_id)
        credit.l10n_ae_principal_id = "100000000500003"
        credit.invoice_line_ids.price_unit = 40
        credit.l10n_ae_fiscal_uuid = "aaaaaaaf-6666-4666-8666-666666666666"
        credit.action_post()
        xml = etree.fromstring(credit.preview_l10n_ae_pint().encode())
        self.assertEqual(
            xml.findtext("cbc:ProfileExecutionID", namespaces=NS), "10000100"
        )
        self.assertEqual(
            xml.findtext(
                "cac:SellerSupplierParty/cac:Party/cac:PartyIdentification/cbc:ID",
                namespaces=NS,
            ),
            "100000000500003",
        )
        self.assertTrue(credit.validate_l10n_ae_pint()["valid"])
        self.assertEqual((credit.amount_tax, credit.amount_total), (2, 42))
        self.assertEqual(original.l10n_ae_principal_id, "100000000300003")
        self.assertEqual(original.l10n_ae_transmission_id, original_issue)
        self.assertEqual(original.preview_l10n_ae_pint(), original_xml)

    @requires_normative_resources("1.0.4")
    def test_native_uae_states_emit_and_freeze_pint_emirate_codes(
        self,
    ):
        binding = self._reviewed_binding(
            {
                "name": "Synthetic reviewed emirate addresses",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_native_uae_states_emit_and_fr"
            "eeze_pint_emirate_codes.",
        )
        cases = (
            ("base.state_ae_az", "AUH", "Abu Dhabi"),
            ("base.state_ae_aj", "AJM", "Ajman"),
            ("base.state_ae_du", "DXB", "Dubai"),
            ("base.state_ae_fu", "FUJ", "Fujairah"),
            ("base.state_ae_rk", "RAK", "Ras al-Khaimah"),
            ("base.state_ae_sh", "SHJ", "Sharjah"),
            ("base.state_ae_uq", "UAQ", "Umm al-Quwain"),
        )
        for state_ref, code, label in cases:
            with self.subTest(emirate=code):
                state = self.env.ref(state_ref)
                self.partner_a.state_id = state
                invoice = self.init_invoice(
                    "out_invoice", amounts=[100], taxes=self.tax_sale_a
                ).with_user(self.simple_accountman)
                self.assertFalse(invoice.env.su)
                invoice.l10n_ae_binding_id = binding
                invoice.action_post()
                ledger_fields = [
                    "account_id",
                    "partner_id",
                    "currency_id",
                    "debit",
                    "credit",
                    "balance",
                    "amount_currency",
                    "tax_ids",
                    "write_date",
                ]
                ledger = invoice.line_ids.sorted("id").read(ledger_fields)
                invoice.action_l10n_ae_prepare()
                issue = invoice.l10n_ae_transmission_id
                frozen = issue.read(
                    [
                        "payload",
                        "payload_hash",
                        "xml_content",
                        "xml_hash",
                        "idempotency_key",
                    ]
                )
                tree = etree.fromstring(issue.xml_content.encode())
                for role, expected in (("Supplier", "DXB"), ("Customer", code)):
                    self.assertEqual(
                        tree.findtext(
                            f"cac:Accounting{role}Party/cac:Party/cac:PostalAddress/"
                            "cbc:CountrySubentity",
                            namespaces=NS,
                        ),
                        expected,
                    )
                snapshot = json.loads(issue.payload)
                self.assertEqual(snapshot["supplier"]["subdivision"], "DXB")
                self.assertEqual(snapshot["supplier"]["subdivision_name"], "Dubai")
                self.assertEqual(snapshot["buyer"]["subdivision"], code)
                self.assertEqual(snapshot["buyer"]["subdivision_name"], label)
                self.assertEqual(invoice.amount_untaxed, 100)
                self.assertEqual(invoice.amount_tax, 5)
                self.assertEqual(invoice.amount_total, 105)
                report = invoice.validate_l10n_ae_pint()
                self.assertTrue(report["valid"], report)
                self.partner_a.state_id = self.env.ref("base.state_ae_du")
                invoice.action_l10n_ae_prepare()
                self.assertEqual(
                    invoice.preview_l10n_ae_pint(), frozen[0]["xml_content"]
                )
                self.assertEqual(
                    issue.read(
                        [
                            "payload",
                            "payload_hash",
                            "xml_content",
                            "xml_hash",
                            "idempotency_key",
                        ]
                    ),
                    frozen,
                )
                self.assertEqual(
                    invoice.line_ids.sorted("id").read(ledger_fields), ledger
                )
                self.assertFalse(issue.attempt_ids)
                self.assertFalse(issue.event_ids)
                self.assertEqual(state.name, label)

    def test_native_form_shows_unverified_effective_aed_quote_and_frozen_source(self):
        usd = self.env.ref("base.USD")
        usd.active = True
        quote = self.env["res.currency.rate"].create(
            {
                "currency_id": usd.id,
                "company_id": self.env.company.id,
                "name": "2018-12-31",
                "rate": 1 / 3.6725,
            }
        )
        binding = self._reviewed_binding(
            {
                "name": "Synthetic native FX form",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_native_form_shows_unverified_"
            "effective_aed_quote_and_frozen_source.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd
        )
        invoice.l10n_ae_binding_id = binding
        invoice = invoice.with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        displayed = {
            "l10n_ae_effective_tax_exchange_rate": "3.672500",
            "l10n_ae_effective_tax_exchange_date": "2018-12-31",
            "l10n_ae_effective_tax_exchange_conversion_date": "2019-01-01",
            "l10n_ae_effective_tax_exchange_source": "Odoo currency rates: USD to AED",
            "l10n_ae_effective_tax_exchange_origin": "native_unverified",
            "l10n_ae_effective_tax_exchange_diagnostic": False,
        }
        form = Form(invoice)
        for name, expected in displayed.items():
            self.assertEqual(getattr(form, name), expected, name)
        with self.assertRaisesRegex(AssertionError, "readonly field"):
            form.l10n_ae_effective_tax_exchange_rate = "3.700000"
        for name in displayed:
            with self.assertRaisesRegex(UserError, "protected"):
                invoice.write({name: False})
        invoice.action_post()
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        frozen = issue.read(["payload", "payload_hash", "xml_content", "xml_hash"])
        quote.rate = 1 / 3.7
        prepared = Form(invoice)
        for name, expected in displayed.items():
            self.assertEqual(getattr(prepared, name), expected, name)
        invoice.action_l10n_ae_prepare()
        self.assertEqual(
            issue.read(["payload", "payload_hash", "xml_content", "xml_hash"]), frozen
        )
        self.assertEqual(invoice.amount_total, 105)
        self.assertEqual(invoice.amount_tax_signed, 18.36)
        self.assertEqual(invoice.amount_total_signed, 385.61)
        self.assertFalse(invoice.l10n_ae_tax_exchange_rate)
        self.assertFalse(invoice.l10n_ae_tax_exchange_date)
        self.assertFalse(invoice.l10n_ae_tax_exchange_source)

    @requires_normative_resources("1.0.4")
    def test_native_partial_commercial_credit_preserves_480_original_and_type_81(self):
        self.env.company.partner_id.vat = False
        self._assert_native_partial_commercial_credit()

    @requires_normative_resources("1.0.4")
    def test_native_self_billed_partial_credit_requires_reviewed_agreement_and_261(
        self,
    ):
        bill, agreement = self._selfbilling_bill_with_agreement()
        binding = bill.l10n_ae_binding_id
        binding.fiscal_uuid_policy = "controlled_local"
        review_controlled_binding(
            binding,
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note=(
                "Synthetic controlled self-billing invoice and credit contract; "
                "reviewed again after selecting controlled_local UUID policy."
            ),
        )
        bill.l10n_ae_fiscal_uuid = False
        self.env.company.write(
            {"l10n_ae_edi_auto_prepare": True, "l10n_ae_edi_binding_id": binding.id}
        )
        bill.action_post()
        original_evidence = (
            bill.l10n_ae_transmission_id.payload,
            bill.l10n_ae_transmission_id.xml_content,
        )
        wizard = (
            self.env["account.move.reversal"]
            .with_context(active_model="account.move", active_ids=bill.ids)
            .create(
                {
                    "reason": "Reviewed self-billed consideration reduction",
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                    "journal_id": bill.journal_id.id,
                    "date": bill.invoice_date,
                }
            )
        )
        self.assertTrue(wizard.l10n_ae_credit_reason_applicable)
        wizard.refund_moves()
        credit = wizard.new_move_ids
        self.assertEqual(
            (credit.state, credit.move_type, credit.is_self_billing),
            ("draft", "in_refund", True),
        )
        self.assertEqual(credit.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertFalse(credit.l10n_ae_selfbilling_agreement_id)
        credit.action_post()
        self.assertEqual(credit.state, "posted")
        self.assertEqual(credit.l10n_ae_preparation_state, "review")
        self.assertIn("self-billing agreement", credit.l10n_ae_preparation_error)
        self.assertFalse(credit.l10n_ae_fiscal_uuid)
        self.assertFalse(credit.l10n_ae_transmission_id)
        with self.assertRaisesRegex(UserError, "self-billing agreement"):
            credit.action_l10n_ae_prepare()
        credit.button_draft()
        self.assertEqual(credit.state, "draft")
        credit.l10n_ae_selfbilling_agreement_id = agreement
        credit.invoice_line_ids.price_unit = 40
        credit.action_post()
        report = credit.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["profile"], "selfbilling")
        frozen = credit.l10n_ae_transmission_id
        snapshot = json.loads(frozen.payload)
        self.assertEqual(snapshot["issuer_role"], "buyer")
        self.assertEqual(snapshot["supplier"]["vat"], "100000000200003")
        self.assertEqual(snapshot["buyer"]["vat"], "100000000100003")
        self.assertEqual(snapshot["selfbilling_agreement"]["reference"], agreement.name)
        tree = etree.fromstring(frozen.xml_content.encode())
        expected = {
            "cbc:CreditNoteTypeCode": "261",
            "cbc:CustomizationID": "urn:peppol:pint:selfbilling-1@ae-1",
            "cbc:ProfileID": "urn:peppol:bis:selfbilling",
            "cac:DiscrepancyResponse/cbc:ResponseCode": "DL8.61.1.C",
            "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID": bill.name,
            "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount": "40.00",
            "cac:TaxTotal/cbc:TaxAmount": "2.00",
            "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount": "42.00",
            "cac:LegalMonetaryTotal/cbc:PrepaidAmount": "42.00",
            "cac:LegalMonetaryTotal/cbc:PayableAmount": "0.00",
        }
        for path, value in expected.items():
            self.assertEqual(tree.findtext(path, namespaces=NS), value, path)
        self.assertEqual(
            (
                bill.amount_total,
                bill.amount_residual,
                credit.amount_total,
                credit.amount_residual,
            ),
            (105, 63, 42, 0),
        )
        self.assertNotEqual(credit.l10n_ae_fiscal_uuid, bill.l10n_ae_fiscal_uuid)
        self.assertEqual(
            (
                bill.l10n_ae_transmission_id.payload,
                bill.l10n_ae_transmission_id.xml_content,
            ),
            original_evidence,
        )

    def test_native_cancel_does_not_implicitly_correct_a_prepared_queued_issue(self):
        binding = self._reviewed_binding(
            {"name": "Synthetic native Cancel sandbox"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_native_cancel_does_not_implic"
            "itly_correct_a_prepared_queued_issue.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        ).with_user(self.simple_accountman)
        invoice.l10n_ae_binding_id = binding
        invoice.action_l10n_ae_prepare()
        original = invoice.l10n_ae_transmission_id
        with self.assertRaisesRegex(UserError, "Reconcile the frozen UAE"):
            invoice.button_cancel()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.l10n_ae_transmission_id, original)
        self.assertFalse(invoice.l10n_ae_correction_id)
        self.assertFalse(original.local_correction_id)
        self.assertFalse(original.attempt_ids)

    def test_native_reset_rolls_back_queued_history_when_accounting_refuses_batch(self):
        binding = self._reviewed_binding(
            {"name": "Synthetic native Reset accounting lock sandbox"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_native_reset_rolls_back_queue"
            "d_history_when_accounting_refuses_batch.",
        )
        invoices = self.env["account.move"]
        originals = []
        for amount in (100, 200):
            invoice = self.init_invoice(
                "out_invoice", amounts=[amount], taxes=self.tax_sale_a, post=True
            )
            invoice.l10n_ae_binding_id = binding
            invoice.action_l10n_ae_prepare()
            issue = invoice.l10n_ae_transmission_id
            originals.append((invoice, issue, issue.payload, issue.xml_content))
            invoices |= invoice
        invoices[-1].button_hash()
        self.assertTrue(invoices[-1].inalterable_hash)
        invoices = invoices.with_user(self.simple_accountman)
        self.assertFalse(invoices.env.su)
        with self.assertRaisesRegex(UserError, "locked journal entry"):
            invoices.button_draft()
        for invoice, issue, payload, xml in originals:
            self.assertEqual(invoice.state, "posted")
            self.assertEqual(invoice.l10n_ae_transmission_id, issue)
            self.assertEqual(invoice.l10n_ae_transmission_ids, issue)
            self.assertFalse(invoice.l10n_ae_correction_id)
            self.assertFalse(issue.local_correction_id)
            self.assertEqual((issue.payload, issue.xml_content), (payload, xml))
            self.assertFalse(issue.attempt_ids)

    @requires_normative_resources("1.0.4")
    def test_native_reset_preserves_queued_original_and_prepares_next_revision(self):
        binding = self._reviewed_binding(
            {
                "name": "Synthetic ordinary native Reset sandbox",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_native_reset_preserves_queued"
            "_original_and_prepares_next_revision.",
        )
        operator = new_test_user(
            self.env,
            login="synthetic-ordinary-native-reset",
            groups="base.group_user,account.group_account_invoice",
            company_id=self.env.company.id,
            company_ids=[self.env.company.id],
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        ).with_user(operator)
        self.assertFalse(invoice.env.su)
        self.assertFalse(operator.has_group("account.group_account_manager"))
        self.assertFalse(operator.has_group("base.group_system"))
        invoice.l10n_ae_binding_id = binding
        invoice.action_l10n_ae_prepare()
        source = invoice.l10n_ae_transmission_id
        original = source.read(
            [
                "payload",
                "payload_hash",
                "xml_content",
                "xml_hash",
                "idempotency_key",
                "revision",
                "submission_state",
                "remote_id",
                "attempt_ids",
                "event_ids",
                "validation_receipt",
            ]
        )
        fiscal_uuid = invoice.l10n_ae_fiscal_uuid
        self.assertEqual(invoice.amount_total, 105)
        self.assertEqual(source.submission_state, "queued")
        self.assertFalse(source.attempt_ids)
        with self.cr.savepoint(), self.assertRaisesRegex(UserError, "frozen UAE"):
            invoice.write({"state": "draft"})

        self.assertTrue(invoice.button_draft())
        self.assertEqual(invoice.state, "draft")
        self.assertFalse(invoice.l10n_ae_transmission_id)
        self.assertEqual(invoice.l10n_ae_transmission_ids, source)
        correction = invoice.l10n_ae_correction_id
        self.assertEqual(correction.source_transmission_id, source)
        self.assertEqual(source.local_correction_id, correction)
        self.assertEqual(correction.started_by, operator)
        self.assertTrue(correction.started_at)
        self.assertTrue(correction.reason)
        self.assertEqual(correction.state, "editing")
        self.assertEqual(invoice.l10n_ae_fiscal_uuid, fiscal_uuid)
        self.assertEqual(source.read(list(original[0].keys() - {"id"})), original)

        invoice.invoice_line_ids.filtered(
            lambda line: line.display_type == "product"
        ).write({"price_unit": 200})
        invoice.action_post()
        invoice.action_l10n_ae_prepare()
        replacement = invoice.l10n_ae_transmission_id
        self.assertNotEqual(replacement, source)
        self.assertEqual(replacement.revision, 2)
        self.assertNotEqual(replacement.idempotency_key, source.idempotency_key)
        self.assertEqual(correction.replacement_transmission_id, replacement)
        self.assertEqual(correction.state, "completed")
        self.assertTrue(correction.completed_at)
        self.assertEqual(invoice.l10n_ae_fiscal_uuid, fiscal_uuid)
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (200, 10, 210),
        )
        self.assertEqual(
            set(invoice.l10n_ae_transmission_ids.ids), {source.id, replacement.id}
        )
        self.assertEqual(source.read(list(original[0].keys() - {"id"})), original)
        self.assertFalse(replacement.attempt_ids)
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        xml = etree.fromstring(replacement.xml_content.encode())
        self.assertEqual(
            xml.findtext(
                "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", namespaces=NS
            ),
            "210.00",
        )
