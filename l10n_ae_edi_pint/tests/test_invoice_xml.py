# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
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
    def _received_test_xml(self):
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
                invoice.l10n_ae_document_category = category
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
