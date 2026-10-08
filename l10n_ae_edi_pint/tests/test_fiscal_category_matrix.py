# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json
from decimal import Decimal

from lxml import etree

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}


@tagged("post_install", "-at_install")
class TestUaeFiscalCategoryMatrix(AccountTestInvoicingCommon):
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
                    "street": "Synthetic fiscal matrix street",
                    "city": "Dubai",
                    "state_id": cls.env.ref("base.state_ae_du").id,
                    "routing_scheme": "0235",
                    "routing_endpoint": endpoint,
                    "l10n_ae_registration_type": "TL",
                    "l10n_ae_registration_authority": "Synthetic Matrix Authority",
                    "additional_identifiers": {"AE_TL": "SYNTH-MATRIX-" + label},
                }
            )

    def test_new_uae_chart_initializes_the_native_pint_categories(self):
        company = self.env["res.company"].create(
            {
                "name": "Synthetic fresh UAE PINT chart",
                "country_id": self.env.ref("base.ae").id,
                "currency_id": self.env.ref("base.AED").id,
            }
        )
        self.env["account.chart.template"].try_loading(
            "ae", company, install_demo=False
        )
        # Independently transcribed native identities/category table from the
        # public LGPL-3 Odoo PR262200; this is configuration, not eligibility
        # proof for VAT-only imports or the complete reverse-charge calculation.
        expected = {
            "uae_sale_tax_5_dubai": "S",
            "uae_sale_tax_5_abu_dhabi": "S",
            "uae_sale_tax_5_sharjah": "S",
            "uae_sale_tax_5_ajman": "S",
            "uae_sale_tax_5_umm_al_quwain": "S",
            "uae_sale_tax_5_ras_al_khaima": "S",
            "uae_sale_tax_5_fujairah": "S",
            "uae_sale_tax_0": "Z",
            "uae_export_tax": "Z",
            "uae_sale_tax_exempted": "E",
            "uae_sale_tax_tourist_refund": "S",
            "uae_purchase_tax_5": "S",
            "uae_import_tax": "S",
            "uae_purchase_tax_0": "Z",
            "uae_purchase_tax_exempted": "E",
            "uae_purchase_postponed_tax": "AE",
            "uae_purchase_tax_reverse_charge": "AE",
            "uae_out_of_scope": "O",
        }
        actual = {}
        for xmlid in expected:
            tax = self.env.ref(f"account.{company.id}_{xmlid}")
            self.assertEqual(tax.company_id, company)
            actual[xmlid] = tax.ubl_cii_tax_category_code
            self.assertFalse(tax.ubl_cii_tax_exemption_reason_code)
        self.assertEqual(actual, expected)
        imported_vat = self.env.ref(f"account.{company.id}_uae_import_tax")
        self.assertEqual(
            (imported_vat.amount, imported_vat.amount_type), (100, "division")
        )
        standard = self.env.ref(f"account.{company.id}_uae_sale_tax_5_dubai")
        self.assertEqual((standard.amount, standard.amount_type), (5, "percent"))

    def test_native_zero_rate_sales_tax_prepares_z_without_an_exemption_reason(self):
        company = self.env.company
        tax = self.env.ref(f"account.{company.id}_uae_sale_tax_0")
        tax_fields = [
            "amount",
            "amount_type",
            "type_tax_use",
            "invoice_repartition_line_ids",
            "refund_repartition_line_ids",
            "tax_group_id",
            "price_include_override",
        ]
        tax_definition = tax.read(tax_fields)
        self.assertEqual(tax.amount, 0)
        self.assertFalse(tax.ubl_cii_tax_exemption_reason_code)
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Native zero tax"})
        invoice = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "journal_id": self.company_data["default_journal_sale"].id,
                    "currency_id": self.env.ref("base.AED").id,
                    "invoice_date": "2019-01-01",
                    "l10n_ae_binding_id": binding.id,
                    "l10n_ae_fiscal_uuid": "cececece-4444-4444-8444-444444444444",
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Native zero-rated supply",
                                "price_unit": 100,
                                "account_id": self.company_data[
                                    "default_account_revenue"
                                ].id,
                                "tax_ids": [Command.set(tax.ids)],
                            }
                        )
                    ],
                }
            )
        )
        self.assertFalse(invoice.env.su)
        invoice.action_post()
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 0, 100),
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
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        self.assertTrue(issue)
        root = etree.fromstring(issue.xml_content.encode())
        self.assertEqual(
            root.findtext(
                "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:ID",
                namespaces=NS,
            ),
            "Z",
        )
        self.assertEqual(
            root.findtext(
                "cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory/cbc:ID", namespaces=NS
            ),
            "Z",
        )
        self.assertEqual(
            Decimal(
                root.findtext(
                    "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:Percent",
                    namespaces=NS,
                )
            ),
            Decimal(0),
        )
        self.assertEqual(
            Decimal(
                root.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=NS)
            ),
            Decimal(100),
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertEqual(tax.read(tax_fields), tax_definition)
        original = issue.read(
            [
                "xml_content",
                "xml_hash",
                "payload",
                "payload_hash",
                "idempotency_key",
                "create_date",
                "write_date",
            ]
        )
        tax.ubl_cii_tax_category_code = "O"
        invoice.action_l10n_ae_prepare()
        self.assertEqual(issue.read(list(original[0])), original)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertFalse(issue.attempt_ids or issue.event_ids)

    def test_native_chart_reload_fills_missing_zero_category_and_keeps_reviewed_values(
        self,
    ):
        company = self.env.company
        zero = self.env.ref(f"account.{company.id}_uae_sale_tax_0")
        custom = zero.copy(
            {"name": "Reviewed custom exempt tax", "ubl_cii_tax_category_code": "E"}
        )
        financial_fields = [
            "id",
            "name",
            "active",
            "amount",
            "amount_type",
            "type_tax_use",
            "invoice_repartition_line_ids",
            "refund_repartition_line_ids",
            "tax_group_id",
            "price_include_override",
            "fiscal_position_ids",
        ]
        native = (
            self.env["account.tax"]
            .with_context(active_test=False)
            .search(
                [
                    ("company_id", "=", company.id),
                ]
            )
        )
        before = native.read(financial_fields)
        zero.ubl_cii_tax_category_code = False
        self.env["account.chart.template"].try_loading(
            "ae", company, install_demo=False, force_create=False
        )
        self.assertEqual(zero.ubl_cii_tax_category_code, "Z")
        self.assertEqual(custom.ubl_cii_tax_category_code, "E")
        self.assertEqual(native.read(financial_fields), before)
        self.assertEqual(self.env.ref(f"account.{company.id}_uae_sale_tax_0"), zero)
        # A reviewed selection is retained on subsequent chart reloads.
        zero.ubl_cii_tax_category_code = "O"
        self.env["account.chart.template"].try_loading(
            "ae", company, install_demo=False, force_create=False
        )
        self.assertEqual(zero.ubl_cii_tax_category_code, "O")
        self.assertEqual(custom.ubl_cii_tax_category_code, "E")
        self.assertEqual(native.read(financial_fields), before)

    def test_tax_inclusive_discount_preserves_net_allowance_and_original_ledger(self):
        # Independent AED example: gross unit price105 includes5% VAT.
        # Before discount the net basis is100. A10% discount therefore means
        # allowance10, net90, VAT4.50 and payable94.50 (IBR-131/147-AE).
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic tax-inclusive S",
                "amount": 5,
                "price_include_override": "tax_included",
                "ubl_cii_tax_category_code": "S",
            }
        )
        self.assertTrue(tax.price_include)
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic tax-inclusive discount"}
        )
        invoice = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "journal_id": self.company_data["default_journal_sale"].id,
                    "currency_id": self.env.ref("base.AED").id,
                    "invoice_date": "2019-01-01",
                    "l10n_ae_binding_id": binding.id,
                    "l10n_ae_fiscal_uuid": "cececece-1111-4111-8111-111111111111",
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Synthetic tax-inclusive service",
                                "quantity": 1,
                                "price_unit": 105,
                                "discount": 10,
                                "account_id": self.company_data[
                                    "default_account_revenue"
                                ].id,
                                "tax_ids": [Command.set(tax.ids)],
                            }
                        )
                    ],
                }
            )
        )
        self.assertFalse(invoice.env.su)
        invoice.action_post()
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (90, 4.50, 94.50),
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
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        original_fields = [
            "revision",
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
            "create_date",
            "write_date",
        ]
        original = issue.read(original_fields)
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["xsd_valid"], report)
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        self.assertEqual(issue.read(original_fields), original)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        xml = etree.fromstring(issue.xml_content.encode())
        line = xml.find("cac:InvoiceLine", NS)
        self.assertEqual(len(line.findall("cac:AllowanceCharge", NS)), 1)
        self.assertEqual(
            line.findtext("cac:AllowanceCharge/cbc:ChargeIndicator", namespaces=NS),
            "false",
        )
        for path, expected in [
            ("cac:Price/cbc:PriceAmount", "100"),
            ("cac:AllowanceCharge/cbc:BaseAmount", "100"),
            ("cac:AllowanceCharge/cbc:Amount", "10"),
            ("cac:AllowanceCharge/cbc:MultiplierFactorNumeric", "10"),
            ("cbc:LineExtensionAmount", "90"),
            ("cac:ItemPriceExtension/cbc:Amount", "94.50"),
            ("cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount", "4.50"),
        ]:
            node = line.find(path, NS)
            self.assertIsNotNone(node, path)
            self.assertLess(
                abs(Decimal(node.text) - Decimal(expected)), Decimal("0.00000001")
            )
            if path != "cac:AllowanceCharge/cbc:MultiplierFactorNumeric":
                self.assertEqual(node.get("currencyID"), "AED")
        for path, expected in [
            ("cac:TaxTotal/cbc:TaxAmount", "4.50"),
            ("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", "90"),
            ("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "94.50"),
            ("cac:LegalMonetaryTotal/cbc:PayableAmount", "94.50"),
        ]:
            self.assertEqual(
                Decimal(xml.findtext(path, namespaces=NS)), Decimal(expected)
            )
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), issue.xml_content)
        self.assertEqual(issue.read(original_fields), original)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertFalse(
            issue.attempt_ids
            or issue.event_ids
            or issue.remote_id
            or issue.validation_receipt
        )

    def test_full_line_discount_preserves_allowance_and_zero_payable(self):
        # Independent AED example: net unit price100 with5% VAT.
        # A100% line allowance is100, leaving net/VAT/gross/payable zero.
        # The undiscounted price/basis remain100 (IBR-131/147-AE).
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic full-discount S",
                "amount": 5,
                "price_include_override": "tax_excluded",
                "ubl_cii_tax_category_code": "S",
            }
        )
        self.assertFalse(tax.price_include)
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic full line discount"}
        )
        invoice = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "journal_id": self.company_data["default_journal_sale"].id,
                    "currency_id": self.env.ref("base.AED").id,
                    "invoice_date": "2019-01-01",
                    "l10n_ae_binding_id": binding.id,
                    "l10n_ae_fiscal_uuid": "bdbdbdbd-1111-4111-8111-111111111111",
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Synthetic full line allowance service",
                                "quantity": 1,
                                "price_unit": 100,
                                "discount": 100,
                                "account_id": self.company_data[
                                    "default_account_revenue"
                                ].id,
                                "tax_ids": [Command.set(tax.ids)],
                            }
                        )
                    ],
                }
            )
        )
        self.assertFalse(invoice.env.su)
        invoice.action_post()
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (0, 0, 0),
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
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        original_fields = [
            "revision",
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
            "create_date",
            "write_date",
        ]
        original = issue.read(original_fields)
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["xsd_valid"], report)
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        self.assertEqual(issue.read(original_fields), original)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        xml = etree.fromstring(issue.xml_content.encode())
        line = xml.find("cac:InvoiceLine", NS)
        self.assertEqual(len(line.findall("cac:AllowanceCharge", NS)), 1)
        self.assertEqual(
            line.findtext("cac:AllowanceCharge/cbc:ChargeIndicator", namespaces=NS),
            "false",
        )
        for path, expected in [
            ("cac:Price/cbc:PriceAmount", "100"),
            ("cac:AllowanceCharge/cbc:BaseAmount", "100"),
            ("cac:AllowanceCharge/cbc:Amount", "100"),
            ("cac:AllowanceCharge/cbc:MultiplierFactorNumeric", "100"),
            ("cbc:LineExtensionAmount", "0"),
            ("cac:ItemPriceExtension/cbc:Amount", "0"),
            ("cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount", "0"),
        ]:
            node = line.find(path, NS)
            self.assertIsNotNone(node, path)
            self.assertLess(
                abs(Decimal(node.text) - Decimal(expected)), Decimal("0.00000001")
            )
            if path != "cac:AllowanceCharge/cbc:MultiplierFactorNumeric":
                self.assertEqual(node.get("currencyID"), "AED")
        for path, expected in [
            ("cac:TaxTotal/cbc:TaxAmount", "0"),
            ("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", "0"),
            ("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "0"),
            ("cac:LegalMonetaryTotal/cbc:PayableAmount", "0"),
        ]:
            self.assertEqual(
                Decimal(xml.findtext(path, namespaces=NS)), Decimal(expected)
            )
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), issue.xml_content)
        self.assertEqual(issue.read(original_fields), original)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertFalse(
            issue.attempt_ids
            or issue.event_ids
            or issue.remote_id
            or issue.validation_receipt
        )

    def test_tax_inclusive_full_discount_preserves_original_net_basis(self):
        # Independent AED example: gross unit price105 includes5% VAT, so the
        # undiscounted net price and allowance basis are100. A100% discount
        # leaves net, VAT, gross and payable at zero (IBR-131/147-AE).
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic tax-inclusive full discount S",
                "amount": 5,
                "price_include_override": "tax_included",
                "ubl_cii_tax_category_code": "S",
            }
        )
        self.assertTrue(tax.price_include)
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic tax-inclusive full discount"}
        )
        invoice = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "journal_id": self.company_data["default_journal_sale"].id,
                    "currency_id": self.env.ref("base.AED").id,
                    "invoice_date": "2019-01-01",
                    "l10n_ae_binding_id": binding.id,
                    "l10n_ae_fiscal_uuid": "dededede-1111-4111-8111-111111111111",
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Synthetic tax-inclusive full discount",
                                "quantity": 1,
                                "price_unit": 105,
                                "discount": 100,
                                "account_id": self.company_data[
                                    "default_account_revenue"
                                ].id,
                                "tax_ids": [Command.set(tax.ids)],
                            }
                        )
                    ],
                }
            )
        )
        self.assertFalse(invoice.env.su)
        invoice.action_post()
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (0, 0, 0),
        )
        ledger_fields = [
            "account_id",
            "date",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "currency_id",
            "amount_residual",
            "amount_residual_currency",
            "reconciled",
            "create_date",
            "write_date",
        ]
        ledger = invoice.line_ids.read(ledger_fields)
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        evidence_fields = [
            "revision",
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
            "create_date",
            "write_date",
        ]
        evidence = issue.read(evidence_fields)
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["xsd_valid"], report)
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        self.assertEqual(issue.read(evidence_fields), evidence)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)

        xml = etree.fromstring(issue.xml_content.encode())
        self.assertEqual(xml.findtext("cbc:DocumentCurrencyCode", namespaces=NS), "AED")
        line = xml.find("cac:InvoiceLine", NS)
        self.assertEqual(len(line.findall("cac:AllowanceCharge", NS)), 1)
        allowance = line.find("cac:AllowanceCharge", NS)
        self.assertIsNotNone(allowance)
        self.assertEqual(
            allowance.findtext("cbc:ChargeIndicator", namespaces=NS), "false"
        )
        self.assertEqual(
            allowance.findtext("cbc:AllowanceChargeReasonCode", namespaces=NS), "95"
        )
        for path, expected in [
            ("cac:Price/cbc:PriceAmount", "100"),
            ("cac:AllowanceCharge/cbc:BaseAmount", "100"),
            ("cac:AllowanceCharge/cbc:Amount", "100"),
            ("cac:AllowanceCharge/cbc:MultiplierFactorNumeric", "100"),
            ("cbc:LineExtensionAmount", "0"),
            ("cac:ItemPriceExtension/cbc:Amount", "0"),
            ("cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount", "0"),
        ]:
            node = line.find(path, NS)
            self.assertIsNotNone(node, path)
            self.assertLess(
                abs(Decimal(node.text) - Decimal(expected)), Decimal("0.00000001")
            )
            if path != "cac:AllowanceCharge/cbc:MultiplierFactorNumeric":
                self.assertEqual(node.get("currencyID"), "AED")
        for path, expected in [
            ("cac:TaxTotal/cbc:TaxAmount", "0"),
            ("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", "0"),
            ("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "0"),
            ("cac:LegalMonetaryTotal/cbc:PayableAmount", "0"),
        ]:
            node = xml.find(path, NS)
            self.assertIsNotNone(node, path)
            self.assertEqual(Decimal(node.text), Decimal(expected))
            self.assertEqual(node.get("currencyID"), "AED")

        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), issue.xml_content)
        self.assertEqual(issue.read(evidence_fields), evidence)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertFalse(
            issue.attempt_ids
            or issue.event_ids
            or issue.remote_id
            or issue.validation_receipt
        )

    def test_line_charge_preserves_adk_reason_and_native_totals(self):
        # Independent AED example: net unit price100 plus a10% line charge
        # yields base100, charge10, net110, VAT5.50 and gross/payable115.50.
        # Odoo stores a surcharge as discount=-10; the UBL core emits ADK.
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic line charge S",
                "amount": 5,
                "price_include_override": "tax_excluded",
                "ubl_cii_tax_category_code": "S",
            }
        )
        self.assertFalse(tax.price_include)
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic line charge"}
        )
        invoice = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "journal_id": self.company_data["default_journal_sale"].id,
                    "currency_id": self.env.ref("base.AED").id,
                    "invoice_date": "2019-01-01",
                    "l10n_ae_binding_id": binding.id,
                    "l10n_ae_fiscal_uuid": "efefefef-1111-4111-8111-111111111111",
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Synthetic line charge service",
                                "quantity": 1,
                                "price_unit": 100,
                                "discount": -10,
                                "account_id": self.company_data[
                                    "default_account_revenue"
                                ].id,
                                "tax_ids": [Command.set(tax.ids)],
                            }
                        )
                    ],
                }
            )
        )
        self.assertFalse(invoice.env.su)
        invoice.action_post()
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (110, 5.50, 115.50),
        )
        ledger_fields = [
            "account_id",
            "date",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "currency_id",
            "amount_residual",
            "amount_residual_currency",
            "reconciled",
            "create_date",
            "write_date",
        ]
        ledger = invoice.line_ids.read(ledger_fields)
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        evidence_fields = [
            "revision",
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
            "create_date",
            "write_date",
        ]
        evidence = issue.read(evidence_fields)
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["xsd_valid"], report)
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        self.assertEqual(issue.read(evidence_fields), evidence)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)

        xml = etree.fromstring(issue.xml_content.encode())
        self.assertEqual(xml.findtext("cbc:DocumentCurrencyCode", namespaces=NS), "AED")
        line = xml.find("cac:InvoiceLine", NS)
        self.assertEqual(len(line.findall("cac:AllowanceCharge", NS)), 1)
        charge = line.find("cac:AllowanceCharge", NS)
        self.assertIsNotNone(charge)
        self.assertEqual(charge.findtext("cbc:ChargeIndicator", namespaces=NS), "true")
        self.assertEqual(
            charge.findtext("cbc:AllowanceChargeReasonCode", namespaces=NS), "ADK"
        )
        for path, expected in [
            ("cac:Price/cbc:PriceAmount", "100"),
            ("cac:AllowanceCharge/cbc:BaseAmount", "100"),
            ("cac:AllowanceCharge/cbc:Amount", "10"),
            ("cac:AllowanceCharge/cbc:MultiplierFactorNumeric", "10"),
            ("cbc:LineExtensionAmount", "110"),
            ("cac:ItemPriceExtension/cbc:Amount", "115.50"),
            ("cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount", "5.50"),
        ]:
            node = line.find(path, NS)
            self.assertIsNotNone(node, path)
            self.assertLess(
                abs(Decimal(node.text) - Decimal(expected)), Decimal("0.00000001")
            )
            if path != "cac:AllowanceCharge/cbc:MultiplierFactorNumeric":
                self.assertEqual(node.get("currencyID"), "AED")
        for path, expected in [
            ("cac:TaxTotal/cbc:TaxAmount", "5.50"),
            ("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", "110"),
            ("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "115.50"),
            ("cac:LegalMonetaryTotal/cbc:PayableAmount", "115.50"),
        ]:
            node = xml.find(path, NS)
            self.assertIsNotNone(node, path)
            self.assertLess(
                abs(Decimal(node.text) - Decimal(expected)), Decimal("0.00000001")
            )
            self.assertEqual(node.get("currencyID"), "AED")

        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), issue.xml_content)
        self.assertEqual(issue.read(evidence_fields), evidence)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertFalse(
            issue.attempt_ids
            or issue.event_ids
            or issue.remote_id
            or issue.validation_receipt
        )

    def test_discounted_partial_credit_keeps_allowance_reference_and_native_balance(
        self,
    ):
        # Independent AED example: one100 unit less10%=90 net+4.50 VAT.
        # Crediting half the quantity retains the10% discount: basis50,
        # allowance5, net45/VAT2.25/gross47.25; original residual47.25.
        # The native wizard reconciles the credit fully against the original,
        # so its issue has prepaid47.25/payable0, without a bank payment.
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic discounted credit S",
                "amount": 5,
                "ubl_cii_tax_category_code": "S",
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic discounted partial credit"}
        )
        original = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "journal_id": self.company_data["default_journal_sale"].id,
                    "currency_id": self.env.ref("base.AED").id,
                    "invoice_date": "2019-01-01",
                    "l10n_ae_binding_id": binding.id,
                    "l10n_ae_fiscal_uuid": "abababab-1111-4111-8111-111111111111",
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Synthetic discounted quantity",
                                "quantity": 1,
                                "price_unit": 100,
                                "discount": 10,
                                "account_id": self.company_data[
                                    "default_account_revenue"
                                ].id,
                                "tax_ids": [Command.set(tax.ids)],
                            }
                        )
                    ],
                }
            )
        )
        self.assertFalse(original.env.su)
        original.action_post()
        self.assertEqual(
            (
                original.amount_untaxed,
                original.amount_tax,
                original.amount_total,
                original.amount_residual,
            ),
            (90, 4.50, 94.50, 94.50),
        )
        original.action_l10n_ae_prepare()
        source_issue = original.l10n_ae_transmission_id
        evidence_fields = [
            "revision",
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
            "create_date",
            "write_date",
        ]
        source_evidence = source_issue.read(evidence_fields)
        source_report = original.validate_l10n_ae_pint()
        self.assertTrue(source_report["valid"], source_report)
        self.assertEqual(source_issue.read(evidence_fields), source_evidence)
        financial_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "currency_id",
            "create_date",
        ]
        source_financial = original.line_ids.read(financial_fields)
        reversal = (
            self.env["account.move.reversal"]
            .with_user(self.simple_accountman)
            .with_context(
                allowed_company_ids=[self.env.company.id],
                active_model="account.move",
                active_ids=original.ids,
            )
            .create(
                {
                    "move_ids": [Command.set(original.ids)],
                    "journal_id": original.journal_id.id,
                    "date": "2019-01-02",
                    "reason": "Synthetic half-quantity adjustment",
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                }
            )
        )
        reversal.refund_moves()
        credit = reversal.new_move_ids
        self.assertFalse(credit.env.su)
        self.assertEqual((credit.state, credit.move_type), ("draft", "out_refund"))
        self.assertEqual(credit.invoice_line_ids.discount, 10)
        credit.invoice_line_ids.write({"quantity": 0.5})
        credit.write(
            {
                "l10n_ae_binding_id": binding.id,
                "l10n_ae_fiscal_uuid": "bcbcbcbc-1111-4111-8111-111111111111",
            }
        )
        credit.action_post()
        self.assertEqual(
            (
                credit.amount_untaxed,
                credit.amount_tax,
                credit.amount_total,
                credit.amount_residual,
                original.amount_residual,
            ),
            (45, 2.25, 47.25, 0, 47.25),
        )
        self.assertEqual(credit.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertEqual(credit.reversed_entry_id, original)
        self.assertEqual(original.line_ids.read(financial_fields), source_financial)
        # Native credit posting legitimately reconciles receivables. Freeze full
        # timestamps after that action, then require all EDI actions to preserve them.
        ledger_fields = [*financial_fields, "write_date"]
        source_ledger = original.line_ids.read(ledger_fields)
        credit_ledger = credit.line_ids.read(ledger_fields)
        credit.action_l10n_ae_prepare()
        issue = credit.l10n_ae_transmission_id
        credit_evidence = issue.read(evidence_fields)
        report = credit.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["xsd_valid"], report)
        xml = etree.fromstring(issue.xml_content.encode())
        self.assertEqual(xml.findtext("cbc:CreditNoteTypeCode", namespaces=NS), "381")
        reference = xml.find("cac:BillingReference/cac:InvoiceDocumentReference", NS)
        self.assertEqual(reference.findtext("cbc:ID", namespaces=NS), original.name)
        self.assertEqual(
            reference.findtext("cbc:IssueDate", namespaces=NS), "2019-01-01"
        )
        line = xml.find("cac:CreditNoteLine", NS)
        self.assertEqual(len(line.findall("cac:AllowanceCharge", NS)), 1)
        self.assertEqual(
            line.findtext("cac:AllowanceCharge/cbc:ChargeIndicator", namespaces=NS),
            "false",
        )
        for path, expected in [
            ("cbc:CreditedQuantity", "0.5"),
            ("cac:Price/cbc:PriceAmount", "100"),
            ("cac:AllowanceCharge/cbc:BaseAmount", "50"),
            ("cac:AllowanceCharge/cbc:Amount", "5"),
            ("cac:AllowanceCharge/cbc:MultiplierFactorNumeric", "10"),
            ("cbc:LineExtensionAmount", "45"),
            ("cac:ItemPriceExtension/cbc:Amount", "47.25"),
            ("cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount", "2.25"),
        ]:
            self.assertEqual(
                Decimal(line.findtext(path, namespaces=NS)), Decimal(expected)
            )
        for path, expected in [
            ("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", "45"),
            ("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "47.25"),
            ("cac:LegalMonetaryTotal/cbc:PrepaidAmount", "47.25"),
            ("cac:LegalMonetaryTotal/cbc:PayableAmount", "0"),
        ]:
            self.assertEqual(
                Decimal(xml.findtext(path, namespaces=NS)), Decimal(expected)
            )
        credit.action_l10n_ae_prepare()
        self.assertEqual(credit.preview_l10n_ae_pint(), issue.xml_content)
        self.assertEqual(issue.read(evidence_fields), credit_evidence)
        self.assertEqual(source_issue.read(evidence_fields), source_evidence)
        self.assertEqual(original.line_ids.read(ledger_fields), source_ledger)
        self.assertEqual(credit.line_ids.read(ledger_fields), credit_ledger)
        for transmission in (source_issue, issue):
            self.assertFalse(
                transmission.attempt_ids
                or transmission.event_ids
                or transmission.remote_id
                or transmission.validation_receipt
            )

    def test_fractional_usd_quantity_keeps_high_precision_price_and_raw_aed_lines(self):
        # Worked vector, independent of the exporter: 400.1234 USD * 0.125
        # = 50.015425 net, 2.50077125 VAT, 52.51619625 gross before rounding.
        # At 3.672500 AED/USD, raw VAT/gross are 9.184082415625 and
        # 192.865730728125. Rounded document headers are 2.50 / 52.52 USD;
        # their converted headers are 9.18 / 192.88 AED.
        # Configure the native quantity precision before entry: at the default
        # two places Odoo stores 0.125 as 0.13, changing the business quantity.
        self.env["decimal.precision"].search([("name", "=", "Product Unit")]).digits = 3
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
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic fractional S",
                "amount": 5,
                "ubl_cii_tax_category_code": "S",
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic high precision USD"}
        )
        invoice = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "journal_id": self.company_data["default_journal_sale"].id,
                    "currency_id": usd.id,
                    "invoice_date": "2019-01-01",
                    "l10n_ae_binding_id": binding.id,
                    "l10n_ae_fiscal_uuid": "fafafafa-1111-4111-8111-111111111111",
                    "l10n_ae_tax_exchange_rate": "3.672500",
                    "l10n_ae_tax_exchange_date": "2019-01-01",
                    "l10n_ae_tax_exchange_source": "SYNTH-USD-FRACTIONAL-PRECISION",
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Synthetic fractional high precision service",
                                "quantity": 0.125,
                                "price_unit": 400.1234,
                                "account_id": self.company_data[
                                    "default_account_revenue"
                                ].id,
                                "tax_ids": [Command.set(tax.ids)],
                            }
                        )
                    ],
                }
            )
        )
        self.assertFalse(invoice.env.su)
        self.assertEqual(invoice.invoice_line_ids.quantity, 0.125)
        self.assertEqual(invoice.invoice_line_ids.price_unit, 400.1234)
        invoice.action_post()
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (50.02, 2.50, 52.52),
        )
        self.assertEqual(
            (invoice.amount_tax_signed, invoice.amount_total_signed), (9.18, 192.88)
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
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["xsd_valid"], report)
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        xml = etree.fromstring(issue.xml_content.encode())
        line = xml.find("cac:InvoiceLine", NS)
        for path, expected in [
            ("cbc:InvoicedQuantity", "0.125"),
            ("cac:Price/cbc:PriceAmount", "400.1234"),
            ("cbc:LineExtensionAmount", "50.02"),
        ]:
            self.assertEqual(
                Decimal(line.findtext(path, namespaces=NS)), Decimal(expected)
            )
        for path, expected in [
            ("cac:ItemPriceExtension/cbc:Amount", "192.865730728125"),
            ("cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount", "9.184082415625"),
        ]:
            amount = line.find(path, NS)
            self.assertEqual(amount.get("currencyID"), "AED")
            # The native price/quantity are floats: tolerate serialization noise,
            # never a currency-cent rounding or substitution of the header base.
            self.assertLess(
                abs(Decimal(amount.text) - Decimal(expected)), Decimal("0.000001")
            )
        self.assertEqual(
            xml.findtext(
                "cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']", namespaces=NS
            ),
            "9.18",
        )
        self.assertEqual(
            xml.findtext(
                "cac:AdditionalDocumentReference[cbc:DocumentTypeCode='aedtotal-incl-vat']/cbc:DocumentDescription",
                namespaces=NS,
            ),
            "192.88",
        )
        self.assertIsNone(
            xml.find("cac:LegalMonetaryTotal/cbc:PayableRoundingAmount", NS)
        )
        original = issue.read(
            ["payload", "xml_content", "idempotency_key", "create_date", "write_date"]
        )
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), issue.xml_content)
        self.assertEqual(
            issue.read(
                [
                    "payload",
                    "xml_content",
                    "idempotency_key",
                    "create_date",
                    "write_date",
                ]
            ),
            original,
        )
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertFalse(
            issue.attempt_ids
            or issue.event_ids
            or issue.remote_id
            or issue.validation_receipt
        )

    def test_fractional_usd_discount_preserves_exact_allowance_and_original_ledger(
        self,
    ):
        # Independently worked: USD400.1234 * 0.125 = 50.015425;
        # 10% allowance = 5.0015425, raw net = 45.0138825,
        # VAT = 2.250694125 and gross = 47.264576625.
        # AED/USD3.672500 gives raw VAT8.2656741740625/gross173.5791576553125.
        # Native rounded headers USD45.01/2.25/47.26 convert to AED8.26/173.56.
        # IBR-131-AE permits the exact allowance/base, and IBR-147-AE must
        # reconcile that allowance with the actual high-precision unit price.
        # Configure the native quantity precision before entry: at the default
        # two places Odoo stores 0.125 as 0.13, changing the business quantity.
        self.env["decimal.precision"].search([("name", "=", "Product Unit")]).digits = 3
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
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic fractional S",
                "amount": 5,
                "ubl_cii_tax_category_code": "S",
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic fractional discount USD"}
        )
        invoice = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "journal_id": self.company_data["default_journal_sale"].id,
                    "currency_id": usd.id,
                    "invoice_date": "2019-01-01",
                    "l10n_ae_binding_id": binding.id,
                    "l10n_ae_fiscal_uuid": "dadadada-1111-4111-8111-111111111111",
                    "l10n_ae_tax_exchange_rate": "3.672500",
                    "l10n_ae_tax_exchange_date": "2019-01-01",
                    "l10n_ae_tax_exchange_source": "SYNTH-USD-FRACTIONAL-DISCOUNT",
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Synthetic fractional high precision service",
                                "quantity": 0.125,
                                "price_unit": 400.1234,
                                "discount": 10,
                                "account_id": self.company_data[
                                    "default_account_revenue"
                                ].id,
                                "tax_ids": [Command.set(tax.ids)],
                            }
                        )
                    ],
                }
            )
        )
        self.assertFalse(invoice.env.su)
        self.assertEqual(invoice.invoice_line_ids.quantity, 0.125)
        self.assertEqual(invoice.invoice_line_ids.price_unit, 400.1234)
        invoice.action_post()
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (45.01, 2.25, 47.26),
        )
        self.assertEqual(
            (invoice.amount_tax_signed, invoice.amount_total_signed), (8.26, 173.56)
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
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["xsd_valid"], report)
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        xml = etree.fromstring(issue.xml_content.encode())
        line = xml.find("cac:InvoiceLine", NS)
        for path, expected in [
            ("cbc:InvoicedQuantity", "0.125"),
            ("cac:Price/cbc:PriceAmount", "400.1234"),
            ("cbc:LineExtensionAmount", "45.01"),
        ]:
            self.assertEqual(
                Decimal(line.findtext(path, namespaces=NS)), Decimal(expected)
            )
        allowance = line.find("cac:AllowanceCharge", NS)
        self.assertEqual(
            allowance.findtext("cbc:ChargeIndicator", namespaces=NS), "false"
        )
        for path, expected in [
            ("cbc:BaseAmount", "50.015425"),
            ("cbc:Amount", "5.0015425"),
            ("cbc:MultiplierFactorNumeric", "10"),
        ]:
            self.assertLess(
                abs(
                    Decimal(allowance.findtext(path, namespaces=NS)) - Decimal(expected)
                ),
                Decimal("0.00000001"),
            )
        self.assertEqual(allowance.find("cbc:Amount", NS).get("currencyID"), "USD")
        for path, expected in [
            ("cac:ItemPriceExtension/cbc:Amount", "173.5791576553125"),
            ("cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount", "8.2656741740625"),
        ]:
            amount = line.find(path, NS)
            self.assertEqual(amount.get("currencyID"), "AED")
            # The native price/quantity are floats: tolerate serialization noise,
            # never a currency-cent rounding or substitution of the header base.
            self.assertLess(
                abs(Decimal(amount.text) - Decimal(expected)), Decimal("0.000001")
            )
        self.assertEqual(
            xml.findtext(
                "cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']", namespaces=NS
            ),
            "8.26",
        )
        self.assertEqual(
            xml.findtext(
                "cac:AdditionalDocumentReference[cbc:DocumentTypeCode='aedtotal-incl-vat']/cbc:DocumentDescription",
                namespaces=NS,
            ),
            "173.56",
        )
        self.assertIsNone(
            xml.find("cac:LegalMonetaryTotal/cbc:PayableRoundingAmount", NS)
        )
        original = issue.read(
            ["payload", "xml_content", "idempotency_key", "create_date", "write_date"]
        )
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), issue.xml_content)
        self.assertEqual(
            issue.read(
                [
                    "payload",
                    "xml_content",
                    "idempotency_key",
                    "create_date",
                    "write_date",
                ]
            ),
            original,
        )
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertFalse(
            issue.attempt_ids
            or issue.event_ids
            or issue.remote_id
            or issue.validation_receipt
        )

    def test_many_small_usd_lines_show_both_fiscal_accounting_differences(self):
        # Independent worked per-line policy: 100 * USD1.23, each tax USD0.06.
        # Native AED base 100 * 4.52 = 452; native VAT 100 * 0.22 = 22;
        # native gross 474. Header conversion: USD6 * 3.6725 -> AED22.04,
        # USD129 * 3.6725 -> AED473.75. Report both differences, never patch them.
        self.env.company.tax_calculation_rounding_method = "round_per_line"
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
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic many small S",
                "amount": 5,
                "ubl_cii_tax_category_code": "S",
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic many small lines"}
        )
        invoice = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "journal_id": self.company_data["default_journal_sale"].id,
                    "currency_id": usd.id,
                    "invoice_date": "2019-01-01",
                    "l10n_ae_binding_id": binding.id,
                    "l10n_ae_fiscal_uuid": "fafafafa-2222-4222-8222-222222222222",
                    "l10n_ae_tax_exchange_rate": "3.672500",
                    "l10n_ae_tax_exchange_date": "2019-01-01",
                    "l10n_ae_tax_exchange_source": "SYNTH-USD-100-SMALL-LINES",
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Synthetic small line " + str(index),
                                "quantity": 1,
                                "price_unit": 1.23,
                                "account_id": self.company_data[
                                    "default_account_revenue"
                                ].id,
                                "tax_ids": [Command.set(tax.ids)],
                            }
                        )
                        for index in range(100)
                    ],
                }
            )
        )
        self.assertFalse(invoice.env.su)
        invoice.action_post()
        self.assertEqual(len(invoice.invoice_line_ids), 100)
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (123, 6, 129),
        )
        self.assertEqual(
            (
                invoice.amount_untaxed_signed,
                invoice.amount_tax_signed,
                invoice.amount_total_signed,
            ),
            (452, 22, 474),
        )
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "create_date",
            "write_date",
        ]
        ledger = invoice.line_ids.read(ledger_fields)
        for action in (invoice.action_l10n_ae_prepare, invoice.preview_l10n_ae_pint):
            with self.assertRaisesRegex(
                UserError,
                r"Fiscal VAT: 22\.04 AED; accounting VAT: 22\.00 AED\. "
                r"Fiscal gross: 473\.75 AED; accounting gross: 474\.00 AED",
            ):
                action()
            self.assertEqual(invoice.state, "posted")
            self.assertFalse(invoice.l10n_ae_transmission_ids)
            self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
            self.assertEqual(
                self.env.company.tax_calculation_rounding_method, "round_per_line"
            )

    def test_all_exempt_tax_invoice_requires_review_without_blocking_posting(self):
        tax = self.env.ref(f"account.{self.env.company.id}_uae_sale_tax_exempted")
        tax.write(
            {
                "ubl_cii_tax_exemption_reason_code": "DL8.46.2",
                "ubl_cii_tax_exemption_reason": "Synthetic reviewed residential lease",
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic all-E tax invoice"}
        )
        invoice = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "journal_id": self.company_data["default_journal_sale"].id,
                    "invoice_date": "2019-01-01",
                    "currency_id": self.env.ref("base.AED").id,
                    "l10n_ae_binding_id": binding.id,
                    "l10n_ae_document_category": "tax",
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Synthetic reviewed residential lease",
                                "price_unit": 25,
                                "account_id": self.company_data[
                                    "default_account_revenue"
                                ].id,
                                "tax_ids": [Command.set(tax.ids)],
                            }
                        )
                    ],
                }
            )
        )
        self.assertFalse(invoice.env.su)
        with self.assertRaisesRegex(UserError, "IBR-151-AE"):
            invoice.action_l10n_ae_validate()
        self.assertEqual(invoice.state, "draft")
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        self.env.company.l10n_ae_edi_auto_prepare = True
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (25, 0, 25),
        )
        self.assertEqual(invoice.l10n_ae_preparation_state, "review")
        self.assertIn("IBR-151-AE", invoice.l10n_ae_preparation_error)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "create_date",
            "write_date",
        ]
        ledger = invoice.line_ids.read(ledger_fields)
        with self.assertRaisesRegex(UserError, "IBR-151-AE"):
            invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertEqual(invoice.l10n_ae_document_category, "tax")
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        self.assertEqual(tax.ubl_cii_tax_exemption_reason_code, "DL8.46.2")

    def test_e_o_only_invoice_and_credit_categories_are_refused_before_issue(self):
        exempt = self.env.ref(f"account.{self.env.company.id}_uae_sale_tax_exempted")
        exempt.write(
            {
                "ubl_cii_tax_exemption_reason_code": "DL8.46.2",
                "ubl_cii_tax_exemption_reason": "Synthetic reviewed residential lease",
            }
        )
        outside = self.env.ref(f"account.{self.env.company.id}_uae_out_of_scope")
        fixed_charge = self.tax_sale_a.copy(
            {
                "name": "Synthetic fixed charge is not a line VAT category",
                "amount_type": "fixed",
                "amount": 5,
                "include_base_amount": True,
                "ubl_cii_tax_category_code": "S",
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic E/O type boundary"}
        )
        # A line's fixed charge or an unclassified/no-tax line must not invent
        # the non-E/O item category required by the published 380/381 predicate.
        for kind in ("out_invoice", "out_refund"):
            for label, line_taxes, net, vat in (
                ("E", [exempt], 25, 0),
                ("O", [outside], 25, 0),
                ("E plus O", [exempt, outside], 50, 0),
                ("no tax", [self.env["account.tax"]], 25, 0),
                ("E with fixed S charge", [exempt | fixed_charge], 25, 5),
            ):
                with self.subTest(kind=kind, categories=label):
                    values = {
                        "move_type": kind,
                        "partner_id": self.partner_a.id,
                        "journal_id": self.company_data["default_journal_sale"].id,
                        "invoice_date": "2019-01-01",
                        "currency_id": self.env.ref("base.AED").id,
                        "l10n_ae_binding_id": binding.id,
                        "l10n_ae_document_category": "tax",
                        "invoice_line_ids": [
                            Command.create(
                                {
                                    "name": "Synthetic " + label,
                                    "price_unit": 25,
                                    "account_id": self.company_data[
                                        "default_account_revenue"
                                    ].id,
                                    "tax_ids": [Command.set(taxes.ids)],
                                }
                            )
                            for taxes in line_taxes
                        ],
                    }
                    if kind == "out_refund":
                        values.update(
                            l10n_ae_credit_reason="DL8.61.1.C",
                            l10n_ae_credit_reference_ids=[
                                Command.create(
                                    {
                                        "number": "SYNTH-REVIEWED-ORIGINAL",
                                        "issue_date": "2018-12-01",
                                        "review_evidence": "Synthetic original review",
                                        "allocated_net": net,
                                        "allocated_tax": vat,
                                    }
                                )
                            ],
                        )
                    invoice = (
                        self.env["account.move"]
                        .with_user(self.simple_accountman)
                        .create(values)
                    )
                    with self.assertRaisesRegex(UserError, "IBR-151-AE"):
                        invoice.action_l10n_ae_validate()
                    self.assertEqual(invoice.state, "draft")
                    self.assertFalse(invoice.l10n_ae_transmission_ids)
                    self.assertFalse(invoice.l10n_ae_fiscal_uuid)
                    self.assertEqual(invoice.l10n_ae_document_category, "tax")

    def test_commercial_e_o_documents_keep_their_explicit_non_tax_types(self):
        exempt = self.env.ref(f"account.{self.env.company.id}_uae_sale_tax_exempted")
        exempt.write(
            {
                "ubl_cii_tax_exemption_reason_code": "DL8.46.2",
                "ubl_cii_tax_exemption_reason": "Synthetic reviewed residential lease",
            }
        )
        outside = self.env.ref(f"account.{self.env.company.id}_uae_out_of_scope")
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic commercial E/O",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        for kind, node, code in (
            ("out_invoice", "cbc:InvoiceTypeCode", "480"),
            ("out_refund", "cbc:CreditNoteTypeCode", "81"),
        ):
            for label, taxes_per_line, net in (
                ("E", [exempt], 25),
                ("O", [outside], 25),
                ("E and O", [exempt, outside], 50),
            ):
                with self.subTest(kind=kind, categories=label):
                    values = {
                        "move_type": kind,
                        "partner_id": self.partner_a.id,
                        "journal_id": self.company_data["default_journal_sale"].id,
                        "invoice_date": "2019-01-01",
                        "currency_id": self.env.ref("base.AED").id,
                        "l10n_ae_binding_id": binding.id,
                        "l10n_ae_document_category": "commercial",
                        "invoice_line_ids": [
                            Command.create(
                                {
                                    "name": "Synthetic commercial " + label,
                                    "price_unit": 25,
                                    "account_id": self.company_data[
                                        "default_account_revenue"
                                    ].id,
                                    "tax_ids": [Command.set(taxes.ids)],
                                }
                            )
                            for taxes in taxes_per_line
                        ],
                    }
                    if kind == "out_refund":
                        values.update(
                            l10n_ae_credit_reason="DL8.61.1.C",
                            l10n_ae_credit_reference_ids=[
                                Command.create(
                                    {
                                        "number": "SYNTH-REVIEWED-COMMERCIAL-ORIGINAL",
                                        "issue_date": "2018-12-01",
                                        "review_evidence": "Synthetic review",
                                        "allocated_net": net,
                                        "allocated_tax": 0,
                                    }
                                )
                            ],
                        )
                    invoice = (
                        self.env["account.move"]
                        .with_user(self.simple_accountman)
                        .create(values)
                    )
                    invoice.action_l10n_ae_validate()
                    invoice.action_post()
                    ledger_fields = [
                        "debit",
                        "credit",
                        "amount_currency",
                        "create_date",
                        "write_date",
                    ]
                    ledger = invoice.line_ids.read(ledger_fields)
                    invoice.action_l10n_ae_prepare()
                    document = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
                    self.assertEqual(document.findtext(node, namespaces=NS), code)
                    self.assertEqual(invoice.amount_untaxed, net)
                    self.assertEqual(invoice.amount_tax, 0)
                    self.assertEqual(invoice.amount_total, net)
                    report = invoice.validate_l10n_ae_pint()
                    self.assertTrue(report["valid"], report)
                    self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
                    self.assertEqual(invoice.l10n_ae_document_category, "commercial")
                    issue = invoice.l10n_ae_transmission_id
                    self.assertFalse(
                        issue.attempt_ids or issue.event_ids or issue.remote_id
                    )

    def test_exempt_without_uae_legal_code_is_refused_before_preparation(self):
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic exempt missing legal code",
                "amount": 0,
                "ubl_cii_tax_category_code": "E",
                "ubl_cii_tax_exemption_reason": "Reviewed text without legal code",
                "ubl_cii_tax_exemption_reason_code": False,
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Synthetic E guard"})
        invoice = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "journal_id": self.company_data["default_journal_sale"].id,
                    "invoice_date": "2019-01-01",
                    "l10n_ae_binding_id": binding.id,
                    "l10n_ae_document_category": "commercial",
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Synthetic exempt service",
                                "quantity": 1,
                                "price_unit": 25,
                                "account_id": self.company_data[
                                    "default_account_revenue"
                                ].id,
                                "tax_ids": [Command.set(tax.ids)],
                            }
                        )
                    ],
                }
            )
        )
        self.assertFalse(invoice.env.su)
        with self.assertRaisesRegex(UserError, "IBR-167-AE"):
            invoice.action_l10n_ae_validate()
        self.assertEqual(invoice.state, "draft")
        self.assertEqual((invoice.amount_untaxed, invoice.amount_tax), (25, 0))
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(tax.ubl_cii_tax_exemption_reason_code)
        self.env.company.l10n_ae_edi_auto_prepare = True
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.l10n_ae_preparation_state, "review")
        self.assertIn("IBR-167-AE", invoice.l10n_ae_preparation_error)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        ledger = invoice.line_ids.read(
            ["debit", "credit", "amount_currency", "create_date", "write_date"]
        )
        for code in (False, "VATEX-EU-132"):
            with self.subTest(code=code):
                tax.ubl_cii_tax_exemption_reason_code = code
                with self.assertRaisesRegex(UserError, "IBR-167-AE"):
                    invoice.action_l10n_ae_prepare()
                self.assertEqual(invoice.state, "posted")
                self.assertEqual(
                    invoice.line_ids.read(
                        [
                            "debit",
                            "credit",
                            "amount_currency",
                            "create_date",
                            "write_date",
                        ]
                    ),
                    ledger,
                )
                self.assertFalse(invoice.l10n_ae_transmission_ids)
                self.assertEqual(tax.ubl_cii_tax_exemption_reason_code, code)

    def test_four_uae_exemption_codes_survive_native_emission(self):
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Synthetic E codes"})
        manager = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
        )
        self.assertFalse(manager.env.su)
        for code, description in [
            ("DL8.46.1", "Certain financial services"),
            ("DL8.46.2", "Supply of residential units (lease or sale)"),
            ("DL8.46.3", "Bare land"),
            ("DL8.46.4", "Local passenger transport"),
        ]:
            with self.subTest(code=code):
                reason = "Synthetic reviewed: " + description
                tax = self.tax_sale_a.copy(
                    {
                        "name": "Synthetic E " + code,
                        "amount": 0,
                        "ubl_cii_tax_category_code": "E",
                        "ubl_cii_tax_exemption_reason_code": code,
                        "ubl_cii_tax_exemption_reason": reason,
                    }
                )
                self.assertEqual(tax.ubl_cii_tax_exemption_reason_code, code)
                invoice = manager.create(
                    {
                        "move_type": "out_invoice",
                        "partner_id": self.partner_a.id,
                        "journal_id": self.company_data["default_journal_sale"].id,
                        "invoice_date": "2019-01-01",
                        "l10n_ae_binding_id": binding.id,
                        "l10n_ae_document_category": "commercial",
                        "l10n_ae_fiscal_uuid": (
                            "efabcdef-2222-4222-8222-22222222222" + code[-1]
                        ),
                        "invoice_line_ids": [
                            Command.create(
                                {
                                    "name": reason,
                                    "quantity": 1,
                                    "price_unit": 25,
                                    "account_id": self.company_data[
                                        "default_account_revenue"
                                    ].id,
                                    "tax_ids": [Command.set(tax.ids)],
                                }
                            )
                        ],
                    }
                )
                invoice.action_post()
                invoice.action_l10n_ae_prepare()
                report = invoice.validate_l10n_ae_pint()
                self.assertTrue(report["valid"], report)
                xml = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
                self.assertEqual(
                    xml.findtext("cbc:InvoiceTypeCode", namespaces=NS), "480"
                )
                self.assertEqual(
                    (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
                    (25, 0, 25),
                )
                for path in (
                    "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory",
                    "cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory",
                ):
                    category = xml.find(path, NS)
                    self.assertIsNotNone(category)
                    self.assertEqual(category.findtext("cbc:ID", namespaces=NS), "E")
                    self.assertEqual(
                        category.findtext("cbc:TaxExemptionReasonCode", namespaces=NS),
                        code,
                    )
                    self.assertEqual(
                        category.findtext("cbc:TaxExemptionReason", namespaces=NS),
                        reason,
                    )
                    self.assertIsNone(category.find("cbc:Percent", NS))
                self.assertIsNone(
                    xml.find("cac:InvoiceLine/cac:ItemPriceExtension/cac:TaxTotal", NS)
                )
                issue = invoice.l10n_ae_transmission_id
                self.assertFalse(
                    issue.attempt_ids
                    or issue.event_ids
                    or issue.remote_id
                    or issue.validation_receipt
                )

    def test_mixed_eur_tax_invoice_preserves_exempt_absence(self):
        eur = self.env.ref("base.EUR")
        eur.active = True
        self.env["res.currency.rate"].create(
            {
                "currency_id": eur.id,
                "company_id": self.env.company.id,
                "name": "2019-01-01",
                "rate": 1 / 4.1,
            }
        )
        standard = self.tax_sale_a.copy(
            {
                "name": "Synthetic S matrix",
                "amount": 5,
                "ubl_cii_tax_category_code": "S",
            }
        )
        zero = standard.copy(
            {
                "name": "Synthetic Z matrix",
                "amount": 0,
                "ubl_cii_tax_category_code": "Z",
                "ubl_cii_tax_exemption_reason": "Synthetic reviewed zero-rated supply",
            }
        )
        exempt = zero.copy(
            {
                "name": "Synthetic E matrix",
                "ubl_cii_tax_exemption_reason_code": "DL8.46.2",
                "ubl_cii_tax_category_code": "E",
                "ubl_cii_tax_exemption_reason": "Synthetic reviewed exempt supply",
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Synthetic EUR SZE"})
        manager = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
        )
        self.assertFalse(manager.env.su)
        invoice = manager.create(
            {
                "move_type": "out_invoice",
                "partner_id": self.partner_a.id,
                "journal_id": self.company_data["default_journal_sale"].id,
                "currency_id": eur.id,
                "invoice_date": "2019-01-01",
                "l10n_ae_binding_id": binding.id,
                "l10n_ae_fiscal_uuid": "efabcdef-1111-4111-8111-111111111111",
                "l10n_ae_tax_exchange_rate": "4.100000",
                "l10n_ae_tax_exchange_date": "2019-01-01",
                "l10n_ae_tax_exchange_source": "SYNTH-EUR-AED-SZE-2019",
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": "Synthetic " + category,
                            "quantity": 1,
                            "price_unit": amount,
                            "account_id": self.company_data[
                                "default_account_revenue"
                            ].id,
                            "tax_ids": [Command.set(tax.ids)],
                        }
                    )
                    for category, amount, tax in [
                        ("S", 100, standard),
                        ("Z", 50, zero),
                        ("E", 25, exempt),
                    ]
                ],
            }
        )
        invoice.action_post()
        self.assertEqual(
            (
                invoice.amount_untaxed,
                invoice.amount_tax,
                invoice.amount_total,
                invoice.amount_residual,
            ),
            (175, 5, 180, 180),
        )
        self.assertEqual(
            (invoice.amount_total_signed, invoice.amount_tax_signed), (738, 20.50)
        )
        ledger = invoice.line_ids.read(
            [
                "account_id",
                "debit",
                "credit",
                "amount_currency",
                "currency_id",
                "create_date",
                "write_date",
            ]
        )
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["xsd_valid"], report)
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        self.assertEqual(report["profile"], "billing")
        xml = etree.fromstring(issue.xml_content.encode())
        self.assertEqual(xml.findtext("cbc:InvoiceTypeCode", namespaces=NS), "380")
        self.assertEqual(xml.findtext("cbc:DocumentCurrencyCode", namespaces=NS), "EUR")
        self.assertEqual(xml.findtext("cbc:TaxCurrencyCode", namespaces=NS), "AED")
        for term, expected in [
            ("TaxExclusiveAmount", 175),
            ("TaxInclusiveAmount", 180),
            ("PayableAmount", 180),
        ]:
            self.assertEqual(
                Decimal(
                    xml.findtext("cac:LegalMonetaryTotal/cbc:" + term, namespaces=NS)
                ),
                Decimal(expected),
            )
        self.assertEqual(
            xml.findtext("cac:TaxExchangeRate/cbc:SourceCurrencyCode", namespaces=NS),
            "EUR",
        )
        self.assertEqual(
            xml.findtext("cac:TaxExchangeRate/cbc:TargetCurrencyCode", namespaces=NS),
            "AED",
        )
        self.assertEqual(
            xml.findtext("cac:TaxExchangeRate/cbc:CalculationRate", namespaces=NS),
            "4.100000",
        )
        self.assertEqual(
            Decimal(
                xml.findtext(
                    "cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']", namespaces=NS
                )
            ),
            Decimal("20.50"),
        )
        self.assertEqual(
            xml.findtext(
                "cac:AdditionalDocumentReference[cbc:DocumentTypeCode='aedtotal-incl-vat']/cbc:DocumentDescription",
                namespaces=NS,
            ),
            "738.00",
        )
        lines = xml.findall("cac:InvoiceLine", NS)
        self.assertEqual(len(lines), 3)
        for line, category, net, gross, vat in zip(
            lines,
            ["S", "Z", "E"],
            [100, 50, 25],
            ["430.50", "205.00", "102.50"],
            ["20.50", "0.00", None],
            strict=True,
        ):
            self.assertEqual(
                line.findtext(
                    "cac:Item/cac:ClassifiedTaxCategory/cbc:ID", namespaces=NS
                ),
                category,
            )
            self.assertEqual(
                Decimal(line.findtext("cbc:LineExtensionAmount", namespaces=NS)),
                Decimal(net),
            )
            self.assertEqual(
                Decimal(
                    line.findtext("cac:ItemPriceExtension/cbc:Amount", namespaces=NS)
                ),
                Decimal(gross),
            )
            vat_node = line.find(
                "cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount", NS
            )
            if vat is None:
                self.assertIsNone(vat_node)
                self.assertEqual(
                    line.findtext(
                        "cac:Item/cac:ClassifiedTaxCategory/cbc:TaxExemptionReasonCode",
                        namespaces=NS,
                    ),
                    "DL8.46.2",
                )
                self.assertIsNone(
                    line.find(
                        "cac:Item/cac:ClassifiedTaxCategory/cbc:Percent",
                        NS,
                    )
                )
            else:
                self.assertIsNotNone(vat_node)
                self.assertEqual(vat_node.get("currencyID"), "AED")
                self.assertEqual(Decimal(vat_node.text), Decimal(vat))
        categories = xml.findall("cac:TaxTotal/cac:TaxSubtotal", NS)
        self.assertEqual(len(categories), 3)
        by_category = {
            node.findtext("cac:TaxCategory/cbc:ID", namespaces=NS): node
            for node in categories
        }
        self.assertEqual(set(by_category), {"S", "Z", "E"})
        self.assertIsNone(by_category["E"].find("cac:TaxCategory/cbc:Percent", NS))
        self.assertEqual(
            by_category["E"].findtext(
                "cac:TaxCategory/cbc:TaxExemptionReasonCode", namespaces=NS
            ),
            "DL8.46.2",
        )
        for category, net, vat in [("S", 100, 5), ("Z", 50, 0), ("E", 25, 0)]:
            self.assertEqual(
                Decimal(
                    by_category[category].findtext("cbc:TaxableAmount", namespaces=NS)
                ),
                Decimal(net),
            )
            self.assertEqual(
                Decimal(by_category[category].findtext("cbc:TaxAmount", namespaces=NS)),
                Decimal(vat),
            )
        self.assertEqual(
            json.loads(issue.payload)["tax_exchange"],
            {
                "source_currency": "EUR",
                "target_currency": "AED",
                "rate": "4.100000",
                "date": "2019-01-01",
                "source": "SYNTH-EUR-AED-SZE-2019",
            },
        )
        original = (
            issue.id,
            issue.payload,
            issue.xml_content,
            issue.idempotency_key,
            issue.create_date,
            issue.write_date,
        )
        invoice.action_l10n_ae_prepare()
        self.assertEqual(
            (
                issue.id,
                issue.payload,
                issue.xml_content,
                issue.idempotency_key,
                issue.create_date,
                issue.write_date,
            ),
            original,
        )
        self.assertEqual(invoice.preview_l10n_ae_pint(), issue.xml_content)
        self.assertEqual(
            invoice.line_ids.read(
                [
                    "account_id",
                    "debit",
                    "credit",
                    "amount_currency",
                    "currency_id",
                    "create_date",
                    "write_date",
                ]
            ),
            ledger,
        )
        self.assertFalse(
            issue.attempt_ids
            or issue.event_ids
            or issue.remote_id
            or issue.validation_receipt
        )
