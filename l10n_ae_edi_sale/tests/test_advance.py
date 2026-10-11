# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json

from lxml import etree

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_ae_edi.tests.common import review_controlled_binding

NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}


@tagged("post_install", "-at_install")
class TestUaeSalesAdvance(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            "sales_team.group_sale_salesman_all_leads"
        )
        cls.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        for partner, tin, license_number in (
            (cls.env.company.partner_id, "1000000001", "SYNTH-ADVANCE-SUPPLIER"),
            (cls.partner_a, "1000000002", "SYNTH-ADVANCE-BUYER"),
        ):
            partner.write(
                {
                    "country_id": cls.env.ref("base.ae").id,
                    "vat": tin + "00003",
                    "street": "Synthetic advance street",
                    "city": "Dubai",
                    "state_id": cls.env.ref("base.state_ae_du").id,
                    "routing_scheme": "0235",
                    "routing_endpoint": tin,
                    "l10n_ae_registration_type": "TL",
                    "l10n_ae_registration_authority": "Synthetic Authority",
                    "additional_identifiers": {"AE_TL": license_number},
                }
            )
        cls.advance_account = cls.env["account.account"].create(
            {
                "name": "Synthetic taxed advances",
                "code": "ADVANCE001",
                "account_type": "liability_current",
            }
        )
        cls.env.company.downpayment_account_id = cls.advance_account
        cls.product_a.write(
            {
                "type": "service",
                "invoice_policy": "order",
                "l10n_ae_service_accounting_code": "SYNTHETIC-ADVANCE-SAC",
                "l10n_ae_service_accounting_version": "SYNTHETIC-SAC-2026",
            }
        )
        cls.binding = cls.env["l10n_ae.edi.binding"].create(
            {
                "name": "Sales advance simulation",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        review_controlled_binding(
            cls.binding,
            cls.simple_accountman,
            capabilities=("submit", "lookup", "report", "billing_invoice"),
            note="Synthetic controlled contract for UAE sales-advance issue tests.",
        )

    def _create_invoice(self, order, method, amount=0):
        before = order.invoice_ids
        self.env["sale.advance.payment.inv"].create(
            {
                "sale_order_ids": [Command.set(order.ids)],
                "advance_payment_method": method,
                "amount": amount,
                "deduct_down_payments": True,
            }
        ).create_invoices()
        invoice = order.invoice_ids - before
        self.assertEqual(len(invoice), 1)
        invoice.invoice_date = fields.Date.today()
        invoice.l10n_ae_binding_id = self.binding
        return invoice

    def _create_fiscal_country_sales_orders(self):
        # Set both countries before creating business documents. US accepts the
        # synthetic 15-digit UAE registration without bypassing native VAT checks.
        self.env.company.write(
            {
                "country_id": self.env.ref("base.us").id,
                "account_fiscal_country_id": self.env.ref("base.ae").id,
                "l10n_ae_edi_auto_prepare": False,
            }
        )
        self.env.company.partner_id.write(
            {
                "state_id": self.env.ref("base.state_us_5").id,
                "city": "San Francisco",
                "zip": "94105",
            }
        )
        other_data = self.setup_other_company(name="Synthetic non-UAE fiscal seller")
        other = other_data["company"]
        other.write(
            {
                "country_id": self.env.ref("base.ae").id,
                "account_fiscal_country_id": self.env.ref("base.fr").id,
                "l10n_ae_edi_auto_prepare": False,
            }
        )
        # Native tax country is configured independently of the address and
        # cached chart defaults; keep the counterpart's accounting coherent.
        other_group = (
            self.env["account.tax.group"]
            .with_company(other)
            .create(
                {
                    "name": "Synthetic counterpart VAT",
                    "country_id": self.env.ref("base.fr").id,
                }
            )
        )
        other_data["default_tax_sale"].write(
            {
                "country_id": self.env.ref("base.fr").id,
                "tax_group_id": other_group.id,
            }
        )
        other.partner_id.write(
            {
                "street": "Synthetic counterpart street",
                "city": "Dubai",
                "state_id": self.env.ref("base.state_ae_du").id,
            }
        )
        other.downpayment_account_id = (
            self.env["account.account"]
            .with_company(other)
            .create(
                {
                    "name": "Synthetic counterpart advances",
                    "code": "ADVANCE002",
                    "account_type": "liability_current",
                }
            )
        )
        self.product_a.with_company(other).property_account_income_id = other_data[
            "default_account_revenue"
        ]
        self.simple_accountman.write(
            {
                "company_ids": [Command.link(other.id)],
                "group_ids": [
                    Command.link(
                        self.env.ref("sales_team.group_sale_salesman_all_leads").id
                    )
                ],
            }
        )
        cases = []
        for label, data, address, fiscal in (
            ("US/AE", self.company_data, "US", "AE"),
            ("AE/FR", other_data, "AE", "FR"),
        ):
            company = data["company"]
            operator = (
                self.env["sale.order"]
                .with_user(self.simple_accountman)
                .with_company(company)
                .with_context(allowed_company_ids=[company.id])
            )
            self.assertFalse(operator.env.su)
            self.assertFalse(operator.env.user.has_group("base.group_system"))
            self.assertEqual(company.country_code, address)
            self.assertEqual(company.account_fiscal_country_id.code, fiscal)
            self.assertEqual(data["default_tax_sale"].amount, 5)
            self.assertEqual(data["default_tax_sale"].country_id.code, fiscal)
            order = operator.create(
                {
                    "partner_id": self.partner_a.id,
                    "order_line": [
                        Command.create(
                            {
                                "product_id": self.product_a.id,
                                "product_uom_qty": 1,
                                "price_unit": 10000,
                                "tax_ids": [Command.set(data["default_tax_sale"].ids)],
                            }
                        )
                    ],
                }
            )
            order.action_confirm()
            cases.append((label, order))
        return cases

    def test_final_advance_reference_follows_fiscal_country_not_address(self):
        # Independent balance: net10000/VAT500 less advance1000/VAT50.
        for label, order in self._create_fiscal_country_sales_orders():
            with self.subTest(country_pair=label):
                operator_env = order.env
                operator_env["sale.advance.payment.inv"].create(
                    {
                        "sale_order_ids": [Command.set(order.ids)],
                        "advance_payment_method": "percentage",
                        "amount": 10,
                    }
                ).create_invoices()
                advance = order.invoice_ids
                self.assertEqual(len(advance), 1)
                advance.invoice_date = "2019-01-01"
                advance.action_post()
                self.assertEqual(
                    (advance.amount_untaxed, advance.amount_tax, advance.amount_total),
                    (1000, 50, 1050),
                )
                if label == "US/AE":
                    advance.l10n_ae_binding_id = self.binding
                    advance.action_l10n_ae_prepare()
                    report = advance.validate_l10n_ae_pint()
                    self.assertTrue(report["valid"], report)
                    original_xml = advance.preview_l10n_ae_pint()
                    original_payload = advance.l10n_ae_transmission_id.payload
                    original_uuid = advance.l10n_ae_fiscal_uuid
                operator_env["sale.advance.payment.inv"].create(
                    {
                        "sale_order_ids": [Command.set(order.ids)],
                        "advance_payment_method": "delivered",
                        "deduct_down_payments": True,
                    }
                ).create_invoices()
                final = order.invoice_ids - advance
                self.assertEqual(len(final), 1)
                final.invoice_date = "2019-01-02"
                final.action_post()
                self.assertEqual(
                    (final.amount_untaxed, final.amount_tax, final.amount_total),
                    (9000, 450, 9450),
                )
                self.assertEqual(
                    final.invoice_line_ids.filtered("is_downpayment").sale_line_ids,
                    advance.invoice_line_ids.sale_line_ids,
                )
                if label == "AE/FR":
                    self.assertFalse(final.l10n_ae_advance_reference_summary)
                    self.assertFalse(advance.l10n_ae_transmission_id)
                    self.assertFalse(advance.l10n_ae_fiscal_uuid)
                    self.assertFalse(final.l10n_ae_transmission_id)
                    self.assertFalse(final.l10n_ae_fiscal_uuid)
                    continue
                self.assertEqual(
                    final.l10n_ae_advance_reference_summary,
                    f"{advance.name} | 2019-01-01 | Net: 1000.00 | VAT: 50.00 AED",
                )
                final.l10n_ae_binding_id = self.binding
                final.action_l10n_ae_prepare()
                xml = etree.fromstring(final.preview_l10n_ae_pint().encode())
                self.assertEqual(
                    xml.xpath(
                        "//cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID/text()",
                        namespaces=NS,
                    ),
                    [advance.name],
                )
                self.assertEqual(
                    xml.xpath(
                        "//cac:AccountingSupplierParty//cac:Country/cbc:IdentificationCode/text()",
                        namespaces=NS,
                    ),
                    ["US"],
                )
                self.assertEqual(
                    json.loads(final.l10n_ae_transmission_id.payload)["taxed_advances"],
                    [
                        {
                            "source_move_id": advance.id,
                            "number": advance.name,
                            "issue_date": "2019-01-01",
                            "consumed_net": "1000.00",
                            "consumed_vat": "50.00",
                        }
                    ],
                )
                self.assertEqual(advance.preview_l10n_ae_pint(), original_xml)
                self.assertEqual(
                    advance.l10n_ae_transmission_id.payload, original_payload
                )
                self.assertEqual(advance.l10n_ae_fiscal_uuid, original_uuid)
                for invoice in (advance, final):
                    report = invoice.validate_l10n_ae_pint()
                    self.assertTrue(report["valid"], report)

    def test_consumed_advance_reset_follows_fiscal_country_not_address(self):
        # A posted final consumes1000/VAT50 even before any fiscal preparation.
        for label, order in self._create_fiscal_country_sales_orders():
            with self.subTest(country_pair=label):
                operator_env = order.env
                operator_env["sale.advance.payment.inv"].create(
                    {
                        "sale_order_ids": [Command.set(order.ids)],
                        "advance_payment_method": "percentage",
                        "amount": 10,
                    }
                ).create_invoices()
                advance = order.invoice_ids
                advance.invoice_date = "2019-01-01"
                advance.action_post()
                operator_env["sale.advance.payment.inv"].create(
                    {
                        "sale_order_ids": [Command.set(order.ids)],
                        "advance_payment_method": "delivered",
                        "deduct_down_payments": True,
                    }
                ).create_invoices()
                final = order.invoice_ids - advance
                final.invoice_date = "2019-01-02"
                final.action_post()
                self.assertEqual(
                    (
                        advance.state,
                        advance.amount_total,
                        final.state,
                        final.amount_total,
                    ),
                    ("posted", 1050, "posted", 9450),
                )
                self.assertFalse(advance.l10n_ae_fiscal_uuid)
                self.assertFalse(advance.l10n_ae_transmission_id)
                self.assertFalse(final.l10n_ae_fiscal_uuid)
                self.assertFalse(final.l10n_ae_transmission_id)
                # Compare persisted native accounting after posting, before the
                # exception harness opens its automatically flushing savepoint.
                operator_env.flush_all()
                ledger_fields = [
                    "account_id",
                    "debit",
                    "credit",
                    "balance",
                    "write_uid",
                    "write_date",
                ]
                advance_ledger = advance.line_ids.sorted("id").read(ledger_fields)
                final_ledger = final.line_ids.sorted("id").read(ledger_fields)
                if label == "US/AE":
                    with self.assertRaisesRegex(
                        UserError, "advance has already been deducted"
                    ):
                        advance.button_draft()
                    self.assertEqual(advance.state, "posted")
                    self.assertEqual(
                        advance.line_ids.sorted("id").read(ledger_fields),
                        advance_ledger,
                    )
                else:
                    advance.button_draft()
                    self.assertEqual(advance.state, "draft")
                self.assertEqual((final.state, final.amount_total), ("posted", 9450))
                self.assertEqual(
                    final.line_ids.sorted("id").read(ledger_fields), final_ledger
                )
                self.assertFalse(advance.l10n_ae_correction_id)

    def test_taxed_advance_receipt_and_final_balance_keep_original_reference(self):
        # Independently worked MoF balance example: 10000/500 less 1000/50.
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner_a.id,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product_a.id,
                            "product_uom_qty": 1,
                            "price_unit": 10000,
                            "tax_ids": [Command.set(self.tax_sale_a.ids)],
                        }
                    )
                ],
            }
        )
        order.action_confirm()
        advance = self._create_invoice(order, "percentage", 10)
        advance.action_post()
        advance.action_l10n_ae_prepare()
        original_xml = advance.preview_l10n_ae_pint()
        self.assertEqual(
            (advance.amount_untaxed, advance.amount_tax, advance.amount_total),
            (1000, 50, 1050),
        )
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=advance.ids
        ).create(
            {"journal_id": self.company_data["default_journal_bank"].id, "amount": 1050}
        ).action_create_payments()
        self.assertEqual(advance.amount_residual, 0)
        self.assertEqual(advance.preview_l10n_ae_pint(), original_xml)

        final = self._create_invoice(order, "delivered")
        final.action_post()
        final.action_l10n_ae_prepare()
        self.assertEqual(
            (
                final.amount_untaxed,
                final.amount_tax,
                final.amount_total,
                final.amount_residual,
            ),
            (9000, 450, 9450, 9450),
        )
        self.assertEqual(
            sum(
                final.line_ids.filtered(
                    lambda line: line.account_id == self.advance_account
                ).mapped("balance")
            ),
            1000,
        )
        final_xml = final.preview_l10n_ae_pint()
        xml = etree.fromstring(final_xml.encode())
        self.assertEqual(
            xml.xpath(
                "//cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID/text()",
                namespaces=NS,
            ),
            [advance.name],
        )
        self.assertEqual(
            xml.xpath(
                "//cac:BillingReference/cac:InvoiceDocumentReference/cbc:IssueDate/text()",
                namespaces=NS,
            ),
            [str(advance.invoice_date)],
        )
        self.assertEqual(
            json.loads(final.l10n_ae_transmission_id.payload)["taxed_advances"],
            [
                {
                    "source_move_id": advance.id,
                    "number": advance.name,
                    "issue_date": str(advance.invoice_date),
                    "consumed_net": "1000.00",
                    "consumed_vat": "50.00",
                }
            ],
        )
        self.assertIn(advance.name, final.l10n_ae_advance_reference_summary)
        self.assertNotEqual(final.l10n_ae_fiscal_uuid, advance.l10n_ae_fiscal_uuid)
        total = xml.find("cac:LegalMonetaryTotal", NS)
        self.assertEqual(
            total.findtext("cbc:TaxExclusiveAmount", namespaces=NS), "9000.00"
        )
        self.assertEqual(
            total.findtext("cbc:TaxInclusiveAmount", namespaces=NS), "9450.00"
        )
        self.assertEqual(total.findtext("cbc:PayableAmount", namespaces=NS), "9450.00")
        self.assertIn(
            total.findtext("cbc:PrepaidAmount", namespaces=NS), (None, "0.00")
        )
        advance_report = advance.validate_l10n_ae_pint()
        final_report = final.validate_l10n_ae_pint()
        self.assertTrue(advance_report["valid"], advance_report)
        self.assertTrue(final_report["valid"], final_report)
        self.assertTrue(
            final.invoice_line_ids.filtered("is_downpayment").write(
                {"quantity": -1, "price_unit": 1000, "discount": 0}
            )
        )
        self.assertEqual(final.preview_l10n_ae_pint(), final_xml)

    def test_fixed_advance_and_final_keep_native_allocations_and_frozen_references(
        self,
    ):
        # Independently worked fixed gross33.34 against net100/VAT5/gross105:
        # VAT5*(33.34/105) rounds1.59; net33.34-1.59=31.75. The final
        # deducts those allocated amounts: net68.25/VAT3.41/gross71.66.
        # Raw line VAT follows the precise native proportional price, not the
        # rounded header allocation; original XML must pass unchanged rules.
        self.simple_accountman.group_ids |= self.env.ref(
            "sales_team.group_sale_salesman_all_leads"
        )
        operator_env = (
            self.env["sale.order"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
            .env
        )
        self.assertFalse(operator_env.su)
        order = operator_env["sale.order"].create(
            {
                "partner_id": self.partner_a.id,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product_a.id,
                            "product_uom_qty": 1,
                            "price_unit": 100,
                            "tax_ids": [Command.set(self.tax_sale_a.ids)],
                        }
                    )
                ],
            }
        )
        order.action_confirm()
        operator_env["sale.advance.payment.inv"].create(
            {
                "sale_order_ids": [Command.set(order.ids)],
                "advance_payment_method": "fixed",
                "fixed_amount": 33.34,
            }
        ).create_invoices()
        advance = order.invoice_ids
        self.assertEqual(len(advance), 1)
        advance.write(
            {"invoice_date": "2019-01-01", "l10n_ae_binding_id": self.binding.id}
        )
        advance.action_post()
        self.assertEqual(
            (advance.amount_untaxed, advance.amount_tax, advance.amount_total),
            (31.75, 1.59, 33.34),
        )
        advance.action_l10n_ae_prepare()
        advance_report = advance.validate_l10n_ae_pint()
        self.assertTrue(advance_report["valid"], advance_report)
        advance_xml = etree.fromstring(
            advance.l10n_ae_transmission_id.xml_content.encode()
        )
        for xpath, expected in [
            ("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", "31.75"),
            ("cac:TaxTotal/cbc:TaxAmount", "1.59"),
            ("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "33.34"),
            ("cac:LegalMonetaryTotal/cbc:PayableAmount", "33.34"),
        ]:
            self.assertEqual(advance_xml.findtext(xpath, namespaces=NS), expected)
        original = advance.l10n_ae_transmission_id
        original_fields = [
            "payload",
            "xml_content",
            "idempotency_key",
            "payload_hash",
            "xml_hash",
            "revision",
            "create_date",
            "write_date",
        ]
        original_evidence = original.read(original_fields)
        operator_env["account.payment.register"].with_context(
            active_model="account.move", active_ids=advance.ids
        ).create(
            {
                "journal_id": self.company_data["default_journal_bank"].id,
                "amount": 33.34,
            }
        ).action_create_payments()
        self.assertEqual(advance.amount_residual, 0)
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "currency_id",
            "quantity",
            "price_unit",
            "discount",
            "tax_ids",
            "name",
            "create_date",
            "write_date",
        ]
        advance_ledger = advance.line_ids.read(ledger_fields)
        operator_env["sale.advance.payment.inv"].create(
            {
                "sale_order_ids": [Command.set(order.ids)],
                "advance_payment_method": "delivered",
                "deduct_down_payments": True,
            }
        ).create_invoices()
        final = order.invoice_ids - advance
        self.assertEqual(len(final), 1)
        final.write(
            {"invoice_date": "2019-01-02", "l10n_ae_binding_id": self.binding.id}
        )
        final.action_post()
        self.assertEqual(
            (
                final.amount_untaxed,
                final.amount_tax,
                final.amount_total,
                final.amount_residual,
            ),
            (68.25, 3.41, 71.66, 71.66),
        )
        advance_after_post = advance.line_ids.read(ledger_fields)
        for before, after in zip(advance_ledger, advance_after_post, strict=True):
            self.assertEqual(
                {key: value for key, value in before.items() if key != "write_date"},
                {key: value for key, value in after.items() if key != "write_date"},
            )
        final_ledger = final.line_ids.read(ledger_fields)
        final.action_l10n_ae_prepare()
        report = final.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["xsd_valid"], report)
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        xml = etree.fromstring(final.l10n_ae_transmission_id.xml_content.encode())
        for xpath, expected in [
            ("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", "68.25"),
            ("cac:TaxTotal/cbc:TaxAmount", "3.41"),
            ("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "71.66"),
            ("cac:LegalMonetaryTotal/cbc:PayableAmount", "71.66"),
            ("cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID", advance.name),
            (
                "cac:BillingReference/cac:InvoiceDocumentReference/cbc:IssueDate",
                "2019-01-01",
            ),
        ]:
            self.assertEqual(xml.findtext(xpath, namespaces=NS), expected)
        self.assertEqual(
            json.loads(final.l10n_ae_transmission_id.payload)["taxed_advances"],
            [
                {
                    "source_move_id": advance.id,
                    "number": advance.name,
                    "issue_date": "2019-01-01",
                    "consumed_net": "31.75",
                    "consumed_vat": "1.59",
                }
            ],
        )
        self.assertEqual(
            sum(
                final.line_ids.filtered(
                    lambda line: line.account_id == self.advance_account
                ).mapped("balance")
            ),
            31.75,
        )
        for move in (advance, final):
            before = move.l10n_ae_transmission_id.read(original_fields)
            move.action_l10n_ae_prepare()
            self.assertEqual(
                move.preview_l10n_ae_pint(), move.l10n_ae_transmission_id.xml_content
            )
            self.assertEqual(move.l10n_ae_transmission_id.read(original_fields), before)
            issue = move.l10n_ae_transmission_id
            self.assertFalse(
                issue.attempt_ids
                or issue.event_ids
                or issue.remote_id
                or issue.validation_receipt
            )
        self.assertEqual(original.read(original_fields), original_evidence)
        self.assertEqual(advance.line_ids.read(ledger_fields), advance_after_post)
        self.assertEqual(final.line_ids.read(ledger_fields), final_ledger)
        self.assertEqual(advance.amount_untaxed + final.amount_untaxed, 100)
        self.assertEqual(advance.amount_tax + final.amount_tax, 5)
        self.assertEqual(advance.amount_total + final.amount_total, 105)

    def test_frozen_advance_and_final_preserve_their_sales_links(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner_a.id,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product_a.id,
                            "product_uom_qty": 1,
                            "price_unit": 10000,
                            "tax_ids": [Command.set(self.tax_sale_a.ids)],
                        }
                    )
                ],
            }
        )
        order.action_confirm()
        advance = self._create_invoice(order, "percentage", 10)
        advance.action_post()
        advance.action_l10n_ae_prepare()
        final = self._create_invoice(order, "delivered")
        final.action_post()
        final.action_l10n_ae_prepare()
        for invoice in (advance, final):
            line = invoice.invoice_line_ids.filtered("is_downpayment")
            payload = invoice.l10n_ae_transmission_id.payload
            xml = invoice.preview_l10n_ae_pint()
            original_links = line.sale_line_ids.ids
            for vals in (
                {"sale_line_ids": [Command.clear()]},
                {"is_downpayment": False},
            ):
                with (
                    self.subTest(invoice=invoice.id, field=next(iter(vals))),
                    self.cr.savepoint(),
                    self.assertRaisesRegex(UserError, "frozen UAE.*line"),
                ):
                    line.write(vals)
            self.assertEqual(line.sale_line_ids.ids, original_links)
            self.assertTrue(line.is_downpayment)
            self.assertEqual(invoice.l10n_ae_transmission_id.payload, payload)
            self.assertEqual(invoice.preview_l10n_ae_pint(), xml)
        self.assertEqual((advance.amount_total, final.amount_total), (1050, 9450))

    def test_final_balance_rejects_unposted_advance_before_accounting_post(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner_a.id,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product_a.id,
                            "product_uom_qty": 1,
                            "price_unit": 10000,
                            "tax_ids": [Command.set(self.tax_sale_a.ids)],
                        }
                    )
                ],
            }
        )
        order.action_confirm()
        advance = self._create_invoice(order, "percentage", 10)
        final = self._create_invoice(order, "delivered")
        self.assertEqual(advance.state, "draft")
        self.assertEqual(final.amount_total, 9450)
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": self.binding.id,
            }
        )
        with self.assertRaisesRegex(UserError, "posted.*advance|advance.*posted"):
            final.action_post()
        self.assertEqual(final.state, "draft")
        self.assertFalse(final.l10n_ae_fiscal_uuid)
        self.assertFalse(final.l10n_ae_transmission_id)

    def test_final_balance_rejects_an_advance_for_another_commercial_customer(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner_a.id,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product_a.id,
                            "product_uom_qty": 1,
                            "price_unit": 10000,
                            "tax_ids": [Command.set(self.tax_sale_a.ids)],
                        }
                    )
                ],
            }
        )
        order.action_confirm()
        advance = self._create_invoice(order, "percentage", 10)
        advance.action_post()
        advance.action_l10n_ae_prepare()
        final = self._create_invoice(order, "delivered")
        final.partner_id = self.partner_a.copy({"name": "Unrelated synthetic customer"})
        with self.assertRaisesRegex(UserError, "same commercial customer"):
            final.action_l10n_ae_validate()
        self.assertEqual(final.state, "draft")
        self.assertFalse(final.l10n_ae_transmission_id)

    def test_final_balance_rejects_deduction_above_the_original_taxed_advance(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner_a.id,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product_a.id,
                            "product_uom_qty": 1,
                            "price_unit": 10000,
                            "tax_ids": [Command.set(self.tax_sale_a.ids)],
                        }
                    )
                ],
            }
        )
        order.action_confirm()
        advance = self._create_invoice(order, "percentage", 10)
        advance.action_post()
        advance.action_l10n_ae_prepare()
        final = self._create_invoice(order, "delivered")
        final.invoice_line_ids.filtered("is_downpayment").quantity = -2
        with self.assertRaisesRegex(UserError, "exceed.*advance|advance.*exceed"):
            final.action_l10n_ae_validate()
        self.assertEqual(final.state, "draft")
        self.assertEqual((final.amount_untaxed, final.amount_tax), (8000, 400))
        self.assertFalse(final.l10n_ae_transmission_id)

    def test_final_balance_rejects_an_advance_in_another_document_currency(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner_a.id,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product_a.id,
                            "product_uom_qty": 1,
                            "price_unit": 10000,
                            "tax_ids": [Command.set(self.tax_sale_a.ids)],
                        }
                    )
                ],
            }
        )
        order.action_confirm()
        advance = self._create_invoice(order, "percentage", 10)
        advance.action_post()
        advance.action_l10n_ae_prepare()
        original = advance.l10n_ae_transmission_id
        original_xml, original_payload = original.xml_content, original.payload
        final = self._create_invoice(order, "delivered")
        final.write(
            {
                "currency_id": self.env.ref("base.USD").id,
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": fields.Date.today(),
                "l10n_ae_tax_exchange_source": "Synthetic reviewed quote",
            }
        )
        with self.assertRaisesRegex(UserError, "same.*currency"):
            final.action_l10n_ae_validate()
        self.assertEqual(final.state, "draft")
        self.assertFalse(final.l10n_ae_fiscal_uuid)
        self.assertFalse(final.l10n_ae_transmission_id)
        self.assertEqual(
            (original.xml_content, original.payload), (original_xml, original_payload)
        )

    def test_final_balance_rejects_a_different_advance_document_category(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner_a.id,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product_a.id,
                            "product_uom_qty": 1,
                            "price_unit": 10000,
                            "tax_ids": [Command.set(self.tax_sale_a.ids)],
                        }
                    )
                ],
            }
        )
        order.action_confirm()
        advance = self._create_invoice(order, "percentage", 10)
        advance.action_post()
        advance.action_l10n_ae_prepare()
        original = advance.l10n_ae_transmission_id
        original_xml, original_payload = original.xml_content, original.payload
        final = self._create_invoice(order, "delivered")
        outside_scope = self.tax_sale_a.copy(
            {
                "name": "Synthetic Advance Outside Scope",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        final.l10n_ae_document_category = "commercial"
        final.invoice_line_ids.tax_ids = outside_scope
        with self.assertRaisesRegex(UserError, "same.*category"):
            final.action_l10n_ae_validate()
        self.assertEqual(final.state, "draft")
        self.assertEqual((final.amount_untaxed, final.amount_tax), (9000, 0))
        self.assertFalse(final.l10n_ae_fiscal_uuid)
        self.assertFalse(final.l10n_ae_transmission_id)
        self.assertEqual(
            (original.xml_content, original.payload), (original_xml, original_payload)
        )

    def test_final_balance_rejects_an_advance_from_another_legal_company(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner_a.id,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product_a.id,
                            "product_uom_qty": 1,
                            "price_unit": 10000,
                            "tax_ids": [Command.set(self.tax_sale_a.ids)],
                        }
                    )
                ],
            }
        )
        order.action_confirm()
        advance = self._create_invoice(order, "percentage", 10)
        advance.action_post()
        advance.action_l10n_ae_prepare()
        original = advance.l10n_ae_transmission_id
        original_xml, original_payload = original.xml_content, original.payload
        other_data = self.setup_other_company(name="Synthetic independent UAE company")
        other = other_data["company"]
        other.write({"l10n_ae_tin": "1000000011", "l10n_ae_tin_confirmed": True})
        other.partner_id.state_id = self.env.ref("base.state_ae_du")
        source_line = advance.invoice_line_ids.filtered("is_downpayment")
        # Public ORM accepts a Sales relation without a company check on that field.
        final = (
            self.env["account.move"]
            .with_company(other)
            .create(
                {
                    "company_id": other.id,
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "invoice_date": fields.Date.today(),
                    "journal_id": other_data["default_journal_sale"].id,
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Synthetic independent service",
                                "quantity": 1,
                                "price_unit": 10000,
                                "account_id": other_data["default_account_revenue"].id,
                                "tax_ids": [Command.set(other.account_sale_tax_id.ids)],
                            }
                        ),
                        Command.create(
                            {
                                "name": "Synthetic foreign-company advance deduction",
                                "quantity": -1,
                                "price_unit": 1000,
                                "is_downpayment": True,
                                "sale_line_ids": [
                                    Command.set(source_line.sale_line_ids.ids)
                                ],
                                "account_id": other_data["default_account_revenue"].id,
                                "tax_ids": [Command.set(other.account_sale_tax_id.ids)],
                            }
                        ),
                    ],
                }
            )
        )
        with self.assertRaisesRegex(UserError, "same.*company"):
            final.action_l10n_ae_validate()
        self.assertEqual(final.state, "draft")
        self.assertFalse(final.l10n_ae_fiscal_uuid)
        self.assertFalse(final.l10n_ae_transmission_id)
        self.assertEqual(
            (original.xml_content, original.payload), (original_xml, original_payload)
        )

    def test_second_final_cannot_reuse_an_already_frozen_advance_deduction(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner_a.id,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product_a.id,
                            "product_uom_qty": 1,
                            "price_unit": 10000,
                            "tax_ids": [Command.set(self.tax_sale_a.ids)],
                        }
                    )
                ],
            }
        )
        order.action_confirm()
        advance = self._create_invoice(order, "percentage", 10)
        advance.action_post()
        advance.action_l10n_ae_prepare()
        final = self._create_invoice(order, "delivered")
        final.action_post()
        final.action_l10n_ae_prepare()
        first = final.l10n_ae_transmission_id
        first_xml, first_payload = first.xml_content, first.payload
        with self.assertRaisesRegex(UserError, "immutable"):
            first.sudo().write({"l10n_ae_advance_source_ids": [Command.clear()]})
        duplicate = final.with_context(include_business_fields=True).copy(
            {"invoice_date": fields.Date.today()}
        )
        self.assertTrue(duplicate.invoice_line_ids.filtered("is_downpayment"))
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": self.binding.id,
            }
        )
        with self.assertRaisesRegex(UserError, "already.*deduct|remaining.*advance"):
            duplicate.action_post()
        self.assertEqual(duplicate.state, "draft")
        self.assertFalse(duplicate.l10n_ae_fiscal_uuid)
        self.assertFalse(duplicate.l10n_ae_transmission_id)
        self.assertEqual((first.xml_content, first.payload), (first_xml, first_payload))
        self.assertEqual((advance.amount_total, final.amount_total), (1050, 9450))

    def test_two_partial_finals_can_consume_exactly_one_advance(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner_a.id,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product_a.id,
                            "product_uom_qty": 1,
                            "price_unit": 10000,
                            "tax_ids": [Command.set(self.tax_sale_a.ids)],
                        }
                    )
                ],
            }
        )
        order.action_confirm()
        advance = self._create_invoice(order, "percentage", 10)
        advance.action_post()
        advance.action_l10n_ae_prepare()
        first = self._create_invoice(order, "delivered")
        first.invoice_line_ids.filtered(
            lambda line: line.display_type == "product" and not line.is_downpayment
        ).quantity = 0.5
        first.invoice_line_ids.filtered("is_downpayment").quantity = -0.4
        first.action_post()
        first.action_l10n_ae_prepare()
        first_xml = first.preview_l10n_ae_pint()
        second = first.with_context(include_business_fields=True).copy(
            {
                "invoice_date": fields.Date.today(),
                "l10n_ae_binding_id": self.binding.id,
            }
        )
        second.invoice_line_ids.filtered("is_downpayment").quantity = -0.6
        second.action_post()
        second.action_l10n_ae_prepare()
        self.assertEqual(
            (first.amount_untaxed, first.amount_tax, first.amount_total),
            (4600, 230, 4830),
        )
        self.assertEqual(
            (second.amount_untaxed, second.amount_tax, second.amount_total),
            (4400, 220, 4620),
        )
        for invoice, net, vat in (
            (first, "400.00", "20.00"),
            (second, "600.00", "30.00"),
        ):
            reference = json.loads(invoice.l10n_ae_transmission_id.payload)[
                "taxed_advances"
            ]
            self.assertEqual(len(reference), 1)
            self.assertEqual(
                (reference[0]["consumed_net"], reference[0]["consumed_vat"]), (net, vat)
            )
            report = invoice.validate_l10n_ae_pint()
            self.assertTrue(report["valid"], report)
        self.assertEqual(first.preview_l10n_ae_pint(), first_xml)
        self.assertNotEqual(first.l10n_ae_fiscal_uuid, second.l10n_ae_fiscal_uuid)
        self.assertEqual(
            advance.amount_total + first.amount_total + second.amount_total, 10500
        )

    def test_posted_final_consumes_advance_before_fiscal_preparation(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner_a.id,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product_a.id,
                            "product_uom_qty": 1,
                            "price_unit": 10000,
                            "tax_ids": [Command.set(self.tax_sale_a.ids)],
                        }
                    )
                ],
            }
        )
        order.action_confirm()
        advance = self._create_invoice(order, "percentage", 10)
        advance.action_post()
        advance.action_l10n_ae_prepare()
        first = self._create_invoice(order, "delivered")
        first.action_post()
        self.assertFalse(first.l10n_ae_transmission_id)
        duplicate = first.with_context(include_business_fields=True).copy(
            {
                "invoice_date": fields.Date.today(),
            }
        )
        with self.assertRaisesRegex(UserError, "already.*deduct|remaining.*advance"):
            duplicate.action_l10n_ae_validate()
        self.assertEqual(duplicate.state, "draft")
        self.assertFalse(duplicate.l10n_ae_fiscal_uuid)
        self.assertFalse(duplicate.l10n_ae_transmission_id)
        self.assertEqual((first.state, first.amount_total), ("posted", 9450))
        self.assertFalse(first.l10n_ae_transmission_id)

    def test_replaced_final_without_deduction_releases_advance_reset(self):
        # Independent amounts: 10000 + VAT500, advance1000 + VAT50,
        # deducted final9000 + VAT450. Removing the deduction restores10500.
        self.env.company.l10n_ae_edi_auto_prepare = False
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner_a.id,
                "order_line": [
                    Command.create(
                        {
                            "product_id": self.product_a.id,
                            "product_uom_qty": 1,
                            "price_unit": 10000,
                            "tax_ids": [Command.set(self.tax_sale_a.ids)],
                        }
                    )
                ],
            }
        )
        order.action_confirm()
        advance = self._create_invoice(order, "percentage", 10).with_user(
            self.simple_accountman
        )
        final = self.env["account.move"]
        self.assertFalse(advance.env.su)
        advance.action_post()
        advance.action_l10n_ae_prepare()
        final = self._create_invoice(order, "delivered").with_user(
            self.simple_accountman
        )
        self.assertFalse(final.env.su)
        final.action_post()
        final.action_l10n_ae_prepare()
        original = final.l10n_ae_transmission_id
        retained_fields = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
            "l10n_ae_advance_source_ids",
        ]
        original_frozen = original.read(retained_fields)
        advance_issue = advance.l10n_ae_transmission_id
        advance_frozen = advance_issue.read(retained_fields)
        self.assertEqual((advance.amount_total, final.amount_total), (1050, 9450))
        self.assertEqual(original.l10n_ae_advance_source_ids, advance)
        with self.assertRaisesRegex(UserError, "advance has already been deducted"):
            advance.button_draft()
        self.assertEqual(advance.state, "posted")
        self.assertFalse(advance.l10n_ae_correction_id)

        # An editing correction still reserves its original frozen deduction.
        final.button_draft()
        first_correction = final.l10n_ae_correction_id
        self.assertEqual(first_correction.state, "editing")
        with self.assertRaisesRegex(UserError, "advance has already been deducted"):
            advance.button_draft()
        final.action_post()
        final.action_l10n_ae_prepare()
        retained = final.l10n_ae_transmission_id
        self.assertEqual(first_correction.replacement_transmission_id, retained)
        self.assertEqual(retained.l10n_ae_advance_source_ids, advance)
        self.assertEqual(final.amount_total, 9450)
        with self.assertRaisesRegex(UserError, "advance has already been deducted"):
            advance.button_draft()

        # Only the effective replacement removes the consumption. Historical
        # references must remain immutable instead of being deleted to unlock.
        retained_frozen = retained.read(retained_fields)
        final.button_draft()
        second_correction = final.l10n_ae_correction_id
        final.invoice_line_ids.filtered(
            lambda line: line.is_downpayment and line.quantity < 0
        ).unlink()
        final.action_post()
        final.action_l10n_ae_prepare()
        replacement = final.l10n_ae_transmission_id
        self.assertEqual(second_correction.replacement_transmission_id, replacement)
        self.assertEqual(second_correction.state, "completed")
        self.assertFalse(replacement.l10n_ae_advance_source_ids)
        self.assertEqual(json.loads(replacement.payload)["taxed_advances"], [])
        self.assertEqual(
            (final.amount_untaxed, final.amount_tax, final.amount_total),
            (10000, 500, 10500),
        )
        final_ledger = final.line_ids.sorted("id").read(
            ["account_id", "debit", "credit", "balance", "write_date"]
        )
        advance.button_draft()
        self.assertEqual(advance.state, "draft")
        self.assertTrue(advance.l10n_ae_correction_id)
        self.assertEqual(final.state, "posted")
        self.assertEqual(
            final.line_ids.sorted("id").read(
                ["account_id", "debit", "credit", "balance", "write_date"]
            ),
            final_ledger,
        )
        self.assertEqual(original.read(retained_fields), original_frozen)
        self.assertEqual(retained.read(retained_fields), retained_frozen)
        self.assertEqual(advance_issue.read(retained_fields), advance_frozen)
