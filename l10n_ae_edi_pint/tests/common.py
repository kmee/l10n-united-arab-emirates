# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
import os
from pathlib import Path

from lxml import etree

from odoo.exceptions import UserError

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_ae_edi.tests.common import (
    CONTROLLED_CAPABILITIES,
    review_controlled_binding,
)

from ..tools.validation import OfflinePintValidator

NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}


class UaePintTestCommon(AccountTestInvoicingCommon):
    def _reviewed_binding(self, values, *, capabilities, note):
        """Create the exact fixture and review it through an ordinary manager."""
        return review_controlled_binding(
            self.env["l10n_ae.edi.binding"].create(values),
            self.simple_accountman,
            capabilities=capabilities,
            note=note,
        )

    def setUp(self):
        super().setUp()
        cache = Path(__file__).resolve().parents[2] / ".runtime"
        # Capture fixture locations before package-cycle tests patch production
        # configuration. Explicit values, including invalid ones, are retained.
        self.normative_directories = {
            "1.0.4": os.environ.get("KMEE_UAE_NORMATIVE_DIR", str(cache / "normative")),
            "1.0.3": os.environ.get(
                "KMEE_UAE_TEST_NORMATIVE_1_0_3_DIR", str(cache / "normative-1.0.3")
            ),
        }

    def _foreign_received_fixture(self):
        usd = self.env.ref("base.USD")
        usd.active = True
        rate = self.env["res.currency.rate"].create(
            {
                "currency_id": usd.id,
                "company_id": self.env.company.id,
                "name": "2019-01-01",
                "rate": 1 / 3.6725,
            }
        )
        source = self.init_invoice(
            "out_invoice",
            amounts=[100],
            taxes=self.tax_sale_a,
            currency=usd,
            post=True,
        )
        source.write(
            {
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": source.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-RECEIVED-FISCAL-QUOTE",
                "l10n_ae_fiscal_uuid": "aaaaaaac-1111-4111-8111-111111111111",
            }
        )
        return self._received_test_xml(source), rate, source

    def _received_test_xml(self, source=None):
        if source is None:
            source = self.init_invoice(
                "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
            )
            source.l10n_ae_fiscal_uuid = "abcdefab-2222-4222-8222-222222222222"
        tree = etree.fromstring(source.preview_l10n_ae_pint().encode())
        supplier = tree.find("cac:AccountingSupplierParty", NS)
        customer = tree.find("cac:AccountingCustomerParty", NS)
        supplier_party, customer_party = supplier[0], customer[0]
        supplier.remove(supplier_party)
        customer.remove(customer_party)
        supplier.append(customer_party)
        customer.append(supplier_party)
        return tree

    def _assert_received_summary_credit(
        self, expected_flags, free_zone, beneficiary_id
    ):
        # Independently worked credit: AED40 net, AED2 VAT, AED42 inclusive;
        # the settled amount is fully prepaid, so payable is zero.
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        original.write({"l10n_ae_fiscal_uuid": "aaaaaaab-3333-4333-8333-333333333333"})
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming Summary supplier credit"}
        )
        binding = review_controlled_binding(
            binding,
            self.simple_accountman,
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
        )
        self.assertFalse(receiver.env.su)
        received_bill = receiver.receive_controlled_document(
            binding.id,
            "summary-credit-original",
            etree.tostring(self._received_test_xml(original)),
        )
        self.assertTrue(received_bill.action_process(), received_bill.diagnostic)
        bill = received_bill.move_id
        self.assertEqual((bill.move_type, bill.state), ("in_invoice", "draft"))
        self.assertEqual(
            (bill.amount_untaxed, bill.amount_tax, bill.amount_total), (100, 5, 105)
        )

        reversal = (
            self.env["account.move.reversal"]
            .with_context(active_model="account.move", active_ids=original.ids)
            .create(
                {
                    "reason": "Synthetic consideration adjustment",
                    "move_ids": [(6, 0, original.ids)],
                    "journal_id": original.journal_id.id,
                }
            )
        )
        reversal.with_user(self.simple_accountman).refund_moves()
        credit = reversal.new_move_ids.with_user(self.simple_accountman)
        self.assertFalse(credit.env.su)
        credit.invoice_line_ids.write({"price_unit": 40})
        credit.write(
            {
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_summary": True,
                "l10n_ae_invoicing_period_start": "2019-01-01",
                "l10n_ae_invoicing_period_end": "2019-01-31",
                "l10n_ae_fiscal_uuid": "aaaaaaab-4444-4444-8444-444444444444",
                **(
                    {
                        "l10n_ae_free_zone": True,
                        "l10n_ae_beneficiary_id": beneficiary_id,
                    }
                    if free_zone
                    else {}
                ),
            }
        )
        credit.action_post()
        content = etree.tostring(self._received_test_xml(credit))
        report = OfflinePintValidator(
            self.normative_directories["1.0.4"],
            ruleset="kmee-pint-ae-1.0.4-ubl-2.1",
        ).validate(content)
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["profile"], "billing")
        tree = etree.fromstring(content)
        self.assertEqual(etree.QName(tree).localname, "CreditNote")
        self.assertEqual(
            etree.QName(tree).namespace,
            "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2",
        )
        self.assertEqual(tree.findtext("cbc:CreditNoteTypeCode", namespaces=NS), "381")
        self.assertEqual(
            tree.findtext("cbc:ProfileExecutionID", namespaces=NS), expected_flags
        )
        if free_zone:
            beneficiary_xpath = (
                "cac:BuyerCustomerParty/cac:Party/cac:PartyIdentification/cbc:ID"
            )
            self.assertEqual(len(tree.findall(beneficiary_xpath, namespaces=NS)), 1)
            self.assertEqual(
                tree.findtext(beneficiary_xpath, namespaces=NS), beneficiary_id
            )
            for party in ("AccountingSupplierParty", "AccountingCustomerParty"):
                for path in (
                    "cac:Party/cbc:EndpointID",
                    "cac:Party/cac:PartyTaxScheme/cbc:CompanyID",
                ):
                    identity = tree.findtext(f"cac:{party}/{path}", namespaces=NS)
                    self.assertTrue(identity)
                    self.assertNotEqual(identity, beneficiary_id)
        self.assertEqual(
            tree.findtext("cbc:CustomizationID", namespaces=NS),
            "urn:peppol:pint:billing-1@ae-1",
        )
        self.assertEqual(
            tree.findtext("cbc:ProfileID", namespaces=NS), "urn:peppol:bis:billing"
        )
        self.assertEqual(
            tree.findtext("cbc:DocumentCurrencyCode", namespaces=NS), "AED"
        )
        self.assertEqual(
            (
                tree.findtext("cac:InvoicePeriod/cbc:StartDate", namespaces=NS),
                tree.findtext("cac:InvoicePeriod/cbc:EndDate", namespaces=NS),
            ),
            ("2019-01-01", "2019-01-31"),
        )
        self.assertEqual(
            (
                tree.findtext(
                    "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", namespaces=NS
                ),
                tree.findtext("cac:TaxTotal/cbc:TaxAmount", namespaces=NS),
                tree.findtext(
                    "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", namespaces=NS
                ),
                tree.findtext(
                    "cac:LegalMonetaryTotal/cbc:PrepaidAmount", namespaces=NS
                ),
                tree.findtext(
                    "cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=NS
                ),
            ),
            ("40.00", "2.00", "42.00", "42.00", "0.00"),
        )
        self.assertEqual(
            tree.findtext(
                "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID",
                namespaces=NS,
            ),
            original.name,
        )
        self.assertEqual(
            tree.findtext(
                "cac:BillingReference/cac:InvoiceDocumentReference/cbc:IssueDate",
                namespaces=NS,
            ),
            str(original.invoice_date),
        )

        move_counts = {
            model: self.env[model].search_count([])
            for model in (
                "l10n_ae.edi.transmission",
                "l10n_ae.edi.attempt",
                "l10n_ae.edi.event",
            )
        }
        account_moves_before_import = self.env["account.move"].search([]).ids
        bill_ledger = bill.line_ids.sorted("id").read(
            ["account_id", "partner_id", "debit", "credit", "balance", "write_date"]
        )
        bill_timestamps = bill.read(["create_date", "write_date"])
        inbox = receiver.receive_controlled_document(
            binding.id, "summary-credit", content
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        refund = inbox.move_id
        self.assertEqual((refund.move_type, refund.state), ("in_refund", "draft"))
        self.assertEqual(refund.partner_id, self.partner_a)
        self.assertEqual(refund.reversed_entry_id, bill)
        self.assertTrue(refund.l10n_ae_summary)
        self.assertEqual(refund.l10n_ae_free_zone, free_zone)
        self.assertEqual(refund.l10n_ae_beneficiary_id or False, beneficiary_id)
        self.assertEqual(refund.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertEqual(
            (
                refund.l10n_ae_invoicing_period_start.isoformat(),
                refund.l10n_ae_invoicing_period_end.isoformat(),
            ),
            ("2019-01-01", "2019-01-31"),
        )
        self.assertEqual(
            (refund.amount_untaxed, refund.amount_tax, refund.amount_total), (40, 2, 42)
        )
        self.assertEqual(refund.l10n_ae_received_prepaid_amount, 42)
        self.assertEqual(refund.l10n_ae_received_payable_amount, 0)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, hashlib.sha256(content).hexdigest())
        self.assertTrue(refund.l10n_ae_received_document)
        self.assertFalse(refund.l10n_ae_transmission_id)
        original_hash = inbox.xml_hash
        history = inbox.processing_history_ids
        self.assertEqual(len(history), 1)
        history_row = history.read(["validation_report", "validation_report_hash"])
        history_report = json.loads(history.validation_report)
        self.assertTrue(history_report["valid"], history_report)
        self.assertEqual(history_report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        self.assertEqual(
            history.validation_report_hash,
            hashlib.sha256(history.validation_report.encode()).hexdigest(),
        )
        refund_ledger = refund.line_ids.sorted("id").read(
            ["account_id", "partner_id", "debit", "credit", "balance", "write_date"]
        )
        refund_timestamps = refund.read(["create_date", "write_date"])
        replay = receiver.receive_controlled_document(
            binding.id, "summary-credit", content
        )
        self.assertEqual(replay, inbox)
        self.assertEqual(
            sorted(self.env["account.move"].search([]).ids),
            sorted(account_moves_before_import + [refund.id]),
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(inbox.move_id, refund)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, original_hash)
        self.assertEqual(inbox.processing_history_ids, history)
        self.assertEqual(
            history.read(["validation_report", "validation_report_hash"]), history_row
        )
        self.assertEqual(
            refund.line_ids.sorted("id").read(
                ["account_id", "partner_id", "debit", "credit", "balance", "write_date"]
            ),
            refund_ledger,
        )
        self.assertEqual(refund.read(["create_date", "write_date"]), refund_timestamps)
        self.assertEqual(
            sorted(self.env["account.move"].search([]).ids),
            sorted(account_moves_before_import + [refund.id]),
        )
        self.assertEqual(
            bill.line_ids.sorted("id").read(
                ["account_id", "partner_id", "debit", "credit", "balance", "write_date"]
            ),
            bill_ledger,
        )
        self.assertEqual(bill.read(["create_date", "write_date"]), bill_timestamps)
        protected_received_values = [
            {"l10n_ae_summary": False},
            {"l10n_ae_invoicing_period_start": "2019-02-01"},
            {"l10n_ae_invoicing_period_end": "2019-02-28"},
        ]
        if free_zone:
            protected_received_values.extend(
                [
                    {"l10n_ae_free_zone": False},
                    {"l10n_ae_beneficiary_id": "1000000004"},
                ]
            )
        for values in protected_received_values:
            with self.assertRaisesRegex(UserError, "received"):
                refund.write(values)
        self.assertEqual(
            {model: self.env[model].search_count([]) for model in move_counts},
            move_counts,
        )
        return content, receiver, binding

    def _commercial_received_fixture(self):
        self.partner_a.vat = False
        outside_scope = self.tax_sale_a.copy(
            {
                "name": "Commercial outside scope",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        purchase_tax = self.env.company.account_purchase_tax_id.copy(
            {
                "name": "Reviewed outside-scope purchase",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        source = self.init_invoice("out_invoice", amounts=[100], taxes=outside_scope)
        source.write(
            {
                "l10n_ae_document_category": "commercial",
                "l10n_ae_fiscal_uuid": "ccccccca-1111-4111-8111-111111111111",
            }
        )
        source.action_post()
        tree = self._received_test_xml(source)
        return source, purchase_tax, tree

    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        # Reviewed synthetic HS for the goods-backed Summary/billing fixtures.
        # No customer or authoritative tariff classification is implied.
        cls.product_a.l10n_ae_classification_code = "999999"
        cls.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        cls.env.company.partner_id.write(
            {
                "vat": "100000000100003",
                "street": "Synthetic supplier street",
                "city": "Dubai",
                "state_id": cls.env.ref("base.state_ae_du").id,
                "routing_scheme": "0235",
                "routing_endpoint": "1000000001",
                "l10n_ae_registration_type": "TL",
                "l10n_ae_registration_authority": "Synthetic Free Zone Authority",
                "additional_identifiers": {"AE_TL": "SYNTH-TRADE-0001"},
            }
        )
        cls.partner_a.write(
            {
                "country_id": cls.env.ref("base.ae").id,
                "vat": "100000000200003",
                "street": "Synthetic buyer street",
                "city": "Dubai",
                "state_id": cls.env.ref("base.state_ae_du").id,
                "routing_scheme": "0235",
                "routing_endpoint": "1000000002",
                "l10n_ae_registration_type": "TL",
                "l10n_ae_registration_authority": "Synthetic Buyer Authority",
                "additional_identifiers": {"AE_TL": "SYNTH-TRADE-0002"},
            }
        )

    def _assert_native_partial_commercial_credit(self):
        outside_scope = self.tax_sale_a.copy(
            {
                "name": "Reviewed synthetic outside-scope sale",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        original = self.init_invoice("out_invoice", amounts=[100], taxes=outside_scope)
        binding = review_controlled_binding(
            self.env["l10n_ae.edi.binding"].create(
                {
                    "name": "Commercial credit local simulation",
                    "fiscal_uuid_policy": "controlled_local",
                }
            ),
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note=(
                "Synthetic controlled contract for _assert_native_partial_commercial_"
                "credit."
            ),
        )
        original.write(
            {
                "l10n_ae_document_category": "commercial",
                "l10n_ae_binding_id": binding.id,
            }
        )
        original.action_post()
        original.action_l10n_ae_prepare()
        original_evidence = (
            original.l10n_ae_transmission_id.payload,
            original.l10n_ae_transmission_id.xml_content,
        )
        report = original.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        wizard = (
            self.env["account.move.reversal"]
            .with_context(active_model="account.move", active_ids=original.ids)
            .create(
                {
                    "reason": "Reviewed synthetic consideration reduction",
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                    "journal_id": original.journal_id.id,
                    "date": original.invoice_date,
                }
            )
        )
        wizard.refund_moves()
        credit = wizard.new_move_ids
        self.assertEqual(
            (credit.state, credit.l10n_ae_document_category), ("draft", "commercial")
        )
        credit.invoice_line_ids.price_unit = 40
        credit.l10n_ae_binding_id = binding
        credit.action_post()
        credit.action_l10n_ae_prepare()
        report = credit.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        tree = etree.fromstring(credit.l10n_ae_transmission_id.xml_content.encode())
        self.assertEqual(tree.findtext("cbc:CreditNoteTypeCode", namespaces=NS), "81")
        self.assertEqual(
            tree.findtext(
                "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID",
                namespaces=NS,
            ),
            original.name,
        )
        self.assertEqual(
            tree.findtext(
                "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", namespaces=NS
            ),
            "40.00",
        )
        self.assertEqual(
            tree.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=NS),
            "0.00",
        )
        self.assertEqual(
            (
                credit.amount_untaxed,
                credit.amount_tax,
                credit.amount_total,
                credit.amount_residual,
            ),
            (40, 0, 40, 0),
        )
        self.assertEqual(
            (original.state, original.amount_total, original.amount_residual),
            ("posted", 100, 60),
        )
        self.assertNotEqual(credit.l10n_ae_fiscal_uuid, original.l10n_ae_fiscal_uuid)
        self.assertEqual(
            (
                original.l10n_ae_transmission_id.payload,
                original.l10n_ae_transmission_id.xml_content,
            ),
            original_evidence,
        )

    def _selfbilling_bill_with_agreement(self):
        bill = self.init_invoice("in_invoice", amounts=[100], taxes=self.tax_purchase_a)
        bill.journal_id.is_self_billing = True
        agreement = self.env["l10n_ae.selfbilling.agreement"].create(
            {
                "name": "SYNTH-AGREEMENT-0002",
                "partner_id": self.partner_a.id,
                "start_date": bill.invoice_date,
                "scope": "Synthetic services and adjustments",
                "onboarding_reference": "SYNTH-ONBOARD-0002",
            }
        )
        bill.write(
            {
                "l10n_ae_selfbilling_agreement_id": agreement.id,
                "l10n_ae_fiscal_uuid": "ddddddda-3333-4333-8333-333333333333",
                "l10n_ae_binding_id": review_controlled_binding(
                    self.env["l10n_ae.edi.binding"].create(
                        {"name": "Synthetic self-billing sandbox"}
                    ),
                    self.simple_accountman,
                    capabilities=CONTROLLED_CAPABILITIES,
                    note=(
                        "Synthetic controlled contract for _selfbilling_bill_with_agr"
                        "eement."
                    ),
                ).id,
            }
        )
        return bill, agreement
