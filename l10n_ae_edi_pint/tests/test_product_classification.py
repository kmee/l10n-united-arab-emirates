# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json
from decimal import Decimal

from lxml import etree

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}


@tagged("post_install", "-at_install")
class TestUaeProductClassification(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        for partner, vat, endpoint, label in [
            (cls.env.company.partner_id, "100000000100003", "1000000001", "Supplier"),
            (cls.partner_a, "100000000200003", "1000000002", "Buyer"),
        ]:
            partner.write(
                {
                    "country_id": cls.env.ref("base.ae").id,
                    "vat": vat,
                    "street": "Synthetic classification street",
                    "city": "Dubai",
                    "state_id": cls.env.ref("base.state_ae_du").id,
                    "routing_scheme": "0235",
                    "routing_endpoint": endpoint,
                    "l10n_ae_registration_type": "TL",
                    "l10n_ae_registration_authority": "Synthetic Product Authority",
                    "additional_identifiers": {"AE_TL": "SYNTH-PRODUCT-" + label},
                }
            )

    def test_native_product_form_preserves_reviewed_rcm_nature_in_frozen_ae_invoice(
        self,
    ):
        # Declared synthetic AE supply: net AED100, offset 5% VAT effects,
        # VAT due0, payable100. No legal RCM eligibility is inferred.
        self.simple_accountman.group_ids |= self.env.ref(
            "product.group_product_manager"
        )
        products = self.env["product.template"].with_user(self.simple_accountman)
        form = Form(products)
        form.name = "Synthetic reviewed electronic goods"
        form.type = "consu"
        form.list_price = 100
        form.barcode = "6291041500213"
        form.l10n_ae_classification_code = "999999"
        form.l10n_ae_edi_rcm_nature_code = "DL8.48.8.2"
        template = form.save()
        product = template.product_variant_id
        self.assertEqual(product.l10n_ae_edi_rcm_nature_code, "DL8.48.8.2")
        # Native tax repartition: the two 5% effects offset each other.
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic declared AE category",
                "amount": 5,
                "ubl_cii_tax_category_code": "AE",
                "invoice_repartition_line_ids": [
                    Command.clear(),
                    Command.create({"repartition_type": "base", "factor_percent": 100}),
                    Command.create({"repartition_type": "tax", "factor_percent": 100}),
                    Command.create({"repartition_type": "tax", "factor_percent": -100}),
                ],
                "refund_repartition_line_ids": [
                    Command.clear(),
                    Command.create({"repartition_type": "base", "factor_percent": 100}),
                    Command.create({"repartition_type": "tax", "factor_percent": 100}),
                    Command.create({"repartition_type": "tax", "factor_percent": -100}),
                ],
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic reverse-charge sandbox",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        # Native product assignment selects the reviewed product's own taxes.
        product.taxes_id = tax
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=tax)
        invoice.invoice_line_ids.product_id = product
        self.assertEqual(invoice.invoice_line_ids.tax_ids, tax)
        invoice.l10n_ae_binding_id = binding
        invoice = invoice.with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        invoice.action_post()
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 0, 100),
        )
        ledger = invoice.line_ids.read(
            [
                "account_id",
                "currency_id",
                "debit",
                "credit",
                "balance",
                "tax_ids",
                "write_date",
            ]
        )
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        tree = etree.fromstring(issue.xml_content.encode())
        item = tree.find("cac:InvoiceLine/cac:Item", NS)
        self.assertEqual(
            item.xpath("cac:ClassifiedTaxCategory/cbc:ID/text()", namespaces=NS), ["AE"]
        )
        self.assertEqual(
            item.xpath(
                "cac:CommodityClassification/cbc:NatureCode/text()", namespaces=NS
            ),
            ["DL8.48.8.2"],
        )
        standard_id = item.find("cac:StandardItemIdentification/cbc:ID", NS)
        self.assertEqual(
            (standard_id.text, standard_id.attrib),
            ("6291041500213", {"schemeID": "0160"}),
        )
        self.assertEqual(
            json.loads(issue.payload)["lines"][0]["item_classification"][
                "rcm_nature_code"
            ],
            "DL8.48.8.2",
        )
        self.assertEqual(
            Decimal(
                tree.findtext(
                    "cac:InvoiceLine/cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount",
                    namespaces=NS,
                )
            ),
            Decimal(0),
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        original = issue.read(
            ["payload", "payload_hash", "xml_content", "xml_hash", "idempotency_key"]
        )
        product.l10n_ae_edi_rcm_nature_code = "DL8.48.3.1"
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), original[0]["xml_content"])
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
            original,
        )
        self.assertEqual(
            invoice.line_ids.read(
                [
                    "account_id",
                    "currency_id",
                    "debit",
                    "credit",
                    "balance",
                    "tax_ids",
                    "write_date",
                ]
            ),
            ledger,
        )
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.event_ids)

    def test_native_manual_ae_line_preserves_reviewed_nature_and_standard_id(self):
        # Synthetic declared AE/0% line: net100, VAT0 and payable100.
        # This exercises BTAE-09/IBR-174, not the nominal-rate interpretation.
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic manually reviewed AE",
                "amount": 0,
                "ubl_cii_tax_category_code": "AE",
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic manual AE sandbox",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=tax)
        invoice.l10n_ae_binding_id = binding
        invoice = invoice.with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        form = Form(invoice)
        with form.invoice_line_ids.edit(0) as line:
            line.product_id = False
            line.name = "Synthetic manually described electronic goods"
            line.tax_ids.clear()
            line.tax_ids.add(tax)
            line.l10n_ae_edi_rcm_nature_code = "DL8.48.8.2"
            line.l10n_ae_edi_rcm_standard_id = "6291041500213"
        invoice = form.save()
        self.assertFalse(invoice.invoice_line_ids.product_id)
        self.assertEqual(invoice.invoice_line_ids.tax_ids, tax)
        invoice.action_post()
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 0, 100),
        )
        ledger = invoice.line_ids.read(
            [
                "account_id",
                "currency_id",
                "debit",
                "credit",
                "balance",
                "tax_ids",
                "write_date",
            ]
        )
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        item = etree.fromstring(issue.xml_content.encode()).find(
            "cac:InvoiceLine/cac:Item", NS
        )
        self.assertEqual(
            item.xpath(
                "cac:CommodityClassification/cbc:NatureCode/text()", namespaces=NS
            ),
            ["DL8.48.8.2"],
        )
        self.assertFalse(
            item.xpath("cac:CommodityClassification/cbc:CommodityCode", namespaces=NS)
        )
        identifier = item.find("cac:StandardItemIdentification/cbc:ID", NS)
        self.assertEqual(
            (identifier.text, identifier.attrib),
            ("6291041500213", {"schemeID": "0160"}),
        )
        classification = json.loads(issue.payload)["lines"][0]["item_classification"]
        self.assertEqual(classification["rcm_nature_code"], "DL8.48.8.2")
        self.assertEqual(classification["rcm_standard_id"], "6291041500213")
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        original = issue.read(["payload", "payload_hash", "xml_content", "xml_hash"])
        with self.assertRaisesRegex(UserError, "frozen UAE invoice lines"):
            invoice.invoice_line_ids.write(
                {"l10n_ae_edi_rcm_nature_code": "DL8.48.3.1"}
            )
        with self.assertRaisesRegex(UserError, "frozen UAE invoice lines"):
            invoice.invoice_line_ids.write(
                {"l10n_ae_edi_rcm_standard_id": "6291041500214"}
            )
        invoice.action_l10n_ae_prepare()
        self.assertEqual(
            issue.read(["payload", "payload_hash", "xml_content", "xml_hash"]), original
        )
        self.assertEqual(
            invoice.line_ids.read(
                [
                    "account_id",
                    "currency_id",
                    "debit",
                    "credit",
                    "balance",
                    "tax_ids",
                    "write_date",
                ]
            ),
            ledger,
        )
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.event_ids)

    def test_service_product_preserves_reviewed_sac_in_frozen_xml_and_snapshot(self):
        # BTAE-13/17, IBR-185/189: S requires a separate SAC identifier.
        # These arbitrary codes/version are synthetic reviewed test data only.
        product = self.env["product.product"].create(
            {
                "name": "Synthetic classified service",
                "type": "service",
                "list_price": 100,
                "l10n_ae_service_accounting_code": "SYNTHETIC-SAC-01",
                "l10n_ae_service_accounting_version": "SYNTHETIC-SAC-2026",
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic service classification sandbox",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.invoice_line_ids.product_id = product
        invoice.l10n_ae_binding_id = binding
        invoice = invoice.with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        invoice.action_post()
        ledger = invoice.line_ids.read(
            ["account_id", "currency_id", "debit", "credit", "balance", "tax_ids"]
        )
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        tree = etree.fromstring(issue.xml_content.encode())
        item = tree.find("cac:InvoiceLine/cac:Item", NS)
        self.assertEqual(
            item.xpath(
                "cac:CommodityClassification/cbc:CommodityCode/text()", namespaces=NS
            ),
            ["S"],
        )
        sac = item.find("cac:AdditionalItemIdentification/cbc:ID", NS)
        self.assertIsNotNone(sac)
        self.assertEqual(sac.text, "SYNTHETIC-SAC-01")
        self.assertEqual(
            sac.attrib, {"schemeID": "SAC", "schemeVersionID": "SYNTHETIC-SAC-2026"}
        )
        self.assertFalse(
            item.xpath(
                "cac:CommodityClassification/cbc:ItemClassificationCode", namespaces=NS
            )
        )
        self.assertEqual(
            json.loads(issue.payload)["lines"][0]["item_classification"],
            {
                "type": "S",
                "hs_code": None,
                "sac_code": "SYNTHETIC-SAC-01",
                "sac_scheme_version": "SYNTHETIC-SAC-2026",
            },
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        original = issue.read(
            ["payload", "payload_hash", "xml_content", "xml_hash", "idempotency_key"]
        )
        product.write(
            {
                "l10n_ae_service_accounting_code": "SYNTHETIC-LATER-SAC",
                "l10n_ae_service_accounting_version": "SYNTHETIC-LATER-VERSION",
                "list_price": 999,
            }
        )
        self.assertEqual(invoice.preview_l10n_ae_pint(), issue.xml_content)
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.l10n_ae_transmission_id, issue)
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
            original,
        )
        self.assertEqual(
            invoice.line_ids.read(
                ["account_id", "currency_id", "debit", "credit", "balance", "tax_ids"]
            ),
            ledger,
        )
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.event_ids)

    def test_incomplete_classification_posts_in_review_and_repairs_without_ledger_edit(
        self,
    ):
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic classification review",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        self.env.company.write(
            {"l10n_ae_edi_auto_prepare": True, "l10n_ae_edi_binding_id": binding.id}
        )
        # Each missing fact has an independently stated expected diagnostic.
        cases = [
            ("consu", False, "l10n_ae_classification_code", "IBR-184-AE"),
            ("service", False, "l10n_ae_service_accounting_code", "IBR-185-AE"),
            (
                "service",
                False,
                "l10n_ae_service_accounting_version",
                "SAC scheme version",
            ),
            ("consu", True, "l10n_ae_classification_code", "IBR-186-AE"),
            ("service", True, "l10n_ae_service_accounting_code", "IBR-186-AE"),
        ]
        for product_type, mixed, missing, diagnostic in cases:
            with self.subTest(missing=missing, mixed=mixed):
                reviewed = {
                    "l10n_ae_classification_code": "999999",
                    "l10n_ae_service_accounting_code": "SYNTHETIC-REPAIRED-SAC",
                    "l10n_ae_service_accounting_version": "SYNTHETIC-SAC-2026",
                }
                product = self.env["product.product"].create(
                    {
                        "name": "Synthetic incomplete classification",
                        "type": product_type,
                        "list_price": 100,
                        "l10n_ae_is_good_and_service": mixed,
                        **reviewed,
                        missing: "   ",
                    }
                )
                invoice = self.init_invoice(
                    "out_invoice", amounts=[100], taxes=self.tax_sale_a
                )
                invoice.invoice_line_ids.product_id = product
                invoice = invoice.with_user(self.simple_accountman)
                invoice.action_post()
                self.assertEqual(invoice.state, "posted")
                self.assertEqual(invoice.l10n_ae_preparation_state, "review")
                self.assertIn(diagnostic, invoice.l10n_ae_preparation_error)
                self.assertTrue(invoice.l10n_ae_preparation_review_at)
                self.assertFalse(invoice.l10n_ae_transmission_id)
                self.assertFalse(invoice.l10n_ae_fiscal_uuid)
                self.assertEqual(
                    (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
                    (100, 5, 105),
                )
                ledger = invoice.line_ids.read(
                    ["debit", "credit", "balance", "tax_ids"]
                )
                with self.assertRaisesRegex(UserError, diagnostic):
                    invoice.action_l10n_ae_validate()
                with self.assertRaisesRegex(UserError, diagnostic):
                    invoice.action_l10n_ae_prepare()
                self.assertFalse(invoice.l10n_ae_transmission_id)
                self.assertFalse(invoice.l10n_ae_fiscal_uuid)
                product.write({missing: reviewed[missing]})
                invoice.action_l10n_ae_validate()
                invoice.action_l10n_ae_prepare()
                issue = invoice.l10n_ae_transmission_id
                self.assertTrue(issue)
                self.assertEqual(invoice.l10n_ae_preparation_state, "ready")
                self.assertFalse(invoice.l10n_ae_preparation_error)
                self.assertFalse(invoice.l10n_ae_preparation_review_at)
                report = invoice.validate_l10n_ae_pint()
                self.assertTrue(report["valid"], report)
                invoice.action_l10n_ae_prepare()
                self.assertEqual(invoice.l10n_ae_transmission_id, issue)
                self.assertEqual(
                    invoice.line_ids.read(["debit", "credit", "balance", "tax_ids"]),
                    ledger,
                )
                self.assertFalse(issue.attempt_ids)
                self.assertFalse(issue.event_ids)

    def test_goods_and_service_preserves_distinct_hs_and_sac(self):
        # BTAE-13 and IBR-186 require both identifiers, not a shared code.
        product = self.env["product.product"].create(
            {
                "name": "Synthetic goods and service",
                "type": "service",
                "list_price": 100,
                "l10n_ae_is_good_and_service": True,
                "l10n_ae_classification_code": "999999",
                "l10n_ae_service_accounting_code": "SYNTHETIC-SAC-B",
                "l10n_ae_service_accounting_version": "SYNTHETIC-SAC-2026",
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic both sandbox", "fiscal_uuid_policy": "controlled_local"}
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.invoice_line_ids.product_id = product
        invoice.l10n_ae_binding_id = binding
        invoice = invoice.with_user(self.simple_accountman)
        invoice.action_post()
        ledger = invoice.line_ids.read(["debit", "credit", "balance", "tax_ids"])
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        item = etree.fromstring(issue.xml_content.encode()).find(
            "cac:InvoiceLine/cac:Item", NS
        )
        self.assertEqual(
            item.xpath(
                "cac:CommodityClassification/cbc:CommodityCode/text()", namespaces=NS
            ),
            ["B"],
        )
        hs = item.find("cac:CommodityClassification/cbc:ItemClassificationCode", NS)
        self.assertEqual((hs.text, hs.attrib), ("999999", {"listID": "HS"}))
        sac = item.find("cac:AdditionalItemIdentification/cbc:ID", NS)
        self.assertEqual(sac.text, "SYNTHETIC-SAC-B")
        self.assertEqual(
            sac.attrib, {"schemeID": "SAC", "schemeVersionID": "SYNTHETIC-SAC-2026"}
        )
        self.assertEqual(
            json.loads(issue.payload)["lines"][0]["item_classification"],
            {
                "type": "B",
                "hs_code": "999999",
                "sac_code": "SYNTHETIC-SAC-B",
                "sac_scheme_version": "SYNTHETIC-SAC-2026",
            },
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        original = issue.read(["payload", "payload_hash", "xml_content", "xml_hash"])
        product.write(
            {
                "l10n_ae_is_good_and_service": False,
                "l10n_ae_classification_code": "111111",
                "l10n_ae_service_accounting_code": "SYNTHETIC-LATER-B",
            }
        )
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.l10n_ae_transmission_id, issue)
        self.assertEqual(
            issue.read(["payload", "payload_hash", "xml_content", "xml_hash"]), original
        )
        self.assertEqual(invoice.preview_l10n_ae_pint(), original[0]["xml_content"])
        self.assertEqual(
            invoice.line_ids.read(["debit", "credit", "balance", "tax_ids"]), ledger
        )
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.event_ids)

    def test_goods_product_preserves_hs_without_emitting_service_identifiers(self):
        # G requires IBT-158 with the UAE HS scheme (IBR-184/188).
        # 999999 is synthetic test input, not an approved goods classification.
        product = self.env["product.product"].create(
            {
                "name": "Synthetic classified goods",
                "type": "consu",
                "list_price": 100,
                "l10n_ae_classification_code": "999999",
                "l10n_ae_service_accounting_code": "UNUSED-SYNTHETIC-SAC",
                "l10n_ae_service_accounting_version": "UNUSED-SYNTHETIC-VERSION",
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic goods classification",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.invoice_line_ids.product_id = product
        invoice.l10n_ae_binding_id = binding
        invoice = invoice.with_user(self.simple_accountman)
        invoice.action_post()
        ledger = invoice.line_ids.read(["debit", "credit", "balance", "tax_ids"])
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        tree = etree.fromstring(issue.xml_content.encode())
        item = tree.find("cac:InvoiceLine/cac:Item", NS)
        self.assertEqual(
            item.xpath(
                "cac:CommodityClassification/cbc:CommodityCode/text()", namespaces=NS
            ),
            ["G"],
        )
        hs = item.find("cac:CommodityClassification/cbc:ItemClassificationCode", NS)
        self.assertEqual(hs.text, "999999")
        self.assertEqual(hs.attrib, {"listID": "HS"})
        self.assertIsNone(item.find("cac:AdditionalItemIdentification", NS))
        self.assertEqual(
            json.loads(issue.payload)["lines"][0]["item_classification"],
            {
                "type": "G",
                "hs_code": "999999",
                "sac_code": None,
                "sac_scheme_version": None,
            },
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        original = issue.read(["payload", "payload_hash", "xml_content", "xml_hash"])
        product.l10n_ae_classification_code = "111111"
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), original[0]["xml_content"])
        self.assertEqual(
            issue.read(["payload", "payload_hash", "xml_content", "xml_hash"]), original
        )
        self.assertEqual(
            invoice.line_ids.read(["debit", "credit", "balance", "tax_ids"]), ledger
        )
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        self.assertFalse(issue.attempt_ids)

    def test_native_product_form_configures_service_variant_for_emission(self):
        self.simple_accountman.group_ids |= self.env.ref(
            "product.group_product_manager"
        )
        products = self.env["product.template"].with_user(self.simple_accountman)
        with Form(products, view="product.product_template_only_form_view") as form:
            form.name = "Synthetic service configured through native Form"
            form.type = "service"
            form.list_price = 100
            form.l10n_ae_service_accounting_code = "SYNTHETIC-FORM-SAC"
            form.l10n_ae_service_accounting_version = "SYNTHETIC-SAC-2026"
        template = form.record
        product = template.product_variant_id
        self.assertEqual(
            (
                template.l10n_ae_service_accounting_code,
                product.l10n_ae_service_accounting_code,
                template.l10n_ae_service_accounting_version,
                product.l10n_ae_service_accounting_version,
            ),
            (
                "SYNTHETIC-FORM-SAC",
                "SYNTHETIC-FORM-SAC",
                "SYNTHETIC-SAC-2026",
                "SYNTHETIC-SAC-2026",
            ),
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic Form sandbox", "fiscal_uuid_policy": "controlled_local"}
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.invoice_line_ids.product_id = product
        invoice.l10n_ae_binding_id = binding
        invoice = invoice.with_user(self.simple_accountman)
        invoice.action_post()
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        self.assertEqual(
            json.loads(issue.payload)["lines"][0]["item_classification"],
            {
                "type": "S",
                "hs_code": None,
                "sac_code": "SYNTHETIC-FORM-SAC",
                "sac_scheme_version": "SYNTHETIC-SAC-2026",
            },
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        original = issue.read(["payload", "payload_hash", "xml_content", "xml_hash"])
        with Form(template, view="product.product_template_only_form_view") as form:
            form.l10n_ae_service_accounting_code = "SYNTHETIC-FORM-LATER"
        self.assertEqual(
            product.l10n_ae_service_accounting_code, "SYNTHETIC-FORM-LATER"
        )
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.l10n_ae_transmission_id, issue)
        self.assertEqual(
            issue.read(["payload", "payload_hash", "xml_content", "xml_hash"]), original
        )

    def test_native_product_form_configures_mixed_variant_with_both_identifiers(self):
        self.simple_accountman.group_ids |= self.env.ref(
            "product.group_product_manager"
        )
        products = self.env["product.template"].with_user(self.simple_accountman)
        with Form(products, view="product.product_template_only_form_view") as form:
            form.name = "Synthetic mixed item configured through native Form"
            form.type = "consu"
            form.list_price = 100
            form.l10n_ae_is_good_and_service = True
            form.l10n_ae_classification_code = "999999"
            form.l10n_ae_service_accounting_code = "SYNTHETIC-FORM-BOTH"
            form.l10n_ae_service_accounting_version = "SYNTHETIC-SAC-2026"
        template = form.record
        product = template.product_variant_id
        self.assertTrue(template.l10n_ae_is_good_and_service)
        self.assertTrue(product.l10n_ae_is_good_and_service)
        self.assertEqual(template.l10n_ae_classification_code, "999999")
        self.assertEqual(product.l10n_ae_classification_code, "999999")
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic mixed Form", "fiscal_uuid_policy": "controlled_local"}
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.invoice_line_ids.product_id = product
        invoice.l10n_ae_binding_id = binding
        invoice = invoice.with_user(self.simple_accountman)
        invoice.action_post()
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        self.assertEqual(
            json.loads(issue.payload)["lines"][0]["item_classification"],
            {
                "type": "B",
                "hs_code": "999999",
                "sac_code": "SYNTHETIC-FORM-BOTH",
                "sac_scheme_version": "SYNTHETIC-SAC-2026",
            },
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_variant_forms_keep_distinct_sac_and_refuse_ambiguous_template_updates(
        self,
    ):
        self.simple_accountman.group_ids |= self.env.ref(
            "product.group_product_manager"
        )
        attribute = self.env["product.attribute"].create(
            {
                "name": "Synthetic service option",
                "value_ids": [
                    Command.create({"name": "A"}),
                    Command.create({"name": "B"}),
                ],
            }
        )
        template = (
            self.env["product.template"]
            .create(
                {
                    "name": "Synthetic service variants",
                    "type": "service",
                    "list_price": 100,
                    "attribute_line_ids": [
                        Command.create(
                            {
                                "attribute_id": attribute.id,
                                "value_ids": [Command.set(attribute.value_ids.ids)],
                            }
                        )
                    ],
                }
            )
            .with_user(self.simple_accountman)
        )
        variants = template.product_variant_ids.sorted("id")
        self.assertEqual(len(variants), 2)
        for product, sac in zip(
            variants, ["SYNTHETIC-SAC-A", "SYNTHETIC-SAC-B"], strict=True
        ):
            with Form(product, view="product.product_normal_form_view") as form:
                form.l10n_ae_service_accounting_code = sac
                form.l10n_ae_service_accounting_version = "SYNTHETIC-SAC-2026"
        self.assertFalse(template.l10n_ae_service_accounting_code)
        self.assertFalse(template.l10n_ae_service_accounting_version)
        with Form(template, view="product.product_template_only_form_view") as form:
            form.name = "Reviewed synthetic service variants"
        with (
            self.cr.savepoint(),
            self.assertRaisesRegex(UserError, "individual variant"),
        ):
            template.write({"l10n_ae_service_accounting_code": "SYNTHETIC-AMBIGUOUS"})
        self.assertEqual(
            variants.mapped("l10n_ae_service_accounting_code"),
            ["SYNTHETIC-SAC-A", "SYNTHETIC-SAC-B"],
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic distinct variants",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100, 100], taxes=self.tax_sale_a
        )
        invoice.invoice_line_ids[0].product_id = variants[0]
        invoice.invoice_line_ids[1].product_id = variants[1]
        invoice.l10n_ae_binding_id = binding
        invoice = invoice.with_user(self.simple_accountman)
        invoice.action_post()
        ledger = invoice.line_ids.read(["debit", "credit", "balance", "tax_ids"])
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        tree = etree.fromstring(issue.xml_content.encode())
        self.assertEqual(
            tree.xpath(
                "cac:InvoiceLine/cac:Item/cac:AdditionalItemIdentification/cbc:ID/text()",
                namespaces=NS,
            ),
            ["SYNTHETIC-SAC-A", "SYNTHETIC-SAC-B"],
        )
        self.assertEqual(
            [
                line["item_classification"]["sac_code"]
                for line in json.loads(issue.payload)["lines"]
            ],
            ["SYNTHETIC-SAC-A", "SYNTHETIC-SAC-B"],
        )
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (200, 10, 210),
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        original = issue.read(["payload", "payload_hash", "xml_content", "xml_hash"])
        with Form(variants[1], view="product.product_normal_form_view") as form:
            form.l10n_ae_service_accounting_code = "SYNTHETIC-VARIANT-LATER"
        invoice.action_l10n_ae_prepare()
        self.assertEqual(
            issue.read(["payload", "payload_hash", "xml_content", "xml_hash"]), original
        )
        self.assertEqual(
            invoice.line_ids.read(["debit", "credit", "balance", "tax_ids"]), ledger
        )
