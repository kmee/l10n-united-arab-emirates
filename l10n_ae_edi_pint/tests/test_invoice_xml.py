# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
import uuid
from decimal import Decimal

from lxml import etree

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}


@tagged("post_install", "-at_install")
class TestUaeInvoiceXml(AccountTestInvoicingCommon):
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

    def test_received_usd_partial_credit_preserves_aed_facts_and_original_reference(
        self,
    ):
        tree, _rate, source = self._foreign_received_fixture()
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming USD credit"}
        )
        inbox_model = self.env["l10n_ae.edi.inbox"]
        original = inbox_model.receive_controlled_document(
            binding.id,
            "usd-credit-original",
            etree.tostring(tree),
        )
        self.assertTrue(original.action_process(), original.diagnostic)
        credit = source.copy(
            {
                "move_type": "out_refund",
                "invoice_date": source.invoice_date,
                "reversed_entry_id": source.id,
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "aaaaaaac-5555-4555-8555-555555555555",
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": source.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-RECEIVED-CREDIT-QUOTE",
            }
        )
        credit.invoice_line_ids.price_unit = 40
        credit.action_post()
        content = etree.tostring(self._received_test_xml(credit))
        incoming = inbox_model.receive_controlled_document(
            binding.id, "usd-credit", content
        )
        self.assertTrue(incoming.action_process(), incoming.diagnostic)
        refund = incoming.move_id
        self.assertEqual((refund.move_type, refund.state), ("in_refund", "draft"))
        self.assertEqual((refund.currency_id.name, refund.amount_total), ("USD", 42))
        self.assertEqual(refund.reversed_entry_id, original.move_id)
        self.assertEqual(refund.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertEqual(refund.l10n_ae_received_tax_exchange_rate, "3.672500")
        self.assertEqual(
            (
                refund.l10n_ae_received_tax_amount,
                refund.l10n_ae_received_tax_inclusive_amount,
            ),
            (7.35, 154.25),
        )
        self.assertEqual(incoming.original_xml.content, content)
        self.assertFalse(refund.l10n_ae_tax_exchange_rate)
        self.assertFalse(refund.l10n_ae_transmission_id)

    def test_received_inactive_document_currency_stays_in_review(self):
        tree, _rate, _source = self._foreign_received_fixture()
        self.env.ref("base.CHF").active = False
        tree.find("cbc:DocumentCurrencyCode", NS).text = "CHF"
        tree.find("cac:TaxExchangeRate/cbc:SourceCurrencyCode", NS).text = "CHF"
        for node in tree.xpath("//*[@currencyID='USD']"):
            node.set("currencyID", "CHF")
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Inactive CHF"})
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id,
            "inactive-chf",
            etree.tostring(tree),
        )
        self.assertFalse(inbox.action_process())
        self.assertEqual(inbox.state, "review")
        self.assertIn("document currency", inbox.diagnostic)
        self.assertFalse(inbox.move_id)
        self.assertFalse(self.env.ref("base.CHF").active)

    def test_received_usd_bill_preserves_sender_aed_facts_separately_from_local_review(
        self,
    ):
        tree, rate, _source = self._foreign_received_fixture()
        content = etree.tostring(tree)
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming USD evidence"}
        )
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id,
            "received-usd-1",
            content,
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        bill = inbox.move_id
        self.assertEqual(bill.currency_id, self.env.ref("base.USD"))
        self.assertEqual(
            (bill.move_type, bill.state, bill.amount_total),
            ("in_invoice", "draft", 105),
        )
        self.assertEqual(bill.l10n_ae_received_currency_id, self.env.ref("base.USD"))
        self.assertEqual(bill.l10n_ae_received_payable_amount, 105)
        self.assertEqual(bill.l10n_ae_received_tax_exchange_rate, "3.672500")
        self.assertEqual(
            bill.l10n_ae_received_fiscal_currency_id, self.env.ref("base.AED")
        )
        self.assertEqual(
            (
                bill.l10n_ae_received_tax_amount,
                bill.l10n_ae_received_tax_inclusive_amount,
            ),
            (18.36, 385.61),
        )
        self.assertFalse(bill.l10n_ae_tax_exchange_rate)
        self.assertFalse(bill.l10n_ae_tax_exchange_date)
        self.assertFalse(bill.l10n_ae_tax_exchange_source)
        self.assertEqual(inbox.original_xml.content, content)
        for name, value in (
            ("l10n_ae_received_tax_exchange_rate", "3.700000"),
            ("l10n_ae_received_fiscal_currency_id", self.env.ref("base.USD").id),
            ("l10n_ae_received_tax_amount", 19),
            ("l10n_ae_received_tax_inclusive_amount", 400),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(UserError, "received"):
                bill.write({name: value})
        rate.rate = 1 / 3.7
        self.assertEqual(bill.l10n_ae_received_tax_exchange_rate, "3.672500")
        self.assertEqual(bill.l10n_ae_received_tax_amount, 18.36)
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": binding.id,
            }
        )
        bill.action_post()
        self.assertFalse(bill.l10n_ae_transmission_id)

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

    def test_received_free_zone_preserves_beneficiary_without_changing_vat(self):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        source.write(
            {
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000003",
                "l10n_ae_fiscal_uuid": "aaaaaaae-1111-4111-8111-111111111111",
            }
        )
        content = etree.tostring(self._received_test_xml(source))
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Received Free Zone"})
        inbox_model = self.env["l10n_ae.edi.inbox"]
        inbox = inbox_model.receive_controlled_document(
            binding.id, "received-free-zone", content
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        bill = inbox.move_id
        self.assertEqual((bill.move_type, bill.state), ("in_invoice", "draft"))
        self.assertTrue(bill.l10n_ae_free_zone)
        self.assertEqual(bill.l10n_ae_beneficiary_id, "1000000003")
        self.assertEqual(
            (bill.amount_untaxed, bill.amount_tax, bill.amount_total), (100, 5, 105)
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(
            inbox_model.receive_controlled_document(
                binding.id, "received-free-zone", content
            ),
            inbox,
        )
        for values in (
            {"l10n_ae_free_zone": False},
            {"l10n_ae_beneficiary_id": "1000000004"},
            {"l10n_ae_exports": True},
        ):
            with self.assertRaisesRegex(UserError, "received"):
                bill.write(values)
        self.env.company.write(
            {"l10n_ae_edi_auto_prepare": True, "l10n_ae_edi_binding_id": binding.id}
        )
        bill.action_post()
        self.assertFalse(bill.l10n_ae_transmission_id)

    def test_duplicate_received_beneficiary_requires_review_without_selecting_first(
        self,
    ):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        source.write(
            {
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000003",
                "l10n_ae_fiscal_uuid": "aaaaaaae-2222-4222-8222-222222222222",
            }
        )
        original = etree.tostring(self._received_test_xml(source))
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Ambiguous received beneficiary"}
        )
        before = self.env["account.move"].search_count(
            [("move_type", "=", "in_invoice")]
        )
        for index, path in enumerate(
            (
                "cac:BuyerCustomerParty/cac:Party/cac:PartyIdentification",
                "cac:BuyerCustomerParty/cac:Party",
                "cac:BuyerCustomerParty",
            )
        ):
            tree = etree.fromstring(original)
            node = tree.find(path, NS)
            node.addnext(etree.fromstring(etree.tostring(node)))
            content = etree.tostring(tree)
            inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
                binding.id, f"ambiguous-beneficiary-{index}", content
            )
            with self.subTest(path=path):
                self.assertFalse(inbox.action_process())
                self.assertEqual(inbox.state, "review")
                self.assertIn("beneficiary", inbox.diagnostic)
                self.assertFalse(inbox.move_id)
                self.assertEqual(inbox.original_xml.content, content)
                self.assertEqual(
                    self.env["account.move"].search_count(
                        [("move_type", "=", "in_invoice")]
                    ),
                    before,
                )

    def test_received_fx_header_discrepancies_stay_in_review_without_a_draft(self):
        original, _rate, _source = self._foreign_received_fixture()
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming FX arithmetic"}
        )
        cases = (
            ("cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']", "19.00"),
            (
                "cac:AdditionalDocumentReference[cbc:DocumentTypeCode='aedtotal-incl-vat']/cbc:DocumentDescription",
                "385.62",
            ),
            ("cac:TaxExchangeRate/cbc:CalculationRate", "0"),
            ("cac:TaxExchangeRate/cbc:CalculationRate", "3.700000"),
            (
                "cac:AdditionalDocumentReference[cbc:DocumentTypeCode='aedtotal-incl-vat']/cbc:DocumentDescription",
                "not-a-number",
            ),
            (
                "cac:AdditionalDocumentReference[cbc:DocumentTypeCode='aedtotal-incl-vat']/cbc:DocumentDescription",
                "NaN",
            ),
        )
        before = self.env["account.move"].search_count([])
        for index, (xpath, value) in enumerate(cases):
            with self.subTest(index=index):
                tree = etree.fromstring(etree.tostring(original))
                tree.find("cbc:UUID", NS).text = f"aaaaaaac-2222-4222-8222-{index:012d}"
                tree.find(xpath, NS).text = value
                content = etree.tostring(tree)
                inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
                    binding.id,
                    f"bad-fx-header-{index}",
                    content,
                )
                self.assertFalse(inbox.action_process())
                self.assertEqual(inbox.state, "review")
                self.assertFalse(inbox.move_id)
                self.assertIn("fiscal AED", inbox.diagnostic)
                self.assertEqual(inbox.original_xml.content, content)
                self.assertEqual(self.env["account.move"].search_count([]), before)

    def test_received_fx_line_currency_and_conversion_require_review(self):
        original, _rate, _source = self._foreign_received_fixture()
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming FX line arithmetic"}
        )
        cases = (
            ("cac:InvoiceLine/cac:ItemPriceExtension/cbc:Amount", "385.62", None),
            (
                "cac:InvoiceLine/cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount",
                "18.37",
                None,
            ),
            ("cac:InvoiceLine/cac:ItemPriceExtension/cbc:Amount", None, "USD"),
            (
                "cac:InvoiceLine/cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount",
                None,
                "USD",
            ),
        )
        for index, (xpath, value, currency) in enumerate(cases):
            with self.subTest(index=index):
                tree = etree.fromstring(etree.tostring(original))
                tree.find("cbc:UUID", NS).text = f"aaaaaaac-3333-4333-8333-{index:012d}"
                node = tree.find(xpath, NS)
                if value:
                    node.text = value
                if currency:
                    node.set("currencyID", currency)
                content = etree.tostring(tree)
                inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
                    binding.id,
                    f"bad-fx-line-{index}",
                    content,
                )
                self.assertFalse(inbox.action_process())
                self.assertEqual(inbox.state, "review")
                self.assertFalse(inbox.move_id)
                self.assertIn("fiscal AED", inbox.diagnostic)
                self.assertEqual(inbox.original_xml.content, content)

    def test_received_fractional_quantity_retains_unrounded_aed_line_values(self):
        tree, _rate, _source = self._foreign_received_fixture()
        # Independently worked incoming vector, not derived from the local emitter:
        # 10 * 1.00001 = 10.0001 USD; VAT .500005; gross 10.500105.
        # Raw AED gross 38.5616356125 / VAT 1.8362683625; headers38.56/1.84.
        values = {
            "cbc:UUID": "aaaaaaac-4444-4444-8444-444444444444",
            "cac:InvoiceLine/cbc:InvoicedQuantity": "1.00001",
            "cac:InvoiceLine/cbc:LineExtensionAmount": "10.00",
            "cac:InvoiceLine/cac:Price/cbc:PriceAmount": "10.0",
            "cac:InvoiceLine/cac:Price/cac:AllowanceCharge/cbc:BaseAmount": "10.0",
            "cac:InvoiceLine/cac:ItemPriceExtension/cbc:Amount": "38.5616356125",
            "cac:InvoiceLine/cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount": (
                "1.8362683625"
            ),
            "cac:TaxTotal/cbc:TaxAmount[@currencyID='USD']": "0.50",
            "cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']": "1.84",
            "cac:TaxTotal/cac:TaxSubtotal/cbc:TaxableAmount": "10.00",
            "cac:TaxTotal/cac:TaxSubtotal/cbc:TaxAmount": "0.50",
            "cac:LegalMonetaryTotal/cbc:LineExtensionAmount": "10.00",
            "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount": "10.00",
            "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount": "10.50",
            "cac:LegalMonetaryTotal/cbc:PayableAmount": "10.50",
            (
                "cac:AdditionalDocumentReference"
                "[cbc:DocumentTypeCode='aedtotal-incl-vat']/cbc:DocumentDescription"
            ): "38.56",
        }
        for xpath, value in values.items():
            tree.find(xpath, NS).text = value
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming fractional quantity"}
        )
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id,
            "fractional-quantity",
            etree.tostring(tree),
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        bill = inbox.move_id
        self.assertEqual(bill.invoice_line_ids.quantity, 1.00001)
        self.assertEqual(bill.amount_total, 10.50)
        self.assertEqual(
            (
                bill.l10n_ae_received_tax_amount,
                bill.l10n_ae_received_tax_inclusive_amount,
            ),
            (1.84, 38.56),
        )

    def test_received_partial_credit_links_the_supplier_bill_and_preserves_reason(self):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        original.l10n_ae_fiscal_uuid = "aaaaaaab-1111-4111-8111-111111111111"
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming supplier adjustment"}
        )
        inbox_model = self.env["l10n_ae.edi.inbox"]
        received = inbox_model.receive_controlled_document(
            binding.id,
            "original-bill",
            etree.tostring(self._received_test_xml(original)),
        )
        self.assertTrue(received.action_process(), received.diagnostic)
        bill = received.move_id
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
        reversal.refund_moves()
        credit = reversal.new_move_ids
        credit.invoice_line_ids.write({"price_unit": 40})
        credit.write(
            {
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "aaaaaaab-2222-4222-8222-222222222222",
            }
        )
        credit.action_post()
        content = etree.tostring(self._received_test_xml(credit))
        inbox = inbox_model.receive_controlled_document(
            binding.id, "partial-credit", content
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        refund = inbox.move_id
        self.assertEqual(refund.move_type, "in_refund")
        self.assertEqual(refund.state, "draft")
        self.assertEqual(refund.partner_id, self.partner_a)
        self.assertEqual(refund.reversed_entry_id, bill)
        self.assertEqual(refund.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertEqual(refund.amount_untaxed, 40)
        self.assertEqual(refund.amount_tax, 2)
        self.assertEqual(refund.amount_total, 42)
        self.assertEqual(refund.l10n_ae_received_prepaid_amount, 42)
        self.assertEqual(refund.l10n_ae_received_payable_amount, 0)
        self.assertEqual(bill.amount_total, 105)
        self.assertEqual(bill.state, "draft")
        self.assertEqual(inbox.original_xml.content, content)
        self.assertTrue(refund.l10n_ae_received_document)
        self.assertFalse(refund.l10n_ae_transmission_id)
        self.assertTrue(inbox.action_process())
        self.assertEqual(inbox.move_id, refund)
        with self.assertRaisesRegex(UserError, "received"):
            refund.write({"l10n_ae_received_payable_amount": 1})
        with self.assertRaisesRegex(UserError, "received"):
            refund.write({"l10n_ae_credit_reason": "DL8.61.1.A"})
        self.env.company.write(
            {"l10n_ae_edi_auto_prepare": True, "l10n_ae_edi_binding_id": binding.id}
        )
        refund.action_post()
        self.assertFalse(refund.l10n_ae_transmission_id)
        self.assertEqual(bill.state, "draft")

    def test_received_supplier_endpoint_must_match_the_known_vat_partner(self):
        tree = self._received_test_xml()
        tree.find(
            "cac:AccountingSupplierParty/cac:Party/cbc:EndpointID", NS
        ).text = "1000000009"
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming endpoint check"}
        )
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id, "supplier-mismatch", etree.tostring(tree)
        )
        inbox.action_process()
        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)
        self.assertIn("supplier", inbox.diagnostic)

    def test_received_declared_amounts_keep_the_xml_currency_during_accounting_review(
        self,
    ):
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Received currency evidence"}
        )
        content = etree.tostring(self._received_test_xml())
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id, "received-currency", content
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        bill = inbox.move_id
        usd = self.env.ref("base.USD")
        usd.active = True
        bill.currency_id = usd
        self.assertEqual(bill.currency_id, usd)
        self.assertEqual(bill.l10n_ae_received_currency_id, self.env.ref("base.AED"))
        self.assertEqual(bill.l10n_ae_received_payable_amount, 105)
        self.assertEqual(inbox.original_xml.content, content)
        with self.assertRaisesRegex(UserError, "received"):
            bill.write({"l10n_ae_received_currency_id": usd.id})

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

    def test_received_commercial_invoice_matches_non_vat_supplier_registration(
        self,
    ):
        _source, purchase_tax, tree = self._commercial_received_fixture()
        self.assertIsNone(
            tree.find("cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme", NS)
        )
        content = etree.tostring(tree)
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming commercial document"}
        )
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id, "non-vat-commercial", content
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        bill = inbox.move_id
        self.assertEqual(bill.move_type, "in_invoice")
        self.assertEqual(bill.state, "draft")
        self.assertEqual(bill.partner_id, self.partner_a)
        self.assertEqual(bill.l10n_ae_document_category, "commercial")
        self.assertEqual(bill.amount_untaxed, 100)
        self.assertEqual(bill.amount_tax, 0)
        self.assertEqual(bill.amount_total, 100)
        self.assertEqual(bill.invoice_line_ids.tax_ids, purchase_tax)
        self.assertEqual(bill.l10n_ae_received_currency_id, self.env.ref("base.AED"))
        self.assertFalse(self.partner_a.vat)
        self.assertEqual(inbox.original_xml.content, content)
        with self.assertRaisesRegex(UserError, "received"):
            bill.write({"l10n_ae_document_category": "tax"})

    def test_non_vat_supplier_with_a_different_license_stays_in_review(self):
        source, _purchase_tax, tree = self._commercial_received_fixture()
        tree.find(
            "cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:CompanyID",
            NS,
        ).text = "UNMAPPED-LICENSE-0042"
        content = etree.tostring(tree)
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming unknown license"}
        )
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id, "unknown-license", content
        )
        self.assertFalse(inbox.action_process())
        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertIn("supplier", inbox.diagnostic)
        self.assertFalse(
            self.env["account.move"].search(
                [
                    ("move_type", "=", "in_invoice"),
                    ("ref", "=", source.name),
                    ("company_id", "=", self.env.company.id),
                ]
            )
        )

    def test_received_commercial_credit_links_the_non_vat_supplier_bill(
        self,
    ):
        source, purchase_tax, tree = self._commercial_received_fixture()
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming commercial adjustment"}
        )
        model = self.env["l10n_ae.edi.inbox"]
        original = model.receive_controlled_document(
            binding.id, "commercial-original", etree.tostring(tree)
        )
        self.assertTrue(original.action_process(), original.diagnostic)
        credit = self.init_invoice(
            "out_refund", amounts=[40], taxes=source.invoice_line_ids.tax_ids
        )
        credit.write(
            {
                "reversed_entry_id": source.id,
                "l10n_ae_document_category": "commercial",
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "ccccccca-2222-4222-8222-222222222222",
            }
        )
        credit.action_post()
        credit_tree = self._received_test_xml(credit)
        self.assertEqual(
            credit_tree.findtext("cbc:CreditNoteTypeCode", namespaces=NS), "81"
        )
        content = etree.tostring(credit_tree)
        inbox = model.receive_controlled_document(
            binding.id, "commercial-credit", content
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        refund = inbox.move_id
        self.assertEqual(refund.state, "draft")
        self.assertEqual(refund.move_type, "in_refund")
        self.assertEqual(refund.partner_id, self.partner_a)
        self.assertEqual(refund.l10n_ae_document_category, "commercial")
        self.assertEqual(refund.reversed_entry_id, original.move_id)
        self.assertEqual(refund.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertEqual(refund.invoice_line_ids.tax_ids, purchase_tax)
        self.assertEqual(refund.amount_total, 40)
        self.assertEqual(refund.amount_tax, 0)
        self.assertEqual(original.move_id.amount_total, 100)
        self.assertEqual(original.move_id.state, "draft")
        self.assertEqual(inbox.original_xml.content, content)
        self.assertTrue(inbox.action_process())
        self.assertEqual(inbox.move_id, refund)

    def test_received_credit_with_a_wrong_original_date_stays_in_review(self):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        original.l10n_ae_fiscal_uuid = "bbbbbbba-1111-4111-8111-111111111111"
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming credit reference review"}
        )
        inbox_model = self.env["l10n_ae.edi.inbox"]
        received = inbox_model.receive_controlled_document(
            binding.id,
            "original-bill",
            etree.tostring(self._received_test_xml(original)),
        )
        self.assertTrue(received.action_process(), received.diagnostic)
        credit = self.init_invoice("out_refund", amounts=[40], taxes=self.tax_sale_a)
        credit.write(
            {
                "reversed_entry_id": original.id,
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "bbbbbbba-2222-4222-8222-222222222222",
            }
        )
        credit.action_post()
        tree = self._received_test_xml(credit)
        tree.find(
            "cac:BillingReference/cac:InvoiceDocumentReference/cbc:IssueDate", NS
        ).text = "2000-01-01"
        content = etree.tostring(tree)
        inbox = inbox_model.receive_controlled_document(
            binding.id, "wrong-date", content
        )
        self.assertFalse(inbox.action_process())
        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertIn("original supplier bill", inbox.diagnostic)
        self.assertEqual(received.move_id.amount_total, 105)

    def test_received_identical_xml_with_another_remote_id_reuses_the_bill(self):
        content = etree.tostring(self._received_test_xml())
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming duplicate check"}
        )
        inbox_model = self.env["l10n_ae.edi.inbox"]
        first = inbox_model.receive_controlled_document(
            binding.id, "remote-first", content
        )
        second = inbox_model.receive_controlled_document(
            binding.id, "remote-second", content
        )
        first.action_process()
        second.action_process()
        self.assertEqual(first.state, "processed", first.diagnostic)
        self.assertEqual(second.state, "processed", second.diagnostic)
        self.assertEqual(first.move_id, second.move_id)

    def test_unsafe_received_xml_is_preserved_without_creating_a_bill(self):
        content = b'<!DOCTYPE Invoice [<!ENTITY ex SYSTEM "file:///etc/passwd">]><Invoice>&ex;</Invoice>'
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Incoming XML check"})
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id, "unsafe-xml", content
        )
        inbox.action_process()
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)
        with self.assertRaises(UserError):
            inbox.write({"original_xml": False})
        with self.assertRaises(UserError):
            inbox.sudo().unlink()

    def test_readonly_accountant_cannot_receive_or_process_documents(self):
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming access check"}
        )
        reader = new_test_user(
            self.env,
            login="uae-inbox-readonly",
            groups="account.group_account_readonly",
        )
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id, "read-only", b"<Invoice/>"
        )
        with self.assertRaises(AccessError):
            inbox.with_user(reader).action_process()
        with self.assertRaises(AccessError):
            self.env["l10n_ae.edi.inbox"].with_user(reader).receive_controlled_document(
                binding.id, "read-only-2", b"<Invoice/>"
            )

    def test_received_document_bytes_are_isolated_from_other_companies(self):
        other = (
            self.env["res.company"]
            .with_context(install_demo=True)
            .create({"name": "Other received-document entity"})
        )
        foreign_binding = (
            self.env["l10n_ae.edi.binding"]
            .sudo()
            .create({"name": "Other receiving ASP", "company_id": other.id})
        )
        foreign = (
            self.env["l10n_ae.edi.inbox"]
            .sudo()
            .receive_controlled_document(
                foreign_binding.id, "foreign-original", b"<Invoice/>"
            )
        )
        reader = new_test_user(
            self.env,
            login="uae-inbox-company-reader",
            groups="account.group_account_readonly",
            company_id=self.env.company.id,
            company_ids=[(6, 0, self.env.company.ids)],
        )
        with self.assertRaises(AccessError):
            foreign.with_user(reader).with_context(
                allowed_company_ids=self.env.company.ids
            ).read(["original_xml"])

    def test_received_identity_with_changed_bytes_preserves_both_for_review(self):
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Inbound replay ASP"})
        inbox_model = self.env["l10n_ae.edi.inbox"]
        first = inbox_model.receive_controlled_document(
            binding.id, "same-id", b"<original/>"
        )
        repeated = inbox_model.receive_controlled_document(
            binding.id, "same-id", b"<original/>"
        )
        self.assertEqual(first, repeated)
        changed = inbox_model.receive_controlled_document(
            binding.id, "same-id", b"<changed/>"
        )
        self.assertNotEqual(first, changed)
        self.assertEqual(first.original_xml.content, b"<original/>")
        self.assertEqual(changed.original_xml.content, b"<changed/>")
        self.assertEqual(first.state, "review")
        self.assertEqual(changed.state, "review")
        self.assertFalse(changed.action_process())
        self.assertIn("identity", changed.diagnostic)

    def test_received_invoice_creates_one_draft_bill_for_the_known_supplier(self):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        source.l10n_ae_fiscal_uuid = "abcdefab-1111-4111-8111-111111111111"
        tree = etree.fromstring(source.preview_l10n_ae_pint().encode())
        supplier = tree.find("cac:AccountingSupplierParty", NS)
        customer = tree.find("cac:AccountingCustomerParty", NS)
        supplier_party, customer_party = supplier[0], customer[0]
        supplier.remove(supplier_party)
        customer.remove(customer_party)
        supplier.append(customer_party)
        customer.append(supplier_party)
        content = etree.tostring(tree)
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Inbound test ASP"})
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id, "received-invoice-1", content
        )
        inbox.action_process()
        bill = inbox.move_id
        self.assertEqual(inbox.state, "processed", inbox.diagnostic)
        self.assertEqual(bill.move_type, "in_invoice")
        self.assertEqual(bill.state, "draft")
        self.assertEqual(bill.partner_id, self.partner_a)
        self.assertEqual(bill.company_id, self.env.company)
        self.assertEqual(bill.amount_untaxed, 100)
        self.assertEqual(bill.amount_tax, 5)
        self.assertEqual(bill.amount_total, 105)
        self.assertEqual(bill.ref, source.name)
        self.assertTrue(bill.l10n_ae_received_document)
        with self.assertRaisesRegex(UserError, "received"):
            bill.write({"l10n_ae_fiscal_uuid": "ffffffff-ffff-4fff-8fff-ffffffffffff"})
        inbox.action_process()
        self.assertEqual(inbox.move_id, bill)
        bill.action_post()
        with self.assertRaisesRegex(UserError, "received"):
            bill.action_l10n_ae_prepare()

    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
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

    def test_usd_invoice_preserves_the_quote_and_unrounded_line_aed_amounts(self):
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
            "out_invoice",
            amounts=[100],
            taxes=self.tax_sale_a,
            currency=usd,
            post=True,
        )
        invoice.write(
            {
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": invoice.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-USD-AED-2019-01-01",
                "l10n_ae_fiscal_uuid": "ddddddda-1111-4111-8111-111111111111",
            }
        )
        tree = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        self.assertEqual(tree.findtext("cbc:TaxCurrencyCode", namespaces=NS), "AED")
        exchange = tree.find("cac:TaxExchangeRate", NS)
        self.assertIsNotNone(exchange)
        self.assertEqual(
            exchange.findtext("cbc:SourceCurrencyCode", namespaces=NS), "USD"
        )
        self.assertEqual(
            exchange.findtext("cbc:TargetCurrencyCode", namespaces=NS), "AED"
        )
        self.assertEqual(
            exchange.findtext("cbc:CalculationRate", namespaces=NS), "3.672500"
        )
        self.assertEqual(
            Decimal(
                tree.findtext(
                    "cac:InvoiceLine/cac:ItemPriceExtension/cbc:Amount", namespaces=NS
                )
            ),
            Decimal("385.612500"),
        )
        self.assertEqual(
            Decimal(
                tree.findtext(
                    "cac:InvoiceLine/cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount",
                    namespaces=NS,
                )
            ),
            Decimal("18.362500"),
        )
        self.assertEqual(
            tree.findtext(
                "cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']", namespaces=NS
            ),
            "18.36",
        )
        self.assertEqual(
            tree.findtext(
                "cac:AdditionalDocumentReference[cbc:DocumentTypeCode='aedtotal-incl-vat']/cbc:DocumentDescription",
                namespaces=NS,
            ),
            "385.61",
        )
        self.assertEqual(invoice.amount_total, 105)
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_invalid_aed_quotes_are_rejected_without_silent_rate_rounding(self):
        usd = self.env.ref("base.USD")
        usd.active = True
        invoice = self.init_invoice(
            "out_invoice",
            amounts=[100],
            taxes=self.tax_sale_a,
            currency=usd,
            post=True,
        )
        invoice.write(
            {
                "l10n_ae_tax_exchange_date": invoice.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-INVALID-QUOTE",
            }
        )
        for value in ("0", "-1", "NaN", "Infinity", "1E-5", "3.6725001"):
            with self.subTest(value=value):
                invoice.l10n_ae_tax_exchange_rate = value
                with self.assertRaisesRegex(UserError, "positive decimal.*six"):
                    invoice.preview_l10n_ae_pint()

    def test_prepared_foreign_invoice_freezes_exchange_value_date_source_and_xml(self):
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
        invoice = self.init_invoice(
            "out_invoice",
            amounts=[100],
            taxes=self.tax_sale_a,
            currency=usd,
            post=True,
        )
        invoice.write(
            {
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": invoice.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-FROZEN-QUOTE",
                "l10n_ae_fiscal_uuid": "ddddddda-2222-4222-8222-222222222222",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "FX quote"})
                .id,
            }
        )
        invoice.action_l10n_ae_prepare()
        transmission = invoice.l10n_ae_transmission_id
        frozen = transmission.xml_content
        self.assertEqual(
            json.loads(transmission.payload)["tax_exchange"],
            {
                "source_currency": "USD",
                "target_currency": "AED",
                "rate": "3.672500",
                "date": "2019-01-01",
                "source": "SYNTH-FROZEN-QUOTE",
            },
        )
        for field, value in (
            ("l10n_ae_tax_exchange_rate", "3.700000"),
            ("l10n_ae_tax_exchange_date", "2019-01-02"),
            ("l10n_ae_tax_exchange_source", "changed source"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(UserError, "frozen"):
                invoice.write({field: value})
        rate.rate = 1 / 3.7
        self.assertEqual(invoice.preview_l10n_ae_pint(), frozen)
        self.assertEqual(transmission.xml_content, frozen)

    def test_an_inverted_quote_cannot_prepare_a_different_aed_accounting_obligation(
        self,
    ):
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
            "out_invoice",
            amounts=[100],
            taxes=self.tax_sale_a,
            currency=usd,
            post=True,
        )
        invoice.write(
            {
                "l10n_ae_tax_exchange_rate": "0.272294",
                "l10n_ae_tax_exchange_date": invoice.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-WRONG-DIRECTION",
                "l10n_ae_fiscal_uuid": "ddddddda-3333-4333-8333-333333333333",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "FX mismatch"})
                .id,
            }
        )
        with self.assertRaisesRegex(UserError, "AED.*accounting"):
            invoice.action_l10n_ae_prepare()
        self.assertFalse(invoice.l10n_ae_transmission_id)
        self.assertEqual(invoice.amount_total, 105)
        self.assertEqual(invoice.amount_total_signed, 385.61)

    def test_foreign_currency_cannot_emit_without_a_reviewed_aed_exchange_quote(self):
        usd = self.env.ref("base.USD")
        usd.active = True
        invoice = self.init_invoice(
            "out_invoice",
            amounts=[100],
            taxes=self.tax_sale_a,
            currency=usd,
            post=True,
        )
        with self.assertRaisesRegex(UserError, "AED exchange rate"):
            invoice.preview_l10n_ae_pint()

    def test_company_eur_ledger_does_not_replace_usd_to_aed_fiscal_currency(self):
        eur = self.env.ref("base.EUR")
        usd = self.env.ref("base.USD")
        eur.active = usd.active = True
        data = self.setup_other_company(
            name="Synthetic EUR accounting company",
            currency_id=eur.id,
        )
        company = data["company"]
        company.write({"l10n_ae_tin": "1000000007", "l10n_ae_tin_confirmed": True})
        company.partner_id.write(
            {
                "vat": "100000000700003",
                "street": "Synthetic supplier street",
                "city": "Dubai",
                "state_id": self.env.ref("base.state_ae_du").id,
                "routing_scheme": "0235",
                "routing_endpoint": "1000000007",
                "l10n_ae_registration_type": "TL",
                "l10n_ae_registration_authority": "Synthetic EUR Ledger Authority",
                "additional_identifiers": {"AE_TL": "SYNTH-EUR-LEDGER-7"},
            }
        )
        self.env["res.currency.rate"].create(
            {
                "currency_id": usd.id,
                "company_id": company.id,
                "name": "2019-01-01",
                "rate": 1 / 0.90,
            }
        )
        invoice = (
            self.env["account.move"]
            .with_company(company)
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "currency_id": usd.id,
                    "invoice_date": "2019-01-01",
                    "invoice_line_ids": [
                        (
                            0,
                            0,
                            {
                                "name": "Synthetic EUR ledger service",
                                "price_unit": 100,
                                "account_id": data["default_account_revenue"].id,
                                "tax_ids": [(6, 0, company.account_sale_tax_id.ids)],
                            },
                        )
                    ],
                    "l10n_ae_tax_exchange_rate": "3.672500",
                    "l10n_ae_tax_exchange_date": "2019-01-01",
                    "l10n_ae_tax_exchange_source": "SYNTH-SEPARATE-FISCAL-QUOTE",
                    "l10n_ae_fiscal_uuid": "ddddddda-4444-4444-8444-444444444444",
                }
            )
        )
        invoice.action_post()
        self.assertEqual(invoice.amount_total, 105)
        self.assertEqual(invoice.amount_total_signed, 94.50)
        tree = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        self.assertEqual(tree.findtext("cbc:TaxCurrencyCode", namespaces=NS), "AED")
        self.assertEqual(
            tree.findtext(
                "cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']", namespaces=NS
            ),
            "18.36",
        )
        self.assertIsNone(
            tree.find("cac:TaxTotal/cbc:TaxAmount[@currencyID='EUR']", NS)
        )
        self.assertEqual(
            tree.findtext(
                "cac:AdditionalDocumentReference[cbc:DocumentTypeCode='aedtotal-incl-vat']/cbc:DocumentDescription",
                namespaces=NS,
            ),
            "385.61",
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_settled_usd_credit_preserves_aed_gross_and_original_reference(self):
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
        original = self.init_invoice(
            "out_invoice",
            amounts=[100],
            taxes=self.tax_sale_a,
            currency=usd,
            post=True,
        )
        reversal = (
            self.env["account.move.reversal"]
            .with_context(
                active_model="account.move",
                active_ids=original.ids,
            )
            .create(
                {
                    "reason": "Synthetic USD partial credit",
                    "move_ids": [(6, 0, original.ids)],
                    "journal_id": original.journal_id.id,
                }
            )
        )
        reversal.refund_moves()
        credit = reversal.new_move_ids
        credit.invoice_line_ids.price_unit = 40
        credit.write(
            {
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": credit.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-CREDIT-REVIEWED-QUOTE",
                "l10n_ae_fiscal_uuid": "ddddddda-5555-4555-8555-555555555555",
            }
        )
        credit.action_post()
        self.assertEqual(credit.amount_total, 42)
        self.assertEqual(credit.amount_residual, 0)
        tree = etree.fromstring(credit.preview_l10n_ae_pint().encode())
        self.assertEqual(
            tree.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=NS),
            "0.00",
        )
        self.assertEqual(
            tree.findtext(
                "cac:AdditionalDocumentReference[cbc:DocumentTypeCode='aedtotal-incl-vat']/cbc:DocumentDescription",
                namespaces=NS,
            ),
            "154.25",
        )
        self.assertEqual(
            tree.findtext(
                "cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']", namespaces=NS
            ),
            "7.35",
        )
        self.assertEqual(
            tree.findtext(
                "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID",
                namespaces=NS,
            ),
            original.name,
        )
        report = credit.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_fractional_usd_vat_divergence_requires_review_without_a_rounding_patch(
        self,
    ):
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
            "out_invoice",
            amounts=[1.23, 1.23, 1.23],
            taxes=self.tax_sale_a,
            currency=usd,
            post=True,
        )
        invoice.write(
            {
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": invoice.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-FRACTIONAL-ROUNDING-REVIEW",
                "l10n_ae_fiscal_uuid": "ddddddda-6666-4666-8666-666666666666",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Fractional VAT review"})
                .id,
            }
        )
        original_totals = (invoice.amount_total, invoice.amount_tax_signed)
        with self.assertRaisesRegex(UserError, "AED.*accounting"):
            invoice.action_l10n_ae_prepare()
        self.assertFalse(invoice.l10n_ae_transmission_id)
        self.assertEqual(
            (invoice.amount_total, invoice.amount_tax_signed), original_totals
        )
        self.assertEqual(invoice.amount_tax, 0.18)

    def test_standard_tax_invoice_uses_ae_profile_and_core_accounting_totals(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        xml = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        self.assertEqual(etree.QName(xml).localname, "Invoice")
        self.assertEqual(
            xml.findtext("cbc:CustomizationID", namespaces=NS),
            "urn:peppol:pint:billing-1@ae-1",
        )
        self.assertEqual(
            xml.findtext("cbc:ProfileID", namespaces=NS), "urn:peppol:bis:billing"
        )
        self.assertEqual(xml.findtext("cbc:InvoiceTypeCode", namespaces=NS), "380")
        self.assertEqual(xml.findtext("cbc:DocumentCurrencyCode", namespaces=NS), "AED")
        self.assertEqual(
            xml.findtext(
                "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", namespaces=NS
            ),
            "100.00",
        )
        self.assertEqual(
            xml.findtext("cac:TaxTotal/cbc:TaxAmount", namespaces=NS), "5.00"
        )
        self.assertEqual(
            xml.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=NS),
            "105.00",
        )
        self.assertEqual(
            xml.findtext(
                "cac:InvoiceLine/cac:ItemPriceExtension/cbc:Amount", namespaces=NS
            ),
            "105.00",
        )
        self.assertEqual(
            xml.findtext(
                "cac:InvoiceLine/cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount",
                namespaces=NS,
            ),
            "5.00",
        )
        registration = xml.find(
            "cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:CompanyID",
            NS,
        )
        self.assertEqual(registration.text, "SYNTH-TRADE-0001")
        self.assertEqual(registration.get("schemeAgencyID"), "TL")
        self.assertEqual(
            registration.get("schemeAgencyName"), "Synthetic Free Zone Authority"
        )

    def test_standard_tax_invoice_passes_the_official_offline_rules(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        # Synthetic ASP-assigned value: separate from number/origin/request identity.
        invoice.l10n_ae_fiscal_uuid = "11111111-1111-4111-8111-111111111111"
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["xsd_valid"])
        self.assertEqual(
            [layer["name"] for layer in report["layers"]], ["shared", "ae"]
        )

    def test_standard_credit_note_preserves_original_invoice_and_reason(self):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        reversal = (
            self.env["account.move.reversal"]
            .with_context(active_model="account.move", active_ids=original.ids)
            .create(
                {
                    "reason": "Synthetic cancelled supply",
                    "move_ids": [(6, 0, original.ids)],
                    "journal_id": original.journal_id.id,
                }
            )
        )
        reversal.refund_moves()
        credit = reversal.new_move_ids
        credit.write(
            {
                "l10n_ae_credit_reason": "DL8.61.1.A",
                "l10n_ae_fiscal_uuid": "55555555-5555-4555-8555-555555555555",
            }
        )
        credit.action_post()
        xml = etree.fromstring(credit.preview_l10n_ae_pint().encode())
        self.assertEqual(
            xml.findtext("cac:DiscrepancyResponse/cbc:ResponseCode", namespaces=NS),
            "DL8.61.1.A",
        )
        self.assertEqual(
            xml.findtext(
                "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID",
                namespaces=NS,
            ),
            original.name,
        )
        self.assertEqual(
            xml.findtext(
                "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", namespaces=NS
            ),
            "105.00",
        )
        # Native full reversal reconciles the unpaid invoice against its credit.
        self.assertEqual(credit.amount_total, 105)
        self.assertEqual(credit.amount_residual, 0)
        self.assertEqual(
            xml.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=NS),
            "0.00",
        )
        report = credit.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_commercial_invoice_without_supplier_vat_passes_official_rules(self):
        self.env.company.partner_id.vat = False
        outside_scope = self.tax_sale_a.copy(
            {
                "name": "Synthetic Out of Scope",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=outside_scope)
        invoice.write(
            {
                "l10n_ae_document_category": "commercial",
                "l10n_ae_fiscal_uuid": "77777777-7777-4777-8777-777777777777",
            }
        )
        invoice.action_post()
        xml = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        self.assertEqual(xml.findtext("cbc:InvoiceTypeCode", namespaces=NS), "480")
        self.assertEqual(
            xml.findtext(
                "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:ID",
                namespaces=NS,
            ),
            "O",
        )
        self.assertEqual(
            xml.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=NS),
            "100.00",
        )
        self.assertIsNone(
            xml.find(
                "cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID",
                NS,
            )
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_tax_invoice_without_supplier_vat_is_rejected_instead_of_using_tin(self):
        self.env.company.partner_id.vat = False
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_fiscal_uuid = "88888888-8888-4888-8888-888888888888"
        report = invoice.validate_l10n_ae_pint()
        self.assertFalse(report["valid"])
        self.assertIn(
            "ibr-134-ae",
            [
                failure["code"]
                for layer in report["layers"]
                for failure in layer["failures"]
            ],
        )

    def test_formal_action_reports_missing_uuid_as_a_normative_diagnostic(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        with self.assertRaisesRegex(UserError, "ibr-193-ae"):
            invoice.action_l10n_ae_validate_pint()

    def test_controlled_preparation_assigns_one_uuid_without_preseeded_identity(self):
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Explicit UUID simulation",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        invoice.l10n_ae_binding_id = binding
        invoice.action_l10n_ae_prepare()
        self.assertTrue(invoice.l10n_ae_fiscal_uuid)
        assigned_uuid = invoice.l10n_ae_fiscal_uuid
        self.assertEqual(uuid.UUID(assigned_uuid).version, 4)
        transmission = invoice.l10n_ae_transmission_id
        self.assertEqual(json.loads(transmission.payload)["fiscal_uuid"], assigned_uuid)
        self.assertEqual(
            json.loads(transmission.payload)["fiscal_uuid_assignment"],
            {
                "policy": "controlled_local",
                "source": "controlled_local",
                "environment": "sandbox",
                "simulation": True,
            },
        )
        tree = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        self.assertEqual(tree.findtext("cbc:UUID", namespaces=NS), assigned_uuid)
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        payload, xml = transmission.payload, transmission.xml_content
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.l10n_ae_fiscal_uuid, assigned_uuid)
        self.assertEqual(invoice.l10n_ae_transmission_id, transmission)
        self.assertEqual(
            (transmission.payload, transmission.xml_content), (payload, xml)
        )
        self.assertFalse(transmission.attempt_ids)

    def test_failed_controlled_preparation_leaves_no_assigned_uuid(self):
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
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd, post=True
        )
        invoice.write(
            {
                "l10n_ae_tax_exchange_rate": "0.272294",
                "l10n_ae_tax_exchange_date": invoice.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-WRONG-DIRECTION",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create(
                    {
                        "name": "UUID failure simulation",
                        "fiscal_uuid_policy": "controlled_local",
                    }
                )
                .id,
            }
        )
        # Odoo assertRaises adds a savepoint; catch directly to verify the action
        # itself restores its UUID when the caller handles the failure.
        try:
            invoice.action_l10n_ae_prepare()
        except UserError as error:
            self.assertRegex(str(error), "AED.*accounting")
        else:
            self.fail("The inconsistent AED obligation must require review.")
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        self.assertFalse(invoice.l10n_ae_transmission_id)
        self.assertEqual(
            (invoice.state, invoice.amount_total_signed), ("posted", 385.61)
        )

    def test_partial_credit_uses_its_own_amount_and_preserves_original_reference(self):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
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
        reversal.refund_moves()
        credit = reversal.new_move_ids
        credit.invoice_line_ids.write({"price_unit": 40})
        credit.write(
            {
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "66666666-6666-4666-8666-666666666666",
            }
        )
        credit.action_post()
        xml = etree.fromstring(credit.preview_l10n_ae_pint().encode())
        # A credit of40+2 against100+5 leaves63 on the original invoice.
        self.assertEqual(credit.amount_total, 42)
        self.assertEqual(original.amount_residual, 63)
        self.assertEqual(
            xml.findtext(
                "cac:CreditNoteLine/cac:ItemPriceExtension/cbc:Amount", namespaces=NS
            ),
            "42.00",
        )
        self.assertEqual(
            xml.findtext(
                "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID",
                namespaces=NS,
            ),
            original.name,
        )
        report = credit.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_discount_and_quantity_keep_core_totals_and_pass_official_rules(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.invoice_line_ids.write({"quantity": 2, "discount": 10})
        invoice.l10n_ae_fiscal_uuid = "44444444-4444-4444-8444-444444444444"
        invoice.action_post()
        xml = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        # Two units at AED100 less 10%: net180, VAT9, payable189.
        self.assertEqual(invoice.amount_total, 189)
        self.assertEqual(
            xml.findtext(
                "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", namespaces=NS
            ),
            "180.00",
        )
        self.assertEqual(
            xml.findtext(
                "cac:InvoiceLine/cac:ItemPriceExtension/cbc:Amount", namespaces=NS
            ),
            "189.00",
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_partial_credit_preserves_two_allocated_original_references(self):
        first = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        second = self.init_invoice(
            "out_invoice", amounts=[200], taxes=self.tax_sale_a, post=True
        )
        before = [
            (move.name, move.amount_total, move.amount_residual)
            for move in (first, second)
        ]
        credit = self.init_invoice("out_refund", amounts=[50], taxes=self.tax_sale_a)
        credit.write(
            {
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "abababab-1111-4111-8111-111111111111",
                "l10n_ae_credit_reference_ids": [
                    (
                        0,
                        0,
                        {
                            "invoice_id": first.id,
                            "allocated_net": 30,
                            "allocated_tax": 1.5,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "invoice_id": second.id,
                            "allocated_net": 20,
                            "allocated_tax": 1,
                        },
                    ),
                ],
            }
        )
        credit.action_post()
        xml = etree.fromstring(credit.preview_l10n_ae_pint().encode())
        references = xml.findall(
            "cac:BillingReference/cac:InvoiceDocumentReference", NS
        )
        self.assertEqual(
            [
                (
                    node.findtext("cbc:ID", namespaces=NS),
                    node.findtext("cbc:IssueDate", namespaces=NS),
                )
                for node in references
            ],
            [
                (first.name, str(first.invoice_date)),
                (second.name, str(second.invoice_date)),
            ],
        )
        self.assertEqual(
            (credit.amount_untaxed, credit.amount_tax, credit.amount_total),
            (50, 2.5, 52.5),
        )
        self.assertEqual(
            xml.findtext(
                "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", namespaces=NS
            ),
            "52.50",
        )
        self.assertEqual(
            [
                (move.name, move.amount_total, move.amount_residual)
                for move in (first, second)
            ],
            before,
        )
        report = credit.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_credit_reference_allocations_must_match_the_credit_not_original_totals(
        self,
    ):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        credit = self.init_invoice("out_refund", amounts=[50], taxes=self.tax_sale_a)
        credit.write(
            {
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_credit_reference_ids": [
                    (
                        0,
                        0,
                        {
                            "invoice_id": original.id,
                            "allocated_net": 100,
                            "allocated_tax": 5,
                        },
                    )
                ],
            }
        )
        with self.assertRaisesRegex(UserError, "allocations must match"):
            credit.action_l10n_ae_validate()
        self.assertEqual((credit.state, credit.amount_total), ("draft", 52.5))
        self.assertEqual(
            (original.state, original.amount_total, original.amount_residual),
            ("posted", 105, 105),
        )
        self.assertFalse(credit.l10n_ae_transmission_id)

    def test_prepared_credit_protects_its_allocated_original_references(self):
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Allocated credit test"}
        )
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        credit = self.init_invoice("out_refund", amounts=[50], taxes=self.tax_sale_a)
        credit.write(
            {
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "abababab-2222-4222-8222-222222222222",
                "l10n_ae_binding_id": binding.id,
                "l10n_ae_credit_reference_ids": [
                    (
                        0,
                        0,
                        {
                            "invoice_id": original.id,
                            "allocated_net": 50,
                            "allocated_tax": 2.5,
                        },
                    )
                ],
            }
        )
        credit.action_post()
        credit.action_l10n_ae_prepare()
        transmission = credit.l10n_ae_transmission_id
        content, payload = transmission.xml_content, transmission.payload
        ref = credit.l10n_ae_credit_reference_ids
        with self.assertRaisesRegex(UserError, "frozen UAE credit"):
            ref.write({"allocated_net": 100})
        with self.assertRaisesRegex(UserError, "frozen UAE credit"):
            ref.unlink()
        with self.assertRaisesRegex(UserError, "frozen UAE"):
            credit.write({"l10n_ae_credit_reference_ids": [(5, 0, 0)]})
        with self.assertRaisesRegex(UserError, "frozen UAE credit"):
            self.env["l10n_ae.credit.reference"].create(
                {
                    "move_id": credit.id,
                    "number": "EXTERNAL-SYNTHETIC",
                    "issue_date": "2019-01-01",
                    "allocated_net": 1,
                }
            )
        self.assertEqual(
            (transmission.xml_content, transmission.payload), (content, payload)
        )
        snapshot = json.loads(payload)
        self.assertEqual(
            snapshot["preceding_invoices"],
            [
                {
                    "number": original.name,
                    "issue_date": str(original.invoice_date),
                    "allocated_net": "50.00",
                    "allocated_vat": "2.50",
                    "review_evidence": None,
                }
            ],
        )

    def test_local_credit_references_reject_unrelated_or_unposted_originals(self):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        credit = self.init_invoice("out_refund", amounts=[50], taxes=self.tax_sale_a)
        credit.write(
            {
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_credit_reference_ids": [
                    (
                        0,
                        0,
                        {
                            "invoice_id": original.id,
                            "allocated_net": 50,
                            "allocated_tax": 2.5,
                        },
                    )
                ],
            }
        )
        other_customer = self.init_invoice(
            "out_invoice",
            amounts=[100],
            taxes=self.tax_sale_a,
            partner=self.partner_b,
            post=True,
        )
        unposted = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        )
        purchase = self.init_invoice(
            "in_invoice", amounts=[100], taxes=self.tax_purchase_a, post=True
        )
        commercial = original.copy({"l10n_ae_document_category": "commercial"})
        commercial.action_post()
        usd = self.env.ref("base.USD")
        usd.active = True
        foreign = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd, post=True
        )
        ref = credit.l10n_ae_credit_reference_ids
        for invalid in (other_customer, unposted, purchase, commercial, foreign):
            with self.subTest(original=invalid.id):
                ref.invoice_id = invalid
                with self.assertRaisesRegex(UserError, "same company, counterparty"):
                    credit.action_l10n_ae_validate()
        ref.invoice_id = original
        self.assertTrue(credit.action_l10n_ae_validate())
        ref.number = "UNRELATED-SYNTHETIC-INVOICE"
        with self.assertRaisesRegex(UserError, "local original identifiers"):
            credit.action_l10n_ae_validate()
        ref.invoice_id = original
        self.assertEqual((credit.state, credit.amount_total), ("draft", 52.5))

    def test_external_credit_originals_need_reviewed_evidence_without_fake_moves(self):
        credit = self.init_invoice("out_refund", amounts=[50], taxes=self.tax_sale_a)
        credit.write(
            {
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "abababab-3333-4333-8333-333333333333",
                "l10n_ae_credit_reference_ids": [
                    (
                        0,
                        0,
                        {
                            "number": "LEGACY-SYNTH-A",
                            "issue_date": "2018-12-01",
                            "allocated_net": 30,
                            "allocated_tax": 1.5,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "number": "LEGACY-SYNTH-B",
                            "issue_date": "2018-12-02",
                            "allocated_net": 20,
                            "allocated_tax": 1,
                        },
                    ),
                ],
            }
        )
        with self.assertRaisesRegex(UserError, "reviewed evidence"):
            credit.action_l10n_ae_validate()
        credit.l10n_ae_credit_reference_ids.write(
            {"review_evidence": "SYNTHETIC-LEGACY-ORIGINALS-REVIEW"}
        )
        credit.action_post()
        xml = etree.fromstring(credit.preview_l10n_ae_pint().encode())
        self.assertEqual(
            xml.xpath(
                "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID/text()",
                namespaces=NS,
            ),
            ["LEGACY-SYNTH-A", "LEGACY-SYNTH-B"],
        )
        self.assertFalse(credit.l10n_ae_credit_reference_ids.invoice_id)
        self.assertEqual((credit.amount_total, credit.amount_residual), (52.5, 52.5))
        report = credit.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_commercial_standard_vat_is_rejected_before_posting_or_freezing(self):
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Invalid commercial simulation",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        self.env.company.write(
            {"l10n_ae_edi_auto_prepare": True, "l10n_ae_edi_binding_id": binding.id}
        )
        outside_scope = self.tax_sale_a.copy(
            {
                "name": "Synthetic outside-scope group child",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        mixed_group = self.tax_sale_a.copy(
            {
                "name": "Synthetic mixed commercial group",
                "amount_type": "group",
                "children_tax_ids": [(6, 0, (self.tax_sale_a | outside_scope).ids)],
            }
        )
        cases = [
            (move_type, taxes)
            for move_type in ("out_invoice", "out_refund")
            for taxes in (self.tax_sale_a, mixed_group)
        ]
        for move_type, taxes in cases:
            with self.subTest(move_type=move_type, taxes=taxes.name):
                move = self.init_invoice(move_type, amounts=[100], taxes=taxes)
                move.write(
                    {
                        "l10n_ae_document_category": "commercial",
                        "l10n_ae_credit_reason": "DL8.61.1.C"
                        if move_type == "out_refund"
                        else False,
                    }
                )
                with self.assertRaisesRegex(UserError, "IBR-122-AE"):
                    move.action_l10n_ae_validate()
                with self.assertRaisesRegex(UserError, "IBR-122-AE"):
                    move.action_post()
                self.assertEqual(move.state, "draft")
                self.assertEqual((move.amount_tax, move.amount_total), (5, 105))
                self.assertFalse(move.l10n_ae_fiscal_uuid)
                self.assertFalse(move.l10n_ae_transmission_id)

    def test_native_partial_commercial_credit_preserves_480_original_and_type_81(self):
        self.env.company.partner_id.vat = False
        self._assert_native_partial_commercial_credit()

    def test_registered_supplier_commercial_credit_preserves_outside_scope_category(
        self,
    ):
        self.assertTrue(self.env.company.partner_id.vat)
        self._assert_native_partial_commercial_credit()

    def _assert_native_partial_commercial_credit(self):
        outside_scope = self.tax_sale_a.copy(
            {
                "name": "Reviewed synthetic outside-scope sale",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        original = self.init_invoice("out_invoice", amounts=[100], taxes=outside_scope)
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Commercial credit local simulation",
                "fiscal_uuid_policy": "controlled_local",
            }
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

    def test_six_document_categories_preserve_type_and_process(self):
        cases = [
            ("out_invoice", "tax", "Invoice", "380", "billing"),
            ("out_refund", "tax", "CreditNote", "381", "billing"),
            ("out_invoice", "commercial", "Invoice", "480", "billing"),
            ("out_refund", "commercial", "CreditNote", "81", "billing"),
            ("in_invoice", "tax", "Invoice", "389", "selfbilling"),
            ("in_refund", "tax", "CreditNote", "261", "selfbilling"),
        ]
        for move_type, category, root, code, process in cases:
            with self.subTest(code=code):
                invoice = self.init_invoice(move_type, amounts=[100], taxes=False)
                if process == "selfbilling":
                    invoice.journal_id.is_self_billing = True
                    invoice.l10n_ae_selfbilling_agreement_id = self.env[
                        "l10n_ae.selfbilling.agreement"
                    ].create(
                        {
                            "name": "Synthetic self-billing agreement",
                            "partner_id": invoice.partner_id.commercial_partner_id.id,
                            "start_date": invoice.invoice_date,
                            "scope": "Synthetic test supplies and adjustments",
                            "onboarding_reference": "SYNTH-ONBOARD-0002",
                        }
                    )
                invoice.l10n_ae_document_category = category
                if root == "CreditNote":
                    invoice.l10n_ae_credit_reason = "DL8.61.1.C"
                invoice.action_post()
                xml = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
                self.assertEqual(etree.QName(xml).localname, root)
                node = "InvoiceTypeCode" if root == "Invoice" else "CreditNoteTypeCode"
                self.assertEqual(xml.findtext(f"cbc:{node}", namespaces=NS), code)
                self.assertEqual(
                    xml.findtext("cbc:CustomizationID", namespaces=NS),
                    f"urn:peppol:pint:{process}-1@ae-1",
                )
                self.assertEqual(
                    xml.findtext("cbc:ProfileID", namespaces=NS),
                    f"urn:peppol:bis:{process}",
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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Synthetic self-billing sandbox"})
                .id,
            }
        )
        return bill, agreement

    def test_self_billed_purchase_freezes_buyer_issuer_and_supplier_roles(self):
        bill, agreement = self._selfbilling_bill_with_agreement()
        bill.action_post()
        bill.action_l10n_ae_prepare()
        frozen = bill.l10n_ae_transmission_id
        snapshot = json.loads(frozen.payload)
        self.assertEqual(snapshot["issuer_role"], "buyer")
        self.assertEqual(snapshot["supplier"]["vat"], "100000000200003")
        self.assertEqual(snapshot["buyer"]["vat"], "100000000100003")
        self.assertEqual(
            snapshot["selfbilling_agreement"]["reference"], "SYNTH-AGREEMENT-0002"
        )
        xml = etree.fromstring(frozen.xml_content.encode())
        self.assertEqual(xml.findtext("cbc:InvoiceTypeCode", namespaces=NS), "389")
        self.assertEqual(
            xml.findtext(
                "cac:AccountingSupplierParty/cac:Party/cbc:EndpointID", namespaces=NS
            ),
            "1000000002",
        )
        self.assertEqual(
            xml.findtext(
                "cac:AccountingCustomerParty/cac:Party/cbc:EndpointID", namespaces=NS
            ),
            "1000000001",
        )
        report = bill.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["profile"], "selfbilling")
        expected = frozen.payload
        agreement.name = "Changed after preparation"
        self.partner_a.name = "Changed supplier after preparation"
        bill.action_l10n_ae_prepare()
        self.assertEqual(frozen.payload, expected)
        with self.assertRaisesRegex(UserError, "frozen"):
            bill.write({"l10n_ae_selfbilling_agreement_id": False})

    def test_native_self_billed_partial_credit_requires_reviewed_agreement_and_261(
        self,
    ):
        bill, agreement = self._selfbilling_bill_with_agreement()
        binding = bill.l10n_ae_binding_id
        binding.fiscal_uuid_policy = "controlled_local"
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
        with self.assertRaisesRegex(UserError, "self-billing agreement"):
            credit.action_post()
        self.assertEqual(credit.state, "draft")
        self.assertFalse(credit.l10n_ae_fiscal_uuid)
        self.assertFalse(credit.l10n_ae_transmission_id)
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

    def test_self_billing_requires_vat_registration_for_both_economic_parties(self):
        bill, _agreement = self._selfbilling_bill_with_agreement()
        for party in (self.env.company.partner_id, self.partner_a):
            with self.subTest(party=party.id):
                original_vat = party.vat
                party.vat = False
                with self.assertRaisesRegex(UserError, "VAT registration"):
                    bill.action_l10n_ae_validate()
                party.vat = original_vat

    def test_self_billing_cannot_use_a_supplier_placeholder_endpoint(self):
        bill, _agreement = self._selfbilling_bill_with_agreement()
        self.partner_a.routing_endpoint = "9900000098"
        with self.assertRaisesRegex(UserError, "onboarding"):
            bill.action_l10n_ae_validate()

    def test_self_billing_does_not_emit_commercial_documents(self):
        bill, _agreement = self._selfbilling_bill_with_agreement()
        bill.l10n_ae_document_category = "commercial"
        with self.assertRaisesRegex(UserError, "tax documents"):
            bill.action_l10n_ae_validate()

    def test_self_billing_on_a_sales_journal_cannot_invert_economic_roles(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.journal_id.is_self_billing = True
        with self.assertRaisesRegex(UserError, "purchase"):
            invoice.action_l10n_ae_validate()

    def test_self_billing_posting_is_atomic_when_agreement_is_outside_its_term(self):
        bill, agreement = self._selfbilling_bill_with_agreement()
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": bill.l10n_ae_binding_id.id,
            }
        )
        agreement.write({"start_date": "2000-01-01", "end_date": "2000-12-31"})
        with self.assertRaisesRegex(UserError, "self-billing agreement"):
            bill.action_post()
        self.assertEqual(bill.state, "draft")
        self.assertFalse(bill.l10n_ae_transmission_id)

    def test_readonly_accountant_cannot_authorize_a_self_billing_agreement(self):
        _bill, agreement = self._selfbilling_bill_with_agreement()
        reader = new_test_user(
            self.env,
            login="selfbilling-review-reader",
            groups="account.group_account_readonly",
        )
        with self.assertRaises(AccessError):
            agreement.with_user(reader).write({"end_date": "2099-12-31"})
        foreign = self.env["res.company"].create({"name": "Foreign agreement entity"})
        agreement.company_id = foreign
        with self.assertRaises(AccessError):
            agreement.with_user(reader).with_context(
                allowed_company_ids=self.env.company.ids
            ).read(["scope"])

    def test_deemed_supply_routes_to_97_without_changing_the_actual_buyer(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_deemed_supply": True,
                "l10n_ae_fiscal_uuid": "aaaaaaae-1111-4111-8111-111111111111",
            }
        )
        tree = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        buyer = tree.find("cac:AccountingCustomerParty/cac:Party", NS)
        endpoint = buyer.find("cbc:EndpointID", NS)
        self.assertEqual(
            (endpoint.get("schemeID"), endpoint.text), ("0235", "9900000097")
        )
        self.assertEqual(
            tree.findtext("cbc:ProfileExecutionID", namespaces=NS), "01000000"
        )
        self.assertEqual(
            buyer.findtext("cac:PartyTaxScheme/cbc:CompanyID", namespaces=NS),
            "100000000200003",
        )
        self.assertEqual(self.partner_a.routing_endpoint, "1000000002")
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_prepared_deemed_supply_preserves_effective_and_master_destinations(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_deemed_supply": True,
                "l10n_ae_fiscal_uuid": "aaaaaaae-2222-4222-8222-222222222222",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Deemed supply destination"})
                .id,
            }
        )
        invoice.action_l10n_ae_prepare()
        transmission = invoice.l10n_ae_transmission_id
        payload = transmission.payload
        xml = transmission.xml_content
        snapshot = json.loads(payload)
        self.assertEqual(
            snapshot.get("destination"),
            {"scheme": "0235", "endpoint": "9900000097", "reason": "deemed_supply"},
        )
        self.assertEqual(snapshot["buyer"]["endpoint"], "1000000002")
        self.partner_a.routing_endpoint = "1000000004"
        self.assertEqual(invoice.preview_l10n_ae_pint(), xml)
        self.assertEqual(transmission.payload, payload)
        self.assertEqual((invoice.amount_tax, invoice.amount_total), (5, 105))

    def test_commercial_deemed_supply_requires_review_even_with_outside_scope_tax(self):
        outside_scope = self.tax_sale_a.copy(
            {
                "name": "Synthetic deemed commercial outside-scope tax",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=outside_scope)
        invoice.write(
            {"l10n_ae_document_category": "commercial", "l10n_ae_deemed_supply": True}
        )
        with self.assertRaisesRegex(UserError, "IBR-157-AE"):
            invoice.action_l10n_ae_validate()
        self.assertFalse(invoice.l10n_ae_transmission_id)
        self.assertEqual(
            (invoice.state, invoice.amount_tax, invoice.amount_total), ("draft", 0, 100)
        )

    def test_deemed_self_billing_requires_review_before_changing_issuer_routing(self):
        bill, _agreement = self._selfbilling_bill_with_agreement()
        bill.l10n_ae_deemed_supply = True
        with self.assertRaisesRegex(UserError, "self-billing.*routing"):
            bill.action_l10n_ae_validate()
        self.assertFalse(bill.l10n_ae_transmission_id)
        self.assertEqual(bill.company_id.partner_id.routing_endpoint, "1000000001")
        self.assertEqual(self.partner_a.routing_endpoint, "1000000002")

    def test_reviewed_domestic_business_buyer_without_onboarding_uses_destination_98(
        self,
    ):
        self.partner_a.routing_endpoint = False
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_fiscal_uuid = "aaaaaaaf-1111-4111-8111-111111111111"
        try:
            invoice.write(
                {
                    "l10n_ae_buyer_not_implemented": True,
                    "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                    "l10n_ae_buyer_onboarding_review_reference": (
                        "SYNTH-DOMESTIC-BUSINESS-NOT-IMPLEMENTED"
                    ),
                }
            )
        except (ValueError, KeyError) as error:
            self.fail(
                "The public invoice action cannot record reviewed buyer "
                f"availability: {error}"
            )
        xml = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        buyer = xml.find("cac:AccountingCustomerParty/cac:Party", NS)
        endpoint = buyer.find("cbc:EndpointID", NS)
        self.assertEqual(
            (endpoint.get("schemeID"), endpoint.text), ("0235", "9900000098")
        )
        self.assertEqual(
            xml.findtext("cbc:ProfileExecutionID", namespaces=NS), "00000000"
        )
        self.assertEqual(
            buyer.findtext("cac:PartyTaxScheme/cbc:CompanyID", namespaces=NS),
            "100000000200003",
        )
        self.assertFalse(self.partner_a.routing_endpoint)
        self.assertEqual(self.partner_a.vat, "100000000200003")
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_buyer_not_implemented_requires_dated_review_evidence(self):
        self.partner_a.routing_endpoint = False
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        for review_date, review_reference in (
            (False, "SYNTH-NOT-IMPLEMENTED"),
            ("2019-01-01", False),
            ("2019-01-01", "   "),
        ):
            with self.subTest(date=review_date, reference=review_reference):
                invoice.write(
                    {
                        "l10n_ae_buyer_not_implemented": True,
                        "l10n_ae_buyer_onboarding_review_date": review_date,
                        "l10n_ae_buyer_onboarding_review_reference": review_reference,
                    }
                )
                with self.assertRaisesRegex(UserError, "review date and source"):
                    invoice.action_l10n_ae_validate()
                self.assertFalse(invoice.l10n_ae_transmission_id)
                self.assertFalse(invoice.l10n_ae_fiscal_uuid)
                self.assertEqual(
                    (invoice.state, invoice.amount_tax, invoice.amount_total),
                    ("draft", 5, 105),
                )

    def test_buyer_status_review_cannot_replace_an_existing_participant_id(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.write(
            {
                "l10n_ae_buyer_not_implemented": True,
                "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                "l10n_ae_buyer_onboarding_review_reference": "SYNTH-CONFLICTING-STATUS",
            }
        )
        with self.assertRaisesRegex(UserError, "existing Participant ID"):
            invoice.action_l10n_ae_validate()
        self.assertEqual(self.partner_a.routing_endpoint, "1000000002")
        self.assertEqual(self.partner_a.vat, "100000000200003")
        self.assertFalse(invoice.l10n_ae_transmission_id)
        self.assertEqual((invoice.state, invoice.amount_total), ("draft", 105))

    def test_buyer_not_implemented_requires_a_domestic_buyer_country(self):
        self.partner_a.routing_endpoint = False
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.write(
            {
                "l10n_ae_buyer_not_implemented": True,
                "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                "l10n_ae_buyer_onboarding_review_reference": "SYNTH-DOMESTIC-REVIEW",
            }
        )
        for country in (self.env.ref("base.us"), self.env["res.country"]):
            with self.subTest(country=country.code):
                self.partner_a.country_id = country
                self.partner_a.routing_endpoint = False
                with self.assertRaisesRegex(UserError, "domestic UAE business buyer"):
                    invoice.action_l10n_ae_validate()
                self.assertFalse(self.partner_a.routing_endpoint)
                self.assertFalse(invoice.l10n_ae_transmission_id)
                self.assertEqual((invoice.state, invoice.amount_total), ("draft", 105))

    def test_buyer_not_implemented_cannot_mix_incompatible_routing_processes(self):
        self.partner_a.routing_endpoint = False
        for flag in ("l10n_ae_deemed_supply", "l10n_ae_exports", "is_self_billing"):
            with self.subTest(flag=flag):
                if flag == "is_self_billing":
                    invoice, _agreement = self._selfbilling_bill_with_agreement()
                    self.partner_a.routing_endpoint = False
                else:
                    invoice = self.init_invoice(
                        "out_invoice", amounts=[100], taxes=self.tax_sale_a
                    )
                invoice.write(
                    {
                        "l10n_ae_buyer_not_implemented": True,
                        "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                        "l10n_ae_buyer_onboarding_review_reference": (
                            "SYNTH-AMBIGUOUS-ROUTING"
                        ),
                        flag: True,
                    }
                )
                with self.assertRaisesRegex(
                    UserError, "not-yet-implemented buyer routing"
                ):
                    invoice.action_l10n_ae_validate()
                self.assertFalse(invoice.l10n_ae_transmission_id)
                self.assertEqual((invoice.state, invoice.amount_total), ("draft", 105))

    def test_prepared_buyer98_freezes_review_and_separate_delivery_obligation(self):
        self.partner_a.routing_endpoint = False
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_buyer_not_implemented": True,
                "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                "l10n_ae_buyer_onboarding_review_reference": "SYNTH-BUYER98-REVIEW",
                "l10n_ae_fiscal_uuid": "aaaaaaaf-2222-4222-8222-222222222222",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Buyer98 reviewed destination"})
                .id,
            }
        )
        invoice.action_l10n_ae_prepare()
        transmission = invoice.l10n_ae_transmission_id
        payload = transmission.payload
        xml = transmission.xml_content
        snapshot = json.loads(payload)
        self.assertEqual(
            snapshot.get("buyer_onboarding_review"),
            {
                "status": "not_implemented",
                "date": "2019-01-01",
                "source": "SYNTH-BUYER98-REVIEW",
            },
        )
        self.assertEqual(
            snapshot.get("alternative_delivery"),
            {"required": True, "reason": "buyer_not_implemented"},
        )
        self.assertEqual(
            snapshot["destination"],
            {
                "scheme": "0235",
                "endpoint": "9900000098",
                "reason": "buyer_not_implemented",
            },
        )
        self.assertIsNone(snapshot["buyer"]["endpoint"])
        self.assertEqual(snapshot["buyer"]["vat"], "100000000200003")
        self.assertEqual(transmission.exchange_mode, "reporting_only")
        self.assertEqual(transmission.confirmation_state, "pending")
        self.assertEqual(transmission.delivery_c3_state, "not_applicable")
        self.assertEqual(transmission.reporting_c3_state, "not_applicable")
        self.partner_a.routing_endpoint = "1000000004"
        self.assertEqual(invoice.preview_l10n_ae_pint(), xml)
        self.assertEqual(transmission.payload, payload)
        self.assertEqual((invoice.amount_tax, invoice.amount_total), (5, 105))

    def test_prepared_buyer98_preserves_native_invoice_html_after_buyer_changes(self):
        self.partner_a.write(
            {"name": "Synthetic Buyer98 HTML Original", "routing_endpoint": False}
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_buyer_not_implemented": True,
                "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                "l10n_ae_buyer_onboarding_review_reference": "SYNTH-BUYER98-HTML",
                "l10n_ae_fiscal_uuid": "aaaaaaaf-4444-4444-8444-444444444444",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Buyer98 conventional presentation"})
                .id,
            }
        )
        invoice.action_l10n_ae_prepare()
        transmission = invoice.l10n_ae_transmission_id
        observed_fields = [
            "conventional_html_content",
            "conventional_html_hash",
            "conventional_report_id",
            "conventional_filename",
            "payload",
            "xml_content",
        ]
        try:
            original = transmission.read(observed_fields)[0]
        except ValueError as error:
            self.fail(f"Preparation does not preserve the native presentation: {error}")
        html = original["conventional_html_content"]
        text = " ".join(etree.HTML(html.encode()).itertext())
        self.assertIn("Synthetic Buyer98 HTML Original", text)
        self.assertIn("100000000200003", text)
        self.assertIn(invoice.name, text)
        self.assertIn("100.00", text)
        self.assertIn("5.00", text)
        self.assertIn("105.00", text)
        self.assertEqual(
            original["conventional_html_hash"],
            hashlib.sha256(html.encode()).hexdigest(),
        )
        self.assertEqual(
            original["conventional_report_id"][0],
            self.env.ref("account.account_invoices").id,
        )
        self.assertTrue(original["conventional_filename"].endswith(".pdf"))
        self.partner_a.write(
            {"name": "Synthetic Changed Buyer98", "street": "Changed buyer street"}
        )
        invoice.action_l10n_ae_prepare()
        self.assertEqual(transmission.read(observed_fields)[0], original)
        self.assertEqual((invoice.amount_tax, invoice.amount_total), (5, 105))

    def test_pdf_issue_cannot_be_attached_to_another_invoice(self):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        original.write(
            {
                "l10n_ae_fiscal_uuid": "aaaaaaaf-5555-4555-8555-555555555555",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Preserved PDF invoice association"})
                .id,
            }
        )
        original.action_l10n_ae_prepare()
        transmission = original.l10n_ae_transmission_id
        unrelated = self.init_invoice(
            "out_invoice", amounts=[200], taxes=self.tax_sale_a, post=True
        )
        with self.assertRaisesRegex(UserError, "frozen UAE invoice reference"):
            unrelated.l10n_ae_transmission_id = transmission
        self.assertFalse(unrelated.l10n_ae_transmission_id)
        with self.assertRaisesRegex(UserError, "Prepare"):
            unrelated.action_l10n_ae_generate_conventional_pdf()
        self.assertFalse(transmission.conventional_pdf_content)
        self.assertEqual(transmission.move_id, original)
        self.assertEqual((original.amount_total, unrelated.amount_total), (105, 210))

    def test_xml_issue_cannot_be_attached_to_another_invoice(self):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        original.write(
            {
                "l10n_ae_fiscal_uuid": "aaaaaaaf-6666-4666-8666-666666666666",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Frozen XML invoice association"})
                .id,
            }
        )
        original.action_l10n_ae_prepare()
        frozen = original.preview_l10n_ae_pint()
        unrelated = self.init_invoice(
            "out_invoice", amounts=[200], taxes=self.tax_sale_a, post=True
        )
        with self.assertRaisesRegex(UserError, "frozen UAE invoice reference"):
            unrelated.l10n_ae_transmission_id = original.l10n_ae_transmission_id
        self.assertFalse(unrelated.l10n_ae_transmission_id)
        self.assertNotEqual(unrelated.preview_l10n_ae_pint(), frozen)
        self.assertEqual(original.preview_l10n_ae_pint(), frozen)
        self.assertEqual((original.amount_total, unrelated.amount_total), (105, 210))

    def test_ordinary_invoice_native_print_keeps_the_core_report_action(self):
        self.env.company.external_report_layout_id = self.env.ref(
            "web.external_layout_standard"
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        action = invoice.action_print_pdf()
        self.assertEqual(action["type"], "ir.actions.report")
        self.assertEqual(action["report_name"], "account.report_invoice_with_payments")
        self.assertFalse(invoice.l10n_ae_transmission_id)
        self.assertEqual(invoice.amount_total, 105)

    def test_prepared_buyer98_review_cannot_be_rewritten_by_an_administrator(self):
        self.partner_a.routing_endpoint = False
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_buyer_not_implemented": True,
                "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                "l10n_ae_buyer_onboarding_review_reference": "SYNTH-FROZEN-BUYER98",
                "l10n_ae_fiscal_uuid": "aaaaaaaf-3333-4333-8333-333333333333",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Protected buyer98 review"})
                .id,
            }
        )
        invoice.action_l10n_ae_prepare()
        payload = invoice.l10n_ae_transmission_id.payload
        xml = invoice.l10n_ae_transmission_id.xml_content
        presentation_fields = [
            "conventional_html_content",
            "conventional_html_hash",
            "conventional_report_id",
            "conventional_filename",
        ]
        transmission = invoice.l10n_ae_transmission_id
        presentation = transmission.read(presentation_fields)
        for field, value in (
            ("l10n_ae_buyer_not_implemented", False),
            ("l10n_ae_buyer_onboarding_review_date", "2019-01-02"),
            ("l10n_ae_buyer_onboarding_review_reference", "REPLACEMENT"),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(UserError, "frozen UAE"):
                    invoice.sudo().write({field: value})
                self.assertEqual(invoice.l10n_ae_transmission_id.payload, payload)
                self.assertEqual(invoice.preview_l10n_ae_pint(), xml)
        for field in presentation_fields + [
            "conventional_pdf_content",
            "conventional_pdf_hash",
            "conventional_pdf_generated_at",
        ]:
            with self.subTest(artifact_field=field):
                with self.assertRaisesRegex(UserError, "immutable"):
                    transmission.sudo().write({field: False})
                self.assertEqual(transmission.read(presentation_fields), presentation)

    def test_prepared_deemed_supply_declares_reporting_only_without_c3_obligations(
        self,
    ):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_deemed_supply": True,
                "l10n_ae_fiscal_uuid": "aaaaaaae-3333-4333-8333-333333333333",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Deemed reporting-only obligations"})
                .id,
            }
        )
        invoice.action_l10n_ae_prepare()
        transmission = invoice.l10n_ae_transmission_id
        self.assertEqual(
            json.loads(transmission.payload).get("exchange_mode"), "reporting_only"
        )
        self.assertEqual(transmission.exchange_mode, "reporting_only")
        self.assertEqual(transmission.submission_state, "queued")
        self.assertEqual(transmission.validation_c2_state, "unknown")
        self.assertEqual(transmission.reporting_c2_state, "unknown")
        self.assertEqual(transmission.delivery_c3_state, "not_applicable")
        self.assertEqual(transmission.reporting_c3_state, "not_applicable")
        self.assertFalse(transmission.attempt_ids)
        self.assertFalse(transmission.event_ids)
        self.assertEqual((invoice.amount_tax, invoice.amount_total), (5, 105))

    def test_combined_free_zone_export_preserves_eight_bits_and_beneficiary(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "synthetic-beneficiary",
                "l10n_ae_exports": True,
            }
        )
        xml = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        self.assertEqual(
            xml.findtext("cbc:ProfileExecutionID", namespaces=NS), "10000001"
        )
        self.assertEqual(
            xml.findtext(
                "cac:BuyerCustomerParty/cac:Party/cac:PartyIdentification/cbc:ID",
                namespaces=NS,
            ),
            "synthetic-beneficiary",
        )

    def test_prepared_free_zone_document_freezes_scenario_and_beneficiary_facts(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000003",
                "l10n_ae_fiscal_uuid": "aaaaaaad-1111-4111-8111-111111111111",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Free Zone snapshot"})
                .id,
            }
        )
        invoice.action_l10n_ae_prepare()
        transmission = invoice.l10n_ae_transmission_id
        snapshot = json.loads(transmission.payload)
        self.assertEqual(snapshot.get("transaction_flags"), "10000000")
        self.assertEqual(snapshot.get("beneficiary_id"), "1000000003")
        original = transmission.xml_content
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        for name, value in (
            ("l10n_ae_free_zone", False),
            ("l10n_ae_deemed_supply", True),
            ("l10n_ae_margin_scheme", True),
            ("l10n_ae_summary", True),
            ("l10n_ae_continuous_supply", True),
            ("l10n_ae_disclosed_agent", True),
            ("l10n_ae_ecommerce", True),
            ("l10n_ae_exports", True),
            ("l10n_ae_beneficiary_id", "1000000004"),
            ("l10n_ae_document_category", "commercial"),
        ):
            with self.subTest(field=name), self.assertRaisesRegex(UserError, "frozen"):
                invoice.write({name: value})
        self.assertEqual(invoice.preview_l10n_ae_pint(), original)
        self.assertEqual(invoice.amount_tax, 5)
        self.assertEqual(invoice.amount_total, 105)

    def test_prepared_xml_survives_later_customer_address_changes(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Synthetic sandbox"})
        invoice.l10n_ae_binding_id = binding
        expected = invoice.preview_l10n_ae_pint()
        invoice.action_l10n_ae_prepare()
        self.partner_a.street = "Changed after preparation"
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.l10n_ae_transmission_id.xml_content, expected)
        self.assertIn(
            "Synthetic buyer street", invoice.l10n_ae_transmission_id.xml_content
        )
        self.assertEqual(invoice.preview_l10n_ae_pint(), expected)

    def test_formal_validation_reads_frozen_xml_after_master_data_changes(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_fiscal_uuid = "22222222-2222-4222-8222-222222222222"
        invoice.l10n_ae_binding_id = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic frozen validation sandbox"}
        )
        invoice.action_l10n_ae_prepare()
        self.partner_a.write({"street": False, "city": False})
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
