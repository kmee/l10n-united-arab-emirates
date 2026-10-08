# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json

from lxml import etree

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaeSelfBillingInbound(AccountTestInvoicingCommon):
    def test_received_selfbilling_frequency_preserves_original_over_local_term(self):
        source, _agreement, binding = self._selfbilling_fixture()
        source.l10n_ae_billing_frequency_code = "MTH"
        term = self.env["account.payment.term"].create(
            {
                "name": "Synthetic local weekly payment term",
                "company_id": self.env.company.id,
                "l10n_ae_billing_frequency_code": "WKY",
                "line_ids": [Command.create({"value": "percent", "value_amount": 100})],
            }
        )
        self.buyer.partner_id.with_company(
            self.env.company
        ).property_payment_term_id = term
        content = source.preview_l10n_ae_pint().encode()
        tree = etree.fromstring(content)
        self.assertEqual(tree.findtext("./{*}InvoiceTypeCode"), "389")
        self.assertEqual(tree.findtext("./{*}InvoicePeriod/{*}DescriptionCode"), "MTH")
        manager = new_test_user(
            self.env,
            login="uae-received-frequency-manager",
            groups="account.group_account_manager",
            company_id=self.env.company.id,
            company_ids=[Command.set(self.env.company.ids)],
        )
        inbox_model = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(manager)
            .with_company(self.env.company)
        )
        self.assertFalse(inbox_model.env.su)
        inbox = inbox_model.receive_controlled_document(
            binding.id, "selfbilling-monthly-frequency", content
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        received = inbox.move_id
        self.assertEqual(received.invoice_payment_term_id, term)
        self.assertEqual(received.l10n_ae_billing_frequency_code, "MTH")
        self.assertEqual((received.move_type, received.state), ("out_invoice", "draft"))
        self.assertTrue(received.l10n_ae_received_document)
        self.assertFalse(received.l10n_ae_transmission_ids)
        self.assertEqual(
            (received.amount_untaxed, received.amount_tax, received.amount_total),
            (100, 5, 105),
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, hashlib.sha256(content).hexdigest())
        ledger_fields = [
            "account_id",
            "partner_id",
            "currency_id",
            "date",
            "date_maturity",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "create_date",
            "write_date",
        ]
        ledger = received.line_ids.sorted("id").read(ledger_fields)
        term.l10n_ae_billing_frequency_code = "DLY"
        with self.assertRaisesRegex(UserError, "received UAE"):
            received.write({"l10n_ae_billing_frequency_code": "WKY"})
        replay = inbox_model.receive_controlled_document(
            binding.id, "selfbilling-monthly-frequency", content
        )
        self.assertEqual(replay, inbox)
        self.assertTrue(replay.action_process(), replay.diagnostic)
        self.assertEqual(replay.move_id, received)
        self.assertEqual(received.l10n_ae_billing_frequency_code, "MTH")
        self.assertEqual(received.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertEqual(replay.original_xml.content, content)

    def test_margin_selfbilling_partial_credit_preserves_roles_and_originals(self):
        # Independently worked representation:1000/N5/VAT0 original,
        # quarter adjustment250. Agreements define buyer and supplier roles.
        supplier = self.env.company
        journal = self.buyer_data["default_journal_purchase"]
        journal.is_self_billing = True
        purchase_tax = self.buyer_data["default_tax_purchase"].copy(
            {
                "name": "Synthetic self-billed Margin purchase",
                "amount": 0,
                "ubl_cii_tax_category_code": "N",
                "l10n_ae_edi_additional_vat_rate": 5,
            }
        )
        sale_tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic self-billed Margin sale",
                "amount": 0,
                "ubl_cii_tax_category_code": "N",
                "l10n_ae_edi_additional_vat_rate": 5,
            }
        )
        buyer_agreement = self.env["l10n_ae.selfbilling.agreement"].create(
            {
                "name": "SYNTH-MARGIN-AGREEMENT",
                "company_id": self.buyer.id,
                "partner_id": supplier.partner_id.id,
                "start_date": "2026-01-01",
                "scope": "Synthetic reviewed representation and adjustments",
                "onboarding_reference": "SYNTH-MARGIN-SUPPLIER",
            }
        )
        supplier_agreement = self.env["l10n_ae.selfbilling.agreement"].create(
            {
                "name": "SYNTH-MARGIN-AGREEMENT",
                "company_id": supplier.id,
                "partner_id": self.buyer.partner_id.id,
                "local_role": "supplier",
                "start_date": "2026-01-01",
                "scope": "Synthetic reviewed representation and adjustments",
                "onboarding_reference": "SYNTH-MARGIN-BUYER",
            }
        )
        self.assertTrue(buyer_agreement.active and supplier_agreement.active)
        self.assertEqual(buyer_agreement.local_role, "buyer")
        self.assertEqual(supplier_agreement.local_role, "supplier")
        buyer_manager = new_test_user(
            self.env,
            login="uae-margin-selfbilling-buyer-manager",
            groups="account.group_account_manager",
            company_id=self.buyer.id,
            company_ids=[Command.set(self.buyer.ids)],
        )
        source_binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic Margin self-billing issuer",
                "company_id": self.buyer.id,
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        receiver_binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic Margin supplier receiver"}
        )
        original = (
            self.env["account.move"]
            .with_user(buyer_manager)
            .with_company(self.buyer)
            .create(
                {
                    "move_type": "in_invoice",
                    "journal_id": journal.id,
                    "partner_id": supplier.partner_id.id,
                    "invoice_date": "2026-10-02",
                    "l10n_ae_binding_id": source_binding.id,
                    "l10n_ae_selfbilling_agreement_id": buyer_agreement.id,
                    "l10n_ae_margin_scheme": True,
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Synthetic reviewed Margin representation",
                                "price_unit": 1000,
                                "account_id": self.buyer_data[
                                    "default_account_expense"
                                ].id,
                                "tax_ids": [Command.set(purchase_tax.ids)],
                            }
                        )
                    ],
                }
            )
        )
        self.assertFalse(original.env.su)
        original.action_post()
        original.action_l10n_ae_prepare()
        report = original.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        original_xml = original.preview_l10n_ae_pint().encode()
        original_tree = etree.fromstring(original_xml)
        self.assertEqual(original_tree.findtext("./{*}InvoiceTypeCode"), "389")
        self.assertEqual(original_tree.findtext("./{*}ProfileExecutionID"), "00100000")
        self.assertEqual(
            original_tree.findtext("./{*}CustomizationID"),
            "urn:peppol:pint:selfbilling-1@ae-1",
        )
        self.assertEqual(
            original_tree.findtext(
                "./{*}AccountingSupplierParty/{*}Party/{*}EndpointID"
            ),
            "1000000001",
        )
        self.assertEqual(
            original_tree.findtext(
                "./{*}AccountingCustomerParty/{*}Party/{*}EndpointID"
            ),
            "1000000006",
        )
        inboxes = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        received_original = inboxes.receive_controlled_document(
            receiver_binding.id, "margin-selfbilling-original", original_xml
        )
        self.assertTrue(
            received_original.action_process(), received_original.diagnostic
        )
        sale = received_original.move_id
        self.assertEqual((sale.move_type, sale.state), ("out_invoice", "draft"))
        self.assertEqual(sale.company_id, supplier)
        self.assertEqual(sale.partner_id, self.buyer.partner_id)
        self.assertEqual(sale.l10n_ae_selfbilling_agreement_id, supplier_agreement)
        self.assertEqual(sale.l10n_ae_received_process, "selfbilling")
        self.assertTrue(sale.l10n_ae_margin_scheme)
        self.assertEqual(sale.invoice_line_ids.tax_ids, sale_tax)
        self.assertEqual(
            (sale.amount_untaxed, sale.amount_tax, sale.amount_total), (1000, 0, 1000)
        )
        self.assertFalse(sale.l10n_ae_transmission_ids)
        reversal = (
            self.env["account.move.reversal"]
            .with_user(buyer_manager)
            .with_company(self.buyer)
            .with_context(active_model="account.move", active_ids=original.ids)
            .create(
                {
                    "move_ids": [Command.set(original.ids)],
                    "journal_id": journal.id,
                    "date": "2026-10-03",
                    "reason": "Synthetic quarter adjustment",
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                }
            )
        )
        reversal.refund_moves()
        credit = reversal.new_move_ids
        self.assertFalse(credit.env.su)
        self.assertEqual(credit.move_type, "in_refund")
        self.assertTrue(credit.l10n_ae_margin_scheme)
        # A new credit deliberately requires fresh agreement/binding review.
        self.assertFalse(credit.l10n_ae_selfbilling_agreement_id)
        self.assertEqual(credit.reversed_entry_id, original)
        credit.invoice_line_ids.write({"quantity": 0.25})
        credit.write(
            {
                "l10n_ae_binding_id": source_binding.id,
                "l10n_ae_selfbilling_agreement_id": buyer_agreement.id,
            }
        )
        credit.action_post()
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "tax_ids",
            "create_date",
            "write_date",
        ]
        original_ledger = original.line_ids.read(ledger_fields)
        credit_ledger = credit.line_ids.read(ledger_fields)
        sale_ledger = sale.line_ids.read(ledger_fields)
        credit.action_l10n_ae_prepare()
        report = credit.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        content = credit.preview_l10n_ae_pint().encode()
        tree = etree.fromstring(content)
        self.assertEqual(tree.findtext("./{*}CreditNoteTypeCode"), "261")
        self.assertEqual(
            tree.findtext("./{*}CustomizationID"),
            "urn:peppol:pint:selfbilling-1@ae-1",
        )
        self.assertEqual(
            tree.findtext("./{*}AccountingSupplierParty/{*}Party/{*}EndpointID"),
            "1000000001",
        )
        self.assertEqual(
            tree.findtext("./{*}AccountingCustomerParty/{*}Party/{*}EndpointID"),
            "1000000006",
        )
        self.assertEqual(tree.findtext("./{*}ProfileExecutionID"), "00100000")
        self.assertEqual(
            tree.findtext("./{*}BillingReference/{*}InvoiceDocumentReference/{*}ID"),
            original.name,
        )
        self.assertEqual(
            tree.findtext("./{*}DiscrepancyResponse/{*}ResponseCode"), "DL8.61.1.C"
        )
        self.assertEqual(
            tree.findtext("./{*}TaxTotal/{*}TaxSubtotal/{*}TaxCategory/{*}ID"), "N"
        )
        self.assertEqual(
            float(
                tree.findtext("./{*}TaxTotal/{*}TaxSubtotal/{*}TaxCategory/{*}Percent")
            ),
            5,
        )
        self.assertEqual(float(tree.findtext("./{*}TaxTotal/{*}TaxAmount")), 0)
        self.assertEqual(
            float(tree.findtext("./{*}LegalMonetaryTotal/{*}TaxInclusiveAmount")), 250
        )
        received_credit = inboxes.receive_controlled_document(
            receiver_binding.id, "margin-selfbilling-quarter", content
        )
        self.assertTrue(received_credit.action_process(), received_credit.diagnostic)
        refund = received_credit.move_id
        self.assertEqual((refund.move_type, refund.state), ("out_refund", "draft"))
        self.assertEqual(refund.company_id, supplier)
        self.assertEqual(refund.partner_id, self.buyer.partner_id)
        self.assertEqual(refund.reversed_entry_id, sale)
        self.assertEqual(refund.l10n_ae_selfbilling_agreement_id, supplier_agreement)
        self.assertEqual(refund.l10n_ae_received_process, "selfbilling")
        self.assertEqual(refund.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertTrue(refund.l10n_ae_margin_scheme)
        self.assertEqual(refund.invoice_line_ids.tax_ids, sale_tax)
        self.assertEqual(
            (refund.amount_untaxed, refund.amount_tax, refund.amount_total),
            (250, 0, 250),
        )
        self.assertFalse(refund.l10n_ae_transmission_ids)
        refund_ledger = refund.line_ids.read(ledger_fields)
        purchase_tax.l10n_ae_edi_additional_vat_rate = 7
        sale_tax.l10n_ae_edi_additional_vat_rate = 7
        replay = inboxes.receive_controlled_document(
            receiver_binding.id, "margin-selfbilling-quarter", content
        )
        self.assertEqual(replay, received_credit)
        self.assertTrue(replay.action_process(), replay.diagnostic)
        self.assertEqual(replay.move_id, refund)
        self.assertEqual(received_original.original_xml.content, original_xml)
        self.assertEqual(received_credit.original_xml.content, content)
        self.assertEqual(received_credit.xml_hash, hashlib.sha256(content).hexdigest())
        self.assertEqual(original.preview_l10n_ae_pint().encode(), original_xml)
        self.assertEqual(credit.preview_l10n_ae_pint().encode(), content)
        for move, ledger in [
            (original, original_ledger),
            (credit, credit_ledger),
            (sale, sale_ledger),
            (refund, refund_ledger),
        ]:
            self.assertEqual(move.line_ids.read(ledger_fields), ledger)
        self.assertEqual((purchase_tax.amount, sale_tax.amount), (0, 0))

    def test_free_zone_margin_credit_roundtrip_preserves_beneficiary_and_originals(
        self,
    ):
        # Independent partial adjustment:1000 original,250 credit, no disclosed
        # VAT, nominal N5. Native receivable reconciliation remains core-owned.
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic margin credit source",
                "amount": 0,
                "ubl_cii_tax_category_code": "N",
                "l10n_ae_edi_additional_vat_rate": 5,
            }
        )
        recipient_tax = self.buyer_data["default_tax_purchase"].copy(
            {
                "name": "Synthetic margin credit purchase",
                "amount": 0,
                "ubl_cii_tax_category_code": "N",
                "l10n_ae_edi_additional_vat_rate": 5,
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic margin credit issuer",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        original = self.init_invoice(
            "out_invoice",
            partner=self.buyer.partner_id,
            invoice_date="2026-10-02",
            amounts=[1000],
            taxes=tax,
        ).with_user(self.simple_accountman)
        original.write(
            {
                "l10n_ae_margin_scheme": True,
                "l10n_ae_binding_id": binding.id,
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000003",
            }
        )
        original.action_post()
        original.action_l10n_ae_prepare()
        self.assertTrue(original.validate_l10n_ae_pint()["valid"])
        original_xml = original.preview_l10n_ae_pint().encode()
        original_tree = etree.fromstring(original_xml)
        self.assertEqual(original_tree.findtext("./{*}ProfileExecutionID"), "10100000")
        self.assertEqual(
            original_tree.findtext(
                "./{*}BuyerCustomerParty/{*}Party/{*}PartyIdentification/{*}ID"
            ),
            "1000000003",
        )
        manager = new_test_user(
            self.env,
            login="uae-free-zone-margin-recipient",
            groups="account.group_account_manager",
            company_id=self.buyer.id,
            company_ids=[Command.set(self.buyer.ids)],
        )
        receiver_binding = (
            self.env["l10n_ae.edi.binding"]
            .with_company(self.buyer)
            .create(
                {
                    "name": "Synthetic margin credit receiver",
                    "company_id": self.buyer.id,
                }
            )
        )
        inboxes = (
            self.env["l10n_ae.edi.inbox"].with_user(manager).with_company(self.buyer)
        )
        received_original = inboxes.receive_controlled_document(
            receiver_binding.id,
            "free-zone-margin-original",
            original_xml,
        )
        self.assertTrue(
            received_original.action_process(), received_original.diagnostic
        )
        bill = received_original.move_id
        self.assertEqual(bill.ref, original.name)
        self.assertTrue(bill.l10n_ae_free_zone)
        self.assertTrue(bill.l10n_ae_margin_scheme)
        self.assertEqual(bill.l10n_ae_beneficiary_id, "1000000003")
        reversal = (
            self.env["account.move.reversal"]
            .with_user(self.simple_accountman)
            .with_context(
                active_model="account.move",
                active_ids=original.ids,
                allowed_company_ids=[self.env.company.id],
            )
            .create(
                {
                    "move_ids": [Command.set(original.ids)],
                    "journal_id": original.journal_id.id,
                    "date": "2026-10-03",
                    "reason": "Synthetic quarter adjustment",
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                }
            )
        )
        reversal.refund_moves()
        credit = reversal.new_move_ids
        self.assertFalse(credit.env.su)
        self.assertTrue(credit.l10n_ae_margin_scheme)
        self.assertTrue(credit.l10n_ae_free_zone)
        self.assertEqual(credit.l10n_ae_beneficiary_id, "1000000003")
        self.assertEqual(credit.reversed_entry_id, original)
        credit.invoice_line_ids.write({"quantity": 0.25})
        credit.l10n_ae_binding_id = binding
        credit.action_post()
        self.assertEqual(
            (credit.amount_untaxed, credit.amount_tax, credit.amount_total),
            (250, 0, 250),
        )
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "tax_ids",
            "create_date",
            "write_date",
        ]
        original_ledger = original.line_ids.read(ledger_fields)
        credit_ledger = credit.line_ids.read(ledger_fields)
        bill_ledger = bill.line_ids.read(ledger_fields)
        credit.action_l10n_ae_prepare()
        report = credit.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        content = credit.preview_l10n_ae_pint().encode()
        tree = etree.fromstring(content)
        self.assertEqual(tree.findtext("./{*}CreditNoteTypeCode"), "381")
        self.assertEqual(tree.findtext("./{*}ProfileExecutionID"), "10100000")
        self.assertEqual(
            tree.findtext(
                "./{*}BuyerCustomerParty/{*}Party/{*}PartyIdentification/{*}ID"
            ),
            "1000000003",
        )
        self.assertEqual(
            tree.findtext("./{*}BillingReference/{*}InvoiceDocumentReference/{*}ID"),
            original.name,
        )
        self.assertEqual(
            tree.findtext("./{*}DiscrepancyResponse/{*}ResponseCode"), "DL8.61.1.C"
        )
        received_credit = inboxes.receive_controlled_document(
            receiver_binding.id,
            "free-zone-margin-quarter",
            content,
        )
        self.assertTrue(received_credit.action_process(), received_credit.diagnostic)
        refund = received_credit.move_id
        self.assertEqual((refund.state, refund.move_type), ("draft", "in_refund"))
        self.assertEqual(refund.reversed_entry_id, bill)
        self.assertEqual(refund.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertTrue(refund.l10n_ae_margin_scheme)
        self.assertTrue(refund.l10n_ae_free_zone)
        self.assertEqual(refund.l10n_ae_beneficiary_id, "1000000003")
        self.assertEqual(refund.invoice_line_ids.tax_ids, recipient_tax)
        self.assertEqual(
            (refund.amount_untaxed, refund.amount_tax, refund.amount_total),
            (250, 0, 250),
        )
        self.assertFalse(refund.l10n_ae_transmission_ids)
        refund_ledger = refund.line_ids.read(ledger_fields)
        tax.l10n_ae_edi_additional_vat_rate = 7
        recipient_tax.l10n_ae_edi_additional_vat_rate = 7
        replay = inboxes.receive_controlled_document(
            receiver_binding.id,
            "free-zone-margin-quarter",
            content,
        )
        self.assertEqual(replay, received_credit)
        self.assertTrue(replay.action_process(), replay.diagnostic)
        self.assertEqual(replay.move_id, refund)
        self.assertEqual(received_original.original_xml.content, original_xml)
        self.assertEqual(received_credit.original_xml.content, content)
        self.assertEqual(received_credit.xml_hash, hashlib.sha256(content).hexdigest())
        self.assertEqual(original.preview_l10n_ae_pint().encode(), original_xml)
        self.assertEqual(credit.preview_l10n_ae_pint().encode(), content)
        for move, ledger in [
            (original, original_ledger),
            (credit, credit_ledger),
            (bill, bill_ledger),
            (refund, refund_ledger),
        ]:
            self.assertEqual(move.line_ids.read(ledger_fields), ledger)
        self.assertEqual((tax.amount, recipient_tax.amount), (0, 0))

    def test_margin_credit_reversal_and_received_reference_preserve_originals(self):
        # Independent partial adjustment:1000 original,250 credit, no disclosed
        # VAT, nominal N5. Native receivable reconciliation remains core-owned.
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic margin credit source",
                "amount": 0,
                "ubl_cii_tax_category_code": "N",
                "l10n_ae_edi_additional_vat_rate": 5,
            }
        )
        recipient_tax = self.buyer_data["default_tax_purchase"].copy(
            {
                "name": "Synthetic margin credit purchase",
                "amount": 0,
                "ubl_cii_tax_category_code": "N",
                "l10n_ae_edi_additional_vat_rate": 5,
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic margin credit issuer",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        original = self.init_invoice(
            "out_invoice",
            partner=self.buyer.partner_id,
            invoice_date="2026-10-02",
            amounts=[1000],
            taxes=tax,
        ).with_user(self.simple_accountman)
        original.write(
            {"l10n_ae_margin_scheme": True, "l10n_ae_binding_id": binding.id}
        )
        original.action_post()
        original.action_l10n_ae_prepare()
        self.assertTrue(original.validate_l10n_ae_pint()["valid"])
        original_xml = original.preview_l10n_ae_pint().encode()
        manager = new_test_user(
            self.env,
            login="uae-margin-credit-recipient",
            groups="account.group_account_manager",
            company_id=self.buyer.id,
            company_ids=[Command.set(self.buyer.ids)],
        )
        receiver_binding = (
            self.env["l10n_ae.edi.binding"]
            .with_company(self.buyer)
            .create(
                {
                    "name": "Synthetic margin credit receiver",
                    "company_id": self.buyer.id,
                }
            )
        )
        inboxes = (
            self.env["l10n_ae.edi.inbox"].with_user(manager).with_company(self.buyer)
        )
        received_original = inboxes.receive_controlled_document(
            receiver_binding.id,
            "margin-credit-original",
            original_xml,
        )
        self.assertTrue(
            received_original.action_process(), received_original.diagnostic
        )
        bill = received_original.move_id
        self.assertEqual(bill.ref, original.name)
        reversal = (
            self.env["account.move.reversal"]
            .with_user(self.simple_accountman)
            .with_context(
                active_model="account.move",
                active_ids=original.ids,
                allowed_company_ids=[self.env.company.id],
            )
            .create(
                {
                    "move_ids": [Command.set(original.ids)],
                    "journal_id": original.journal_id.id,
                    "date": "2026-10-03",
                    "reason": "Synthetic quarter adjustment",
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                }
            )
        )
        reversal.refund_moves()
        credit = reversal.new_move_ids
        self.assertFalse(credit.env.su)
        self.assertTrue(credit.l10n_ae_margin_scheme)
        self.assertEqual(credit.reversed_entry_id, original)
        credit.invoice_line_ids.write({"quantity": 0.25})
        credit.l10n_ae_binding_id = binding
        credit.action_post()
        self.assertEqual(
            (credit.amount_untaxed, credit.amount_tax, credit.amount_total),
            (250, 0, 250),
        )
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "tax_ids",
            "create_date",
            "write_date",
        ]
        original_ledger = original.line_ids.read(ledger_fields)
        credit_ledger = credit.line_ids.read(ledger_fields)
        bill_ledger = bill.line_ids.read(ledger_fields)
        credit.action_l10n_ae_prepare()
        report = credit.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        content = credit.preview_l10n_ae_pint().encode()
        tree = etree.fromstring(content)
        self.assertEqual(tree.findtext("./{*}CreditNoteTypeCode"), "381")
        self.assertEqual(tree.findtext("./{*}ProfileExecutionID"), "00100000")
        self.assertEqual(
            tree.findtext("./{*}BillingReference/{*}InvoiceDocumentReference/{*}ID"),
            original.name,
        )
        self.assertEqual(
            tree.findtext("./{*}DiscrepancyResponse/{*}ResponseCode"), "DL8.61.1.C"
        )
        received_credit = inboxes.receive_controlled_document(
            receiver_binding.id,
            "margin-credit-quarter",
            content,
        )
        self.assertTrue(received_credit.action_process(), received_credit.diagnostic)
        refund = received_credit.move_id
        self.assertEqual((refund.state, refund.move_type), ("draft", "in_refund"))
        self.assertEqual(refund.reversed_entry_id, bill)
        self.assertEqual(refund.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertTrue(refund.l10n_ae_margin_scheme)
        self.assertEqual(refund.invoice_line_ids.tax_ids, recipient_tax)
        self.assertEqual(
            (refund.amount_untaxed, refund.amount_tax, refund.amount_total),
            (250, 0, 250),
        )
        self.assertFalse(refund.l10n_ae_transmission_ids)
        refund_ledger = refund.line_ids.read(ledger_fields)
        tax.l10n_ae_edi_additional_vat_rate = 7
        recipient_tax.l10n_ae_edi_additional_vat_rate = 7
        replay = inboxes.receive_controlled_document(
            receiver_binding.id,
            "margin-credit-quarter",
            content,
        )
        self.assertEqual(replay, received_credit)
        self.assertTrue(replay.action_process(), replay.diagnostic)
        self.assertEqual(replay.move_id, refund)
        self.assertEqual(received_original.original_xml.content, original_xml)
        self.assertEqual(received_credit.original_xml.content, content)
        self.assertEqual(received_credit.xml_hash, hashlib.sha256(content).hexdigest())
        self.assertEqual(original.preview_l10n_ae_pint().encode(), original_xml)
        self.assertEqual(credit.preview_l10n_ae_pint().encode(), content)
        for move, ledger in [
            (original, original_ledger),
            (credit, credit_ledger),
            (bill, bill_ledger),
            (refund, refund_ledger),
        ]:
            self.assertEqual(move.line_ids.read(ledger_fields), ledger)
        self.assertEqual((tax.amount, recipient_tax.amount), (0, 0))

    def test_received_margin_n_matches_reviewed_nominal_metadata_not_native_rate(self):
        # Independent PINT example: net1000, nominal N5, disclosed VAT0,
        # payable1000. This is representation, not margin liability accounting.
        source_tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic source margin representation",
                "amount": 0,
                "ubl_cii_tax_category_code": "N",
                "l10n_ae_edi_additional_vat_rate": 5,
            }
        )
        source = self.init_invoice(
            "out_invoice",
            partner=self.buyer.partner_id,
            invoice_date="2026-10-02",
            amounts=[1000],
            taxes=source_tax,
        )
        source.write(
            {
                "l10n_ae_margin_scheme": True,
                "l10n_ae_fiscal_uuid": "aabbccdd-1111-4111-8111-111111111111",
            }
        )
        source.action_post()
        content = source.preview_l10n_ae_pint().encode()
        self.assertEqual((source.amount_tax, source.amount_total), (0, 1000))
        recipient_tax = self.buyer_data["default_tax_purchase"].copy(
            {
                "name": "Synthetic reviewed N nominal5 native0",
                "amount": 0,
                "ubl_cii_tax_category_code": "N",
                "l10n_ae_edi_additional_vat_rate": 5,
            }
        )
        decoy = recipient_tax.copy(
            {"name": "Synthetic native5 must not match nominal5", "amount": 5}
        )
        self.buyer.account_purchase_tax_id = decoy
        recipient_tax.copy(
            {
                "name": "Synthetic nominal7 must not match nominal5",
                "l10n_ae_edi_additional_vat_rate": 7,
            }
        )
        manager = new_test_user(
            self.env,
            login="uae-margin-receiving-manager",
            groups="account.group_account_manager",
            company_id=self.buyer.id,
            company_ids=[Command.set(self.buyer.ids)],
        )
        binding = (
            self.env["l10n_ae.edi.binding"]
            .with_company(self.buyer)
            .create({"name": "Synthetic margin recipient", "company_id": self.buyer.id})
        )
        inbox_model = (
            self.env["l10n_ae.edi.inbox"].with_user(manager).with_company(self.buyer)
        )
        inbox = inbox_model.receive_controlled_document(
            binding.id, "synthetic-margin-nominal5", content
        )
        self.assertFalse(inbox.env.su)
        # Neither the nominal7 zero tax nor the native5 default may stand in
        # for the missing nominal5/native0 reviewed purchase tax.
        recipient_tax.l10n_ae_edi_additional_vat_rate = 7
        before = self.env["account.move"].search_count(
            [("company_id", "=", self.buyer.id)]
        )
        self.assertFalse(inbox.action_process())
        self.assertEqual(inbox.state, "review")
        self.assertIn("tax category requires master data review", inbox.diagnostic)
        self.assertFalse(inbox.move_id)
        self.assertEqual(
            self.env["account.move"].search_count([("company_id", "=", self.buyer.id)]),
            before,
        )
        self.assertEqual(inbox.original_xml.content, content)
        recipient_tax.l10n_ae_edi_additional_vat_rate = 5
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        bill = inbox.move_id
        self.assertEqual(
            (bill.company_id, bill.partner_id),
            (self.buyer, self.env.company.partner_id),
        )
        self.assertEqual((bill.move_type, bill.state), ("in_invoice", "draft"))
        self.assertEqual(
            (bill.amount_untaxed, bill.amount_tax, bill.amount_total), (1000, 0, 1000)
        )
        self.assertEqual(bill.invoice_line_ids.tax_ids, recipient_tax)
        self.assertTrue(bill.l10n_ae_margin_scheme)
        self.assertTrue(bill.l10n_ae_received_document)
        self.assertFalse(bill.l10n_ae_transmission_ids)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, hashlib.sha256(content).hexdigest())
        with self.assertRaisesRegex(UserError, "cannot be emitted again"):
            bill.preview_l10n_ae_pint()
        self.assertEqual(inbox.original_xml.content, content)
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "tax_ids",
            "create_date",
            "write_date",
        ]
        ledger = bill.line_ids.read(ledger_fields)
        recipient_tax.l10n_ae_edi_additional_vat_rate = 7
        replay = inbox_model.receive_controlled_document(
            binding.id, "synthetic-margin-nominal5", content
        )
        self.assertEqual(replay, inbox)
        self.assertTrue(replay.action_process(), replay.diagnostic)
        self.assertEqual(replay.move_id, bill)
        self.assertEqual(bill.line_ids.read(ledger_fields), ledger)
        with self.assertRaisesRegex(UserError, "cannot be emitted again"):
            bill.preview_l10n_ae_pint()
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual((recipient_tax.amount, decoy.amount), (0, 5))

    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.buyer_data = cls.setup_other_company(name="Synthetic self-billing buyer")
        cls.buyer = cls.buyer_data["company"]
        for company, tin, license_number in (
            (cls.env.company, "1000000001", "SYNTH-SUPPLIER-TL-1"),
            (cls.buyer, "1000000006", "SYNTH-BUYER-TL-6"),
        ):
            company.write({"l10n_ae_tin": tin, "l10n_ae_tin_confirmed": True})
            company.partner_id.write(
                {
                    "vat": tin + "00003",
                    "street": "Synthetic UAE street",
                    "city": "Dubai",
                    "state_id": cls.env.ref("base.state_ae_du").id,
                    "routing_scheme": "0235",
                    "routing_endpoint": tin,
                    "l10n_ae_registration_type": "TL",
                    "l10n_ae_registration_authority": "Synthetic Authority",
                    "additional_identifiers": {"AE_TL": license_number},
                }
            )

    def _selfbilling_fixture(self):
        supplier = self.env.company
        journal = self.buyer_data["default_journal_purchase"]
        journal.is_self_billing = True
        source_agreement = self.env["l10n_ae.selfbilling.agreement"].create(
            {
                "name": "SYNTH-AGREEMENT-6-1",
                "company_id": self.buyer.id,
                "partner_id": supplier.partner_id.id,
                "start_date": "2026-01-01",
                "scope": "Synthetic services and adjustments",
                "onboarding_reference": "SYNTH-SUPPLIER-ONBOARD-1",
            }
        )
        recipient_agreement = self.env["l10n_ae.selfbilling.agreement"].create(
            {
                "name": "SYNTH-AGREEMENT-6-1",
                "company_id": supplier.id,
                "partner_id": self.buyer.partner_id.id,
                "local_role": "supplier",
                "start_date": "2026-01-01",
                "scope": "Synthetic services and adjustments",
                "onboarding_reference": "SYNTH-BUYER-ONBOARD-6",
            }
        )
        source = (
            self.env["account.move"]
            .with_company(self.buyer)
            .create(
                {
                    "move_type": "in_invoice",
                    "journal_id": journal.id,
                    "partner_id": supplier.partner_id.id,
                    "invoice_date": "2026-10-02",
                    "l10n_ae_selfbilling_agreement_id": source_agreement.id,
                    "l10n_ae_fiscal_uuid": "eeeeeeea-1111-4111-8111-111111111111",
                    "invoice_line_ids": [
                        (
                            0,
                            0,
                            {
                                "name": "Synthetic services",
                                "price_unit": 100,
                                "account_id": self.buyer_data[
                                    "default_account_expense"
                                ].id,
                                "tax_ids": [
                                    (6, 0, self.buyer_data["default_tax_purchase"].ids)
                                ],
                            },
                        )
                    ],
                }
            )
        )
        source.action_post()
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Supplier receiving binding",
                "company_id": supplier.id,
            }
        )
        return source, recipient_agreement, binding

    def test_received_invalid_emirate_retains_original_and_explains_local_review(self):
        source, _agreement, binding = self._selfbilling_fixture()
        company = self.env.company
        manager = new_test_user(
            self.env,
            login="uae-inbound-emirate-manager",
            groups="account.group_account_manager",
            company_id=company.id,
            company_ids=[(6, 0, company.ids)],
        )
        self.assertFalse(manager.has_group("base.group_system"))
        namespaces = {
            "cac": (
                "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
            ),
            "cbc": (
                "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
            ),
        }
        root = etree.fromstring(source.preview_l10n_ae_pint().encode())
        subdivision = root.find(
            "cac:AccountingSupplierParty/cac:Party/cac:PostalAddress/cbc:CountrySubentity",
            namespaces,
        )
        self.assertEqual(subdivision.text, "DXB")
        subdivision.text = "Dubai"
        content = etree.tostring(root)
        inbox_model = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(manager)
            .with_context(allowed_company_ids=company.ids)
        )
        moves = (
            self.env["account.move"]
            .with_user(manager)
            .with_context(allowed_company_ids=company.ids)
        )
        domain = [("company_id", "=", company.id), ("move_type", "=", "out_invoice")]
        before = moves.search_count(domain)
        inbox = inbox_model.receive_controlled_document(
            binding.id, "invalid-emirate-received-selfbilling", content
        )
        self.assertFalse(inbox.action_process())
        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)
        self.assertEqual(moves.search_count(domain), before)
        self.assertIn("ibr-128-ae", inbox.diagnostic)
        self.assertIn("valid emirate code", inbox.diagnostic)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, hashlib.sha256(content).hexdigest())
        history = inbox.processing_history_ids
        self.assertEqual(len(history), 1)
        self.assertEqual(history.operator_id, manager)
        self.assertEqual(history.outcome, "review")
        report = json.loads(history.validation_report)
        self.assertFalse(report["valid"])
        self.assertTrue(report["xsd_valid"])
        self.assertTrue(all(not layer["failures"] for layer in report["layers"]))
        self.assertEqual(
            [f["code"] for f in report["supplemental_checks"][0]["failures"]],
            ["ibr-128-ae"],
        )
        self.assertEqual(
            hashlib.sha256(history.validation_report.encode()).hexdigest(),
            history.validation_report_hash,
        )

    def test_received_blank_agreement_evidence_requires_review_and_repaired_retry(self):
        source, agreement, binding = self._selfbilling_fixture()
        company = self.env.company
        manager = new_test_user(
            self.env,
            login="uae-inbound-agreement-manager",
            groups="account.group_account_manager",
            company_id=company.id,
            company_ids=[(6, 0, company.ids)],
        )
        self.assertFalse(manager.has_group("base.group_system"))
        self.assertEqual(manager.company_ids, company)
        agreement = agreement.with_user(manager).with_context(
            allowed_company_ids=company.ids
        )
        inbox_model = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(manager)
            .with_context(allowed_company_ids=company.ids)
        )
        moves = (
            self.env["account.move"]
            .with_user(manager)
            .with_context(allowed_company_ids=company.ids)
        )
        domain = [("company_id", "=", company.id), ("move_type", "=", "out_invoice")]
        before = moves.search_count(domain)
        content = source.preview_l10n_ae_pint().encode()
        digest = hashlib.sha256(content).hexdigest()
        inbox = inbox_model.receive_controlled_document(
            binding.id, "nonblank-received-agreement", content
        )
        history_before = []
        history_fields = [
            "operator_id",
            "company_id",
            "xml_hash",
            "outcome",
            "diagnostic",
            "validation_report",
            "validation_report_hash",
            "normative_ruleset",
            "normative_manifest_hash",
            "move_id",
        ]
        for index, name in enumerate(("scope", "name", "onboarding_reference"), 1):
            original = agreement[name]
            agreement.write({name: " \t\n "})
            self.assertFalse(inbox.action_process(), name)
            self.assertEqual(inbox.state, "review")
            self.assertIn("agreement", inbox.diagnostic)
            self.assertFalse(inbox.move_id)
            self.assertEqual(moves.search_count(domain), before)
            self.assertEqual(inbox.original_xml.content, content)
            self.assertEqual(inbox.xml_hash, digest)
            history = inbox.processing_history_ids.sorted("id")
            self.assertEqual(len(history), index)
            self.assertEqual(history[:-1].read(history_fields), history_before)
            current = history[-1]
            self.assertEqual(current.operator_id, manager)
            self.assertEqual(current.company_id, company)
            self.assertEqual(current.xml_hash, digest)
            self.assertEqual(current.outcome, "review")
            self.assertFalse(current.move_id)
            report = json.loads(current.validation_report)
            self.assertTrue(report["valid"], report)
            self.assertTrue(report["xsd_valid"], report)
            self.assertEqual(
                hashlib.sha256(current.validation_report.encode()).hexdigest(),
                current.validation_report_hash,
            )
            history_before = history.read(history_fields)
            agreement.write({name: original})

        repaired = {
            "name": "  SYNTH-AGREEMENT-REPAIRED  ",
            "scope": "  Reviewed synthetic services\n",
            "onboarding_reference": "  SYNTH-BUYER-ONBOARD-REPAIRED  ",
        }
        agreement.write(repaired)
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(inbox.state, "processed")
        sale = inbox.move_id
        self.assertEqual((sale.move_type, sale.state), ("out_invoice", "draft"))
        self.assertEqual(sale.company_id, company)
        self.assertEqual(sale.partner_id, self.buyer.partner_id)
        self.assertEqual(
            (sale.amount_untaxed, sale.amount_tax, sale.amount_total), (100, 5, 105)
        )
        self.assertEqual(sale.l10n_ae_selfbilling_agreement_id, agreement)
        snapshot = {
            "reference": repaired["name"],
            "local_role": "supplier",
            "start_date": "2026-01-01",
            "end_date": False,
            "scope": repaired["scope"],
            "onboarding_reference": repaired["onboarding_reference"],
        }
        self.assertEqual(sale.l10n_ae_received_agreement_snapshot, snapshot)
        self.assertFalse(sale.l10n_ae_transmission_ids)
        history = inbox.processing_history_ids.sorted("id")
        self.assertEqual(len(history), 4)
        self.assertEqual(history[:-1].read(history_fields), history_before)
        self.assertEqual(history[-1].outcome, "processed")
        self.assertEqual(history[-1].operator_id, manager)
        self.assertEqual(history[-1].xml_hash, digest)
        self.assertEqual(history[-1].move_id, sale)
        self.assertEqual(moves.search_count(domain), before + 1)
        agreement.write({"scope": " ", "active": False})
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(inbox.move_id, sale)
        self.assertEqual(len(inbox.processing_history_ids), 4)
        self.assertEqual(sale.l10n_ae_received_agreement_snapshot, snapshot)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, digest)
        self.assertEqual(moves.search_count(domain), before + 1)

    def test_received_single_s_header_allowance_creates_one_adjustment_line(self):
        source, agreement, binding = self._selfbilling_fixture()
        tree = etree.fromstring(source.preview_l10n_ae_pint().encode())

        # The checked-in source is a 100 AED taxable line at 5%. Introduce one
        # document-level 10 AED allowance, with explicit S/5% classification.
        # Independent arithmetic: (100 - 10) * 5% = 4.50 VAT; payable 94.50.
        ns = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
        cbc = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

        def q(uri, name):
            return f"{{{uri}}}{name}"

        tax_total = tree.find("./{*}TaxTotal")
        assert tax_total is not None
        self.assertEqual(len(tree.findall("./{*}TaxTotal")), 1)
        self.assertIsNone(tree.find("./{*}TaxCurrencyCode"))
        self.assertIsNone(tree.find("./{*}TaxExchangeRate"))
        self.assertEqual(tax_total.find("./{*}TaxAmount").get("currencyID"), "AED")

        allowance = etree.Element(q(ns, "AllowanceCharge"))
        etree.SubElement(allowance, q(cbc, "ChargeIndicator")).text = "false"
        etree.SubElement(allowance, q(cbc, "AllowanceChargeReasonCode")).text = "100"
        etree.SubElement(
            allowance, q(cbc, "AllowanceChargeReason")
        ).text = "Special Rebate"
        etree.SubElement(allowance, q(cbc, "MultiplierFactorNumeric")).text = "10"
        etree.SubElement(allowance, q(cbc, "Amount"), currencyID="AED").text = "10.00"
        etree.SubElement(
            allowance, q(cbc, "BaseAmount"), currencyID="AED"
        ).text = "100.00"
        tax_category = etree.SubElement(allowance, q(ns, "TaxCategory"))
        etree.SubElement(tax_category, q(cbc, "ID")).text = "S"
        etree.SubElement(tax_category, q(cbc, "Percent")).text = "5"
        scheme = etree.SubElement(tax_category, q(ns, "TaxScheme"))
        etree.SubElement(scheme, q(cbc, "ID")).text = "VAT"
        # UBL sequence places header allowances after payment terms and before
        # TaxTotal. This insertion anchors to the first TaxTotal in the fixture.
        tree.insert(tree.index(tax_total), allowance)

        def set_text(xpath, value):
            node = tree.find(xpath)
            assert node is not None, f"fixture XPath missing: {xpath}"
            node.text = value

        set_text("./{*}LegalMonetaryTotal/{*}TaxExclusiveAmount", "90.00")
        set_text("./{*}LegalMonetaryTotal/{*}TaxInclusiveAmount", "94.50")
        set_text("./{*}LegalMonetaryTotal/{*}PayableAmount", "94.50")
        legal_total = tree.find("./{*}LegalMonetaryTotal")
        assert legal_total is not None
        self.assertEqual(float(legal_total.findtext("./{*}LineExtensionAmount")), 100)
        allowance_total = legal_total.find(q(cbc, "AllowanceTotalAmount"))
        if allowance_total is None:
            allowance_total = etree.Element(
                q(cbc, "AllowanceTotalAmount"), currencyID="AED"
            )
            legal_total.insert(
                legal_total.index(legal_total.find(q(cbc, "TaxInclusiveAmount"))) + 1,
                allowance_total,
            )
        allowance_total.text = "10.00"
        tax_total.find("./{*}TaxAmount").text = "4.50"
        subtotal = tax_total.find("./{*}TaxSubtotal")
        assert subtotal is not None
        subtotal.find("./{*}TaxableAmount").text = "90.00"
        subtotal.find("./{*}TaxAmount").text = "4.50"

        content = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "header-allowance-100-10", content)
        )

        # action_process runs the public pinned UBL/PINT validator before the
        # importer and retains its report even when the current review gate fires.
        processed = inbox.action_process()
        history = inbox.processing_history_ids
        self.assertEqual(len(history), 1)
        report = json.loads(history.validation_report)
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["xsd_valid"], report)
        # This is the behavior under development. Current code stops at the
        # explicit header-AllowanceCharge review gate after formal validation.
        self.assertTrue(processed, inbox.diagnostic)
        move = inbox.move_id
        self.assertEqual(move.move_type, "out_invoice")
        self.assertEqual(move.state, "draft")
        self.assertEqual(move.l10n_ae_selfbilling_agreement_id, agreement)
        self.assertEqual(
            (move.amount_untaxed, move.amount_tax, move.amount_total), (90, 4.5, 94.5)
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(
            len(
                move.invoice_line_ids.filtered(
                    lambda line: "Special Rebate" in (line.name or "")
                )
            ),
            1,
        )
        allowance_line = move.invoice_line_ids.filtered(
            lambda line: "Special Rebate" in (line.name or "")
        )
        self.assertEqual(allowance_line.price_subtotal, -10)
        self.assertEqual(allowance_line.tax_ids, self.tax_sale_a)
        self.assertEqual(move.l10n_ae_received_payable_amount, 94.5)
        self.assertEqual(len(move.invoice_line_ids), 2)
        self.assertEqual(move.company_id, self.env.company)
        self.assertEqual(move.partner_id, self.buyer.partner_id)
        self.assertEqual(
            move.l10n_ae_fiscal_uuid, "eeeeeeea-1111-4111-8111-111111111111"
        )
        self.assertTrue(move.l10n_ae_received_document)
        self.assertEqual(move.l10n_ae_received_process, "selfbilling")
        self.assertFalse(move.l10n_ae_transmission_id)
        self.assertTrue(inbox.action_process())
        self.assertEqual(inbox.move_id, move)
        self.assertEqual(len(move.invoice_line_ids), 2)
        self.assertEqual(len(inbox.processing_history_ids), 1)
        self.assertEqual(
            self.env["account.move"].search_count(
                [
                    ("company_id", "=", self.env.company.id),
                    ("l10n_ae_received_document", "=", True),
                    (
                        "l10n_ae_fiscal_uuid",
                        "=",
                        "eeeeeeea-1111-4111-8111-111111111111",
                    ),
                ]
            ),
            1,
        )

    def test_received_single_s_header_charge_creates_one_adjustment_line(self):
        source, agreement, binding = self._selfbilling_fixture()
        tree = etree.fromstring(source.preview_l10n_ae_pint().encode())

        # The checked-in source is a 100 AED taxable line at 5%. Add one
        # document-level 10 AED charge: 100 + 10 = 110; VAT is 5.50.
        ns = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
        cbc = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

        def q(uri, name):
            return f"{{{uri}}}{name}"

        tax_total = tree.find("./{*}TaxTotal")
        assert tax_total is not None
        self.assertEqual(len(tree.findall("./{*}TaxTotal")), 1)
        self.assertIsNone(tree.find("./{*}TaxCurrencyCode"))
        self.assertIsNone(tree.find("./{*}TaxExchangeRate"))
        self.assertEqual(tax_total.find("./{*}TaxAmount").get("currencyID"), "AED")

        charge = etree.Element(q(ns, "AllowanceCharge"))
        etree.SubElement(charge, q(cbc, "ChargeIndicator")).text = "true"
        etree.SubElement(charge, q(cbc, "AllowanceChargeReasonCode")).text = "AAT"
        etree.SubElement(charge, q(cbc, "AllowanceChargeReason")).text = "Rush Delivery"
        etree.SubElement(charge, q(cbc, "MultiplierFactorNumeric")).text = "10"
        etree.SubElement(charge, q(cbc, "Amount"), currencyID="AED").text = "10.00"
        etree.SubElement(charge, q(cbc, "BaseAmount"), currencyID="AED").text = "100.00"
        tax_category = etree.SubElement(charge, q(ns, "TaxCategory"))
        etree.SubElement(tax_category, q(cbc, "ID")).text = "S"
        etree.SubElement(tax_category, q(cbc, "Percent")).text = "5"
        scheme = etree.SubElement(tax_category, q(ns, "TaxScheme"))
        etree.SubElement(scheme, q(cbc, "ID")).text = "VAT"
        tree.insert(tree.index(tax_total), charge)
        uuid_node = tree.find("./{*}UUID")
        assert uuid_node is not None
        uuid_node.text = "eeeeeeea-2222-4222-8222-222222222222"

        def set_text(xpath, value):
            node = tree.find(xpath)
            assert node is not None, f"fixture XPath missing: {xpath}"
            node.text = value

        set_text("./{*}LegalMonetaryTotal/{*}TaxExclusiveAmount", "110.00")
        set_text("./{*}LegalMonetaryTotal/{*}TaxInclusiveAmount", "115.50")
        set_text("./{*}LegalMonetaryTotal/{*}PayableAmount", "115.50")
        legal_total = tree.find("./{*}LegalMonetaryTotal")
        assert legal_total is not None
        self.assertEqual(float(legal_total.findtext("./{*}LineExtensionAmount")), 100)
        charge_total = legal_total.find(q(cbc, "ChargeTotalAmount"))
        if charge_total is None:
            charge_total = etree.Element(q(cbc, "ChargeTotalAmount"), currencyID="AED")
            insert_before = next(
                (
                    node
                    for tag in (
                        "PrepaidAmount",
                        "PayableRoundingAmount",
                        "PayableAmount",
                    )
                    if (node := legal_total.find(q(cbc, tag))) is not None
                ),
                None,
            )
            assert insert_before is not None
            legal_total.insert(legal_total.index(insert_before), charge_total)
        charge_total.text = "10.00"
        tax_total.find("./{*}TaxAmount").text = "5.50"
        subtotal = tax_total.find("./{*}TaxSubtotal")
        assert subtotal is not None
        subtotal.find("./{*}TaxableAmount").text = "110.00"
        subtotal.find("./{*}TaxAmount").text = "5.50"

        content = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "header-charge-100-10", content)
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(len(inbox.processing_history_ids), 1)
        report = json.loads(inbox.processing_history_ids.validation_report)
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["xsd_valid"], report)

        move = inbox.move_id
        self.assertEqual(move.move_type, "out_invoice")
        self.assertEqual(move.state, "draft")
        self.assertEqual(move.l10n_ae_selfbilling_agreement_id, agreement)
        self.assertEqual(
            (move.amount_untaxed, move.amount_tax, move.amount_total), (110, 5.5, 115.5)
        )
        self.assertEqual(inbox.original_xml.content, content)
        charge_lines = move.invoice_line_ids.filtered(
            lambda line: "Rush Delivery" in (line.name or "")
        )
        self.assertEqual(len(charge_lines), 1)
        self.assertEqual(charge_lines.price_subtotal, 10)
        self.assertEqual(charge_lines.tax_ids, self.tax_sale_a)
        self.assertEqual(move.l10n_ae_received_payable_amount, 115.5)
        self.assertEqual(len(move.invoice_line_ids), 2)
        self.assertEqual(move.company_id, self.env.company)
        self.assertEqual(move.partner_id, self.buyer.partner_id)
        self.assertEqual(
            move.l10n_ae_fiscal_uuid, "eeeeeeea-2222-4222-8222-222222222222"
        )
        self.assertTrue(move.l10n_ae_received_document)
        self.assertEqual(move.l10n_ae_received_process, "selfbilling")
        self.assertFalse(move.l10n_ae_transmission_id)
        self.assertTrue(inbox.action_process())
        self.assertEqual(inbox.move_id, move)
        self.assertEqual(len(move.invoice_line_ids), 2)
        self.assertEqual(len(inbox.processing_history_ids), 1)
        self.assertEqual(
            self.env["account.move"].search_count(
                [
                    ("company_id", "=", self.env.company.id),
                    ("l10n_ae_received_document", "=", True),
                    (
                        "l10n_ae_fiscal_uuid",
                        "=",
                        "eeeeeeea-2222-4222-8222-222222222222",
                    ),
                ]
            ),
            1,
        )

    def test_agent_selfbilling_uses_supplier_vat_and_refuses_mismatched_principal(self):
        source, agreement, binding = self._selfbilling_fixture()
        source.write(
            {"l10n_ae_disclosed_agent": True, "l10n_ae_principal_id": "100000000100003"}
        )
        with self.assertRaisesRegex(UserError, "IBR-176-AE"):
            source.action_l10n_ae_validate()
        # The emitting buyer's VAT is not the economic supplier's VAT.
        source.l10n_ae_principal_id = "100000000600003"
        self.assertTrue(source.validate_l10n_ae_pint()["valid"])
        existing = self.init_invoice(
            "out_invoice",
            partner=self.buyer.partner_id,
            invoice_date=source.invoice_date,
            amounts=[100],
            taxes=self.tax_sale_a,
        )
        existing.write(
            {
                "ref": source.name,
                "l10n_ae_origin_system": "internal-billing",
                "l10n_ae_origin_document_id": "SYNTH-AGENT-RECONCILIATION",
                "l10n_ae_disclosed_agent": True,
                "l10n_ae_principal_id": "100000000300003",
            }
        )
        content = source.preview_l10n_ae_pint().encode()
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "agent-existing-sale", content)
        )
        self.assertFalse(inbox.action_process())
        self.assertIn("principal", inbox.diagnostic.lower())
        self.assertFalse(inbox.move_id)
        self.assertFalse(existing.l10n_ae_received_document)
        self.assertEqual(existing.l10n_ae_principal_id, "100000000300003")
        existing.l10n_ae_principal_id = "100000000600003"
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(inbox.move_id, existing)
        self.assertEqual(existing.l10n_ae_selfbilling_agreement_id, agreement)
        self.assertEqual(existing.l10n_ae_principal_id, "100000000600003")
        self.assertEqual(existing.state, "draft")
        self.assertEqual((existing.amount_tax, existing.amount_total), (5, 105))
        self.assertEqual(inbox.original_xml.content, content)

    def test_buyer_issued_invoice_is_received_as_a_supplier_sale_without_emission(self):
        supplier = self.env.company
        source, recipient_agreement, binding = self._selfbilling_fixture()
        content = source.preview_l10n_ae_pint().encode()
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id,
                "buyer-issued-invoice",
                content,
            )
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        sale = inbox.move_id
        self.assertEqual(sale.company_id, supplier)
        self.assertEqual(sale.partner_id, self.buyer.partner_id)
        self.assertEqual(sale.move_type, "out_invoice")
        self.assertEqual(sale.state, "draft")
        self.assertEqual(
            (sale.amount_untaxed, sale.amount_tax, sale.amount_total), (100, 5, 105)
        )
        self.assertEqual(sale.l10n_ae_selfbilling_agreement_id, recipient_agreement)
        self.assertTrue(sale.l10n_ae_received_document)
        self.assertEqual(sale.l10n_ae_received_process, "selfbilling")
        self.assertEqual(inbox.original_xml.content, content)
        supplier.write(
            {"l10n_ae_edi_auto_prepare": True, "l10n_ae_edi_binding_id": binding.id}
        )
        sale.action_post()
        self.assertFalse(sale.l10n_ae_transmission_id)

    def test_received_self_billed_credit_links_the_original_supplier_sale(self):
        source, agreement, binding = self._selfbilling_fixture()
        inbox_model = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        original = inbox_model.receive_controlled_document(
            binding.id,
            "buyer-original",
            source.preview_l10n_ae_pint().encode(),
        )
        self.assertTrue(original.action_process(), original.diagnostic)
        buyer_agreement = source.l10n_ae_selfbilling_agreement_id
        credit = source.copy(
            {
                "move_type": "in_refund",
                "invoice_date": source.invoice_date,
                "reversed_entry_id": source.id,
                "l10n_ae_selfbilling_agreement_id": buyer_agreement.id,
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "eeeeeeea-2222-4222-8222-222222222222",
            }
        )
        credit.invoice_line_ids.write({"price_unit": 40})
        credit.action_post()
        content = credit.preview_l10n_ae_pint().encode()
        inbox = inbox_model.receive_controlled_document(
            binding.id, "buyer-credit", content
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        refund = inbox.move_id
        self.assertEqual(refund.company_id, self.env.company)
        self.assertEqual(refund.partner_id, self.buyer.partner_id)
        self.assertEqual(refund.move_type, "out_refund")
        self.assertEqual(refund.state, "draft")
        self.assertEqual(
            (refund.amount_untaxed, refund.amount_tax, refund.amount_total), (40, 2, 42)
        )
        self.assertEqual(refund.reversed_entry_id, original.move_id)
        self.assertEqual(refund.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertEqual(refund.l10n_ae_received_process, "selfbilling")
        self.assertEqual(refund.l10n_ae_selfbilling_agreement_id, agreement)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(original.move_id.amount_total, 105)
        self.assertEqual(original.move_id.state, "draft")
        self.env.company.write(
            {"l10n_ae_edi_auto_prepare": True, "l10n_ae_edi_binding_id": binding.id}
        )
        refund.action_post()
        self.assertFalse(refund.l10n_ae_transmission_id)
        self.assertEqual(original.move_id.state, "draft")

    def test_received_self_billing_links_the_existing_billing_sale_without_rewriting_it(
        self,
    ):
        source, agreement, binding = self._selfbilling_fixture()
        existing = self.init_invoice(
            "out_invoice",
            partner=self.buyer.partner_id,
            invoice_date=source.invoice_date,
            amounts=[100],
            taxes=self.tax_sale_a,
        )
        existing.write(
            {
                "ref": source.name,
                "l10n_ae_origin_system": "internal-billing",
                "l10n_ae_origin_document_id": "SYNTH-AFFILIATE-001",
            }
        )
        existing.invoice_line_ids.name = "Internal affiliate billing memo"
        content = source.preview_l10n_ae_pint().encode()
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id,
                "existing-billing-sale",
                content,
            )
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(inbox.move_id, existing)
        self.assertEqual(
            existing.invoice_line_ids.name, "Internal affiliate billing memo"
        )
        self.assertEqual(existing.l10n_ae_origin_system, "internal-billing")
        self.assertEqual(existing.l10n_ae_origin_document_id, "SYNTH-AFFILIATE-001")
        self.assertEqual(existing.l10n_ae_selfbilling_agreement_id, agreement)
        self.assertTrue(existing.l10n_ae_received_document)
        self.assertEqual(existing.l10n_ae_fiscal_uuid, source.l10n_ae_fiscal_uuid)
        self.assertEqual(existing.amount_total, 105)
        self.assertEqual(inbox.original_xml.content, content)

    def test_xml_comments_do_not_prevent_linking_the_existing_billing_sale(self):
        source, _agreement, binding = self._selfbilling_fixture()
        existing = self.init_invoice(
            "out_invoice",
            partner=self.buyer.partner_id,
            invoice_date=source.invoice_date,
            amounts=[100],
            taxes=self.tax_sale_a,
        )
        existing.ref = source.name
        tree = etree.fromstring(source.preview_l10n_ae_pint().encode())
        tree.find("./{*}InvoiceLine/{*}Price").append(
            etree.Comment("Synthetic annotation")
        )
        content = etree.tostring(tree)
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id,
                "annotated-billing-sale",
                content,
            )
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(inbox.move_id, existing)
        self.assertEqual(inbox.original_xml.content, content)

    def test_selfbilling_reconciliation_requires_matching_free_zone_and_beneficiary(
        self,
    ):
        source, agreement, binding = self._selfbilling_fixture()
        source.write(
            {"l10n_ae_free_zone": True, "l10n_ae_beneficiary_id": "1000000003"}
        )
        existing = self.init_invoice(
            "out_invoice",
            partner=self.buyer.partner_id,
            invoice_date=source.invoice_date,
            amounts=[100],
            taxes=self.tax_sale_a,
        )
        existing.write(
            {
                "ref": source.name,
                "l10n_ae_origin_system": "internal-billing",
                "l10n_ae_origin_document_id": "SYNTH-FREEZONE-RECONCILIATION",
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000004",
            }
        )
        content = source.preview_l10n_ae_pint().encode()
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "freezone-existing-sale", content)
        )
        before = self.env["account.move"].search_count(
            [("move_type", "=", "out_invoice")]
        )
        self.assertFalse(inbox.action_process())
        self.assertIn("scenario and beneficiary", inbox.diagnostic)
        self.assertEqual(existing.l10n_ae_beneficiary_id, "1000000004")
        self.assertFalse(existing.l10n_ae_received_document)
        existing.write({"l10n_ae_free_zone": False, "l10n_ae_beneficiary_id": False})
        self.assertFalse(inbox.action_process())
        self.assertIn("scenario and beneficiary", inbox.diagnostic)
        self.assertFalse(existing.l10n_ae_free_zone)
        self.assertFalse(existing.l10n_ae_received_document)
        self.assertFalse(inbox.move_id)
        self.assertEqual(
            self.env["account.move"].search_count([("move_type", "=", "out_invoice")]),
            before,
        )
        existing.write(
            {"l10n_ae_free_zone": True, "l10n_ae_beneficiary_id": "1000000003"}
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(inbox.move_id, existing)
        self.assertEqual(existing.l10n_ae_selfbilling_agreement_id, agreement)
        self.assertEqual(existing.l10n_ae_beneficiary_id, "1000000003")
        self.assertEqual(
            existing.l10n_ae_origin_document_id, "SYNTH-FREEZONE-RECONCILIATION"
        )
        self.assertEqual((existing.amount_tax, existing.amount_total), (5, 105))
        self.assertEqual(inbox.original_xml.content, content)

    def test_received_agreement_evidence_survives_later_contract_changes(self):
        source, agreement, binding = self._selfbilling_fixture()
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id,
                "agreement-evidence",
                source.preview_l10n_ae_pint().encode(),
            )
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        sale = inbox.move_id
        agreement.write(
            {"name": "REVISED-REFERENCE", "scope": "Revised scope", "active": False}
        )
        self.assertEqual(
            sale.l10n_ae_received_agreement_snapshot,
            {
                "reference": "SYNTH-AGREEMENT-6-1",
                "local_role": "supplier",
                "start_date": "2026-01-01",
                "end_date": False,
                "scope": "Synthetic services and adjustments",
                "onboarding_reference": "SYNTH-BUYER-ONBOARD-6",
            },
        )
        self.assertEqual(
            sale.l10n_ae_received_agreement_reference, "SYNTH-AGREEMENT-6-1"
        )
        with self.assertRaises(UserError):
            sale.write(
                {"l10n_ae_received_agreement_snapshot": {"reference": "REWRITTEN"}}
            )

    def test_equal_totals_do_not_allow_replacing_different_billing_quantities(self):
        source, _agreement, binding = self._selfbilling_fixture()
        existing = self.init_invoice(
            "out_invoice",
            partner=self.buyer.partner_id,
            invoice_date=source.invoice_date,
            amounts=[50],
            taxes=self.tax_sale_a,
        )
        existing.invoice_line_ids.quantity = 2
        existing.ref = source.name
        self.assertEqual(existing.amount_total, 105)
        content = source.preview_l10n_ae_pint().encode()
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id,
                "different-billing-quantities",
                content,
            )
        )
        before = self.env["account.move"].search_count(
            [("move_type", "=", "out_invoice")]
        )
        self.assertFalse(inbox.action_process())
        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)
        self.assertIn("fiscal lines", inbox.diagnostic)
        self.assertEqual(
            self.env["account.move"].search_count([("move_type", "=", "out_invoice")]),
            before,
        )
        self.assertEqual(
            (existing.invoice_line_ids.quantity, existing.invoice_line_ids.price_unit),
            (2, 50),
        )
        self.assertFalse(existing.l10n_ae_received_document)
        self.assertEqual(inbox.original_xml.content, content)

    def test_ineligible_or_ambiguous_receiving_agreement_stays_in_review(self):
        source, agreement, binding = self._selfbilling_fixture()
        content = source.preview_l10n_ae_pint().encode()
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id,
                "review-agreement",
                content,
            )
        )
        before = self.env["account.move"].search_count(
            [("move_type", "=", "out_invoice")]
        )
        for values in (
            {"active": False},
            {"active": True, "end_date": "2026-09-30"},
            {"end_date": False, "local_role": "buyer"},
        ):
            agreement.write(values)
            self.assertFalse(inbox.action_process())
            self.assertEqual(inbox.state, "review")
            self.assertFalse(inbox.move_id)
            self.assertIn("agreement", inbox.diagnostic)
            self.assertEqual(
                self.env["account.move"].search_count(
                    [("move_type", "=", "out_invoice")]
                ),
                before,
            )
        agreement.local_role = "supplier"
        duplicate = agreement.copy({"name": "DUPLICATE-REQUIRES-REVIEW"})
        self.assertFalse(inbox.action_process())
        self.assertFalse(inbox.move_id)
        duplicate.active = False
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(inbox.move_id.l10n_ae_selfbilling_agreement_id, agreement)
        self.assertEqual(inbox.original_xml.content, content)

    def test_sender_prepaid_credit_does_not_claim_a_local_payment_in_the_history(self):
        source, _agreement, binding = self._selfbilling_fixture()
        inbox_model = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        original = inbox_model.receive_controlled_document(
            binding.id,
            "prepaid-original",
            source.preview_l10n_ae_pint().encode(),
        )
        self.assertTrue(original.action_process(), original.diagnostic)
        buyer_agreement = source.l10n_ae_selfbilling_agreement_id
        credit = source.copy(
            {
                "move_type": "in_refund",
                "invoice_date": source.invoice_date,
                "reversed_entry_id": source.id,
                "l10n_ae_selfbilling_agreement_id": buyer_agreement.id,
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "eeeeeeec-7777-4777-8777-777777777777",
            }
        )
        credit.invoice_line_ids.price_unit = 40
        credit.action_post()
        payments = self.env["account.payment"].search_count(
            [("company_id", "=", self.env.company.id)]
        )
        inbox = inbox_model.receive_controlled_document(
            binding.id,
            "prepaid-credit",
            credit.preview_l10n_ae_pint().encode(),
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        refund = inbox.move_id
        self.assertEqual(refund.l10n_ae_received_prepaid_amount, 42)
        self.assertEqual(refund.l10n_ae_received_payable_amount, 0)
        self.assertEqual(refund.state, "draft")
        self.assertEqual(
            self.env["account.payment"].search_count(
                [("company_id", "=", self.env.company.id)]
            ),
            payments,
        )
        history = " ".join(str(body) for body in refund.message_ids.mapped("body"))
        self.assertNotIn("A payment of", history)
        self.assertIn("The sender declares a prepaid amount", history)
        self.assertIn("Review it against local accounting", history)
