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
        with self.assertRaisesRegex(UserError, "IBR-167-AE"):
            invoice.action_post()
        self.assertEqual(invoice.state, "draft")
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.env.company.l10n_ae_edi_auto_prepare = False
        invoice.action_post()
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
