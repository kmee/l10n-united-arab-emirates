# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json

from lxml import etree

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

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
        cls.product_a.write({"type": "service", "invoice_policy": "order"})
        cls.binding = cls.env["l10n_ae.edi.binding"].create(
            {
                "name": "Sales advance simulation",
                "fiscal_uuid_policy": "controlled_local",
            }
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
