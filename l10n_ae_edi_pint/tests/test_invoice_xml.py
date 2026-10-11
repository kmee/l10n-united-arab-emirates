# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import io
import json
import logging
import os
import sys
import uuid
import zipfile
from decimal import Decimal
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.l10n_ae_edi.tests.common import (
    CONTROLLED_CAPABILITIES,
)

from ..tools.validation import OfflinePintValidator
from .common import NS, UaePintTestCommon, requires_normative_resources

_logger = logging.getLogger(__name__)


@tagged("post_install", "-at_install")
class TestUaeInvoiceXml(UaePintTestCommon):
    @requires_normative_resources("1.0.4")
    def test_explicit_period_wins_without_merging_native_dates_or_freezing_them(self):
        if not {"deferred_start_date", "deferred_end_date"}.issubset(
            self.env["account.move.line"]._fields
        ):
            self.skipTest("Native deferred-date fields are not installed.")
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": False,
                "generate_deferred_revenue_entries_method": "manual",
            }
        )
        binding = self._reviewed_binding(
            {
                "name": "Synthetic explicit period precedence",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_explicit_period_wins_without_"
            "merging_native_dates_or_freezing_them.",
        )
        # Explicit dates are independent reviewed facts. Do not combine one
        # explicit endpoint with the accounting recognition period in January.
        cases = (
            (True, "2026-02-01", False, "2026-02-01", None),
            (True, False, "2026-02-28", None, "2026-02-28"),
            (True, "2026-02-01", "2026-02-28", "2026-02-01", "2026-02-28"),
            (False, False, False, None, None),
        )
        for summary, start, end, expected_start, expected_end in cases:
            with self.subTest(summary=summary, start=start, end=end):
                invoice = self.init_invoice(
                    "out_invoice", amounts=[100], taxes=self.tax_sale_a
                ).with_user(self.simple_accountman)
                invoice.write(
                    {
                        "invoice_date": "2026-01-31",
                        "l10n_ae_summary": summary,
                        "l10n_ae_invoicing_period_start": start,
                        "l10n_ae_invoicing_period_end": end,
                        "l10n_ae_billing_frequency_code": False,
                        "l10n_ae_binding_id": binding.id,
                        "preferred_payment_method_line_id": False,
                        "partner_bank_id": False,
                    }
                )
                line = invoice.invoice_line_ids
                line.write(
                    {
                        "deferred_start_date": "2026-01-01",
                        "deferred_end_date": "2026-01-31",
                    }
                )
                invoice.action_post()
                invoice.action_l10n_ae_validate()
                invoice.action_l10n_ae_prepare()
                issue = invoice.l10n_ae_transmission_id
                payload = json.loads(issue.payload)
                tree = etree.fromstring(issue.xml_content.encode())
                period = tree.find("cac:InvoicePeriod", NS)
                if summary:
                    self.assertIsNotNone(period)
                    self.assertEqual(
                        period.findtext("cbc:StartDate", namespaces=NS), expected_start
                    )
                    self.assertEqual(
                        period.findtext("cbc:EndDate", namespaces=NS), expected_end
                    )
                    self.assertEqual(
                        payload["invoicing_period"],
                        {"start_date": expected_start, "end_date": expected_end},
                    )
                else:
                    self.assertIsNone(period)
                    self.assertNotIn("invoicing_period", payload)
                self.assertNotIn("invoicing_period_origin", payload)
                self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
                self.assertEqual(
                    (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
                    (100, 5, 105),
                )
                immutable = [
                    "payload",
                    "payload_hash",
                    "xml_content",
                    "xml_hash",
                    "idempotency_key",
                ]
                frozen = issue.read(immutable)
                # These deferrals did not supply fiscal header dates. Ordinary
                # accounting scheduling remains editable under native policy.
                line.write({"deferred_end_date": "2026-02-28"})
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
                ledger_after_native_edit = invoice.line_ids.sorted("id").read(
                    ledger_fields
                )
                invoice.action_l10n_ae_prepare()
                self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
                self.assertEqual(issue.read(immutable), frozen)
                self.assertEqual(
                    invoice.line_ids.sorted("id").read(ledger_fields),
                    ledger_after_native_edit,
                )
                self.assertFalse(issue.attempt_ids or issue.event_ids)

    @requires_normative_resources("1.0.4")
    def test_other_eight_reviewed_term_codes_render_valid_code_only_periods(self):
        # Published IBR-005-AE literals, complementing the Monthly/Other
        # journeys. These are ordinary AED380 representations, not all flows.
        codes = ("DLY", "WKY", "Q15", "Q45", "Q60", "QTR", "YRL", "HYR")
        self.env.company.l10n_ae_edi_auto_prepare = False
        binding = self._reviewed_binding(
            {
                "name": "Synthetic published billing codes",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_other_eight_reviewed_term_cod"
            "es_render_valid_code_only_periods.",
        )
        for code in codes:
            with self.subTest(code=code):
                term = self.env["account.payment.term"].create(
                    {
                        "name": "Synthetic reviewed cadence " + code,
                        "company_id": self.env.company.id,
                        "l10n_ae_billing_frequency_code": code,
                        "line_ids": [
                            (
                                0,
                                0,
                                {
                                    "value": "percent",
                                    "value_amount": 100,
                                    "nb_days": 60,
                                },
                            )
                        ],
                    }
                )
                invoice = (
                    self.env["account.move"]
                    .with_user(self.simple_accountman)
                    .create(
                        {
                            "move_type": "out_invoice",
                            "partner_id": self.partner_a.id,
                            "invoice_date": "2026-01-31",
                            "invoice_payment_term_id": term.id,
                            "l10n_ae_binding_id": binding.id,
                            "invoice_line_ids": [
                                (
                                    0,
                                    0,
                                    {
                                        "name": "Synthetic reviewed cadence supply",
                                        "product_id": self.product_a.id,
                                        "price_unit": 100,
                                        "tax_ids": [(6, 0, self.tax_sale_a.ids)],
                                    },
                                )
                            ],
                        }
                    )
                )
                self.assertFalse(invoice.env.su)
                self.assertEqual(invoice.l10n_ae_billing_frequency_code, code)
                invoice.action_post()
                self.assertEqual(str(invoice.invoice_date_due), "2026-04-01")
                self.assertEqual(
                    (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
                    (100, 5, 105),
                )
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
                ledger = invoice.line_ids.sorted("id").read(ledger_fields)
                invoice.action_l10n_ae_prepare()
                issue = invoice.l10n_ae_transmission_id
                tree = etree.fromstring(issue.xml_content.encode())
                periods = tree.findall("cac:InvoicePeriod", namespaces=NS)
                self.assertEqual(len(periods), 1)
                self.assertEqual(
                    periods[0].findtext("cbc:DescriptionCode", namespaces=NS), code
                )
                self.assertIsNone(periods[0].find("cbc:StartDate", namespaces=NS))
                self.assertIsNone(periods[0].find("cbc:EndDate", namespaces=NS))
                report = invoice.validate_l10n_ae_pint()
                self.assertTrue(report["valid"], report)
                immutable = [
                    "payload",
                    "xml_content",
                    "payload_hash",
                    "xml_hash",
                    "idempotency_key",
                ]
                original = issue.read(immutable)
                term.l10n_ae_billing_frequency_code = False
                self.assertEqual(invoice.l10n_ae_billing_frequency_code, code)
                invoice.action_l10n_ae_prepare()
                self.assertEqual(issue.read(immutable), original)
                self.assertEqual(
                    invoice.line_ids.sorted("id").read(ledger_fields), ledger
                )
                self.assertFalse(issue.attempt_ids or issue.event_ids)

    @requires_normative_resources("1.0.4")
    def test_api_invoice_defaults_reviewed_monthly_term_without_supply_dates(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        term = self.env["account.payment.term"].create(
            {
                "name": "Synthetic API reviewed monthly sixty-day term",
                "company_id": self.env.company.id,
                "l10n_ae_billing_frequency_code": "MTH",
                "line_ids": [
                    (0, 0, {"value": "percent", "value_amount": 100, "nb_days": 60})
                ],
            }
        )
        binding = self._reviewed_binding(
            {
                "name": "Synthetic API monthly",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_api_invoice_defaults_reviewed"
            "_monthly_term_without_supply_dates.",
        )
        invoice = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_company(self.env.company)
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner_a.id,
                    "invoice_date": "2026-01-31",
                    "invoice_payment_term_id": term.id,
                    "l10n_ae_binding_id": binding.id,
                    "invoice_line_ids": [
                        (
                            0,
                            0,
                            {
                                "product_id": self.product_a.id,
                                "name": "Synthetic reviewed monthly supply",
                                "price_unit": 100,
                                "tax_ids": [(6, 0, self.tax_sale_a.ids)],
                            },
                        )
                    ],
                }
            )
        )
        self.assertFalse(invoice.env.su)
        self.assertEqual(invoice.l10n_ae_billing_frequency_code, "MTH")
        self.assertFalse(invoice.l10n_ae_invoicing_period_start)
        self.assertFalse(invoice.l10n_ae_invoicing_period_end)
        self.assertEqual(str(invoice.invoice_date_due), "2026-04-01")
        invoice.action_post()
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
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
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        tree = etree.fromstring(issue.xml_content.encode())
        periods = tree.findall("cac:InvoicePeriod", namespaces=NS)
        self.assertEqual(len(periods), 1)
        self.assertEqual(
            periods[0].findtext("cbc:DescriptionCode", namespaces=NS), "MTH"
        )
        self.assertIsNone(periods[0].find("cbc:StartDate", namespaces=NS))
        self.assertIsNone(periods[0].find("cbc:EndDate", namespaces=NS))
        self.assertEqual(tree.findtext("cbc:DueDate", namespaces=NS), "2026-04-01")
        self.assertEqual(
            json.loads(issue.payload)["invoicing_period"],
            {"start_date": None, "end_date": None, "billing_frequency_code": "MTH"},
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        immutable = [
            "payload",
            "xml_content",
            "payload_hash",
            "xml_hash",
            "idempotency_key",
        ]
        originals = issue.read(immutable)
        term.l10n_ae_billing_frequency_code = "WKY"
        self.assertEqual(invoice.l10n_ae_billing_frequency_code, "MTH")
        invoice.action_l10n_ae_prepare()
        self.assertEqual(issue.read(immutable), originals)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertFalse(issue.attempt_ids or issue.event_ids)

    @requires_normative_resources("1.0.4")
    def test_other_billing_frequency_reviews_explanation_without_replacing_terms(self):
        binding = self._reviewed_binding(
            {
                "name": "Synthetic other billing cadence",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_other_billing_frequency_revie"
            "ws_explanation_without_replacing_terms.",
        )
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": binding.id,
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        with Form(invoice) as form:
            form.invoice_payment_term_id = self.env["account.payment.term"]
            form.preferred_payment_method_line_id = self.env[
                "account.payment.method.line"
            ]
            form.partner_bank_id = self.env["res.partner.bank"]
            form.narration = "<p>Synthetic ordinary terms.</p>"
            form.l10n_ae_billing_frequency_code = "OTH"
            form.l10n_ae_binding_id = binding
        self.assertFalse(invoice.l10n_ae_invoicing_period_start)
        self.assertFalse(invoice.l10n_ae_invoicing_period_end)
        with self.assertRaisesRegex(UserError, "IBR-160-AE"):
            invoice.action_l10n_ae_validate()
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.l10n_ae_preparation_state, "review")
        self.assertIn("IBR-160-AE", invoice.l10n_ae_preparation_error)
        self.assertFalse(invoice.l10n_ae_fiscal_uuid or invoice.l10n_ae_transmission_id)
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        with self.assertRaisesRegex(UserError, "IBR-160-AE"):
            invoice.action_l10n_ae_prepare()
        with Form(invoice) as form:
            form.l10n_ae_billing_frequency_note = (
                "Synthetic cadence: tenth business day."
            )
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
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        invoice.action_l10n_ae_validate()
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        tree = etree.fromstring(issue.xml_content.encode())
        periods = tree.findall("cac:InvoicePeriod", namespaces=NS)
        self.assertEqual(len(periods), 1)
        self.assertEqual(
            periods[0].findtext("cbc:DescriptionCode", namespaces=NS), "OTH"
        )
        self.assertIsNone(periods[0].find("cbc:StartDate", namespaces=NS))
        self.assertIsNone(periods[0].find("cbc:EndDate", namespaces=NS))
        self.assertIsNone(periods[0].find("cbc:Description", namespaces=NS))
        notes = tree.findall("cbc:Note", namespaces=NS)
        self.assertEqual(len(notes), 1)
        self.assertEqual(
            [line for line in notes[0].text.splitlines() if line.strip()],
            [
                "Synthetic ordinary terms.",
                "Synthetic cadence: tenth business day.",
            ],
        )
        self.assertEqual(
            json.loads(issue.payload)["invoicing_period"],
            {
                "start_date": None,
                "end_date": None,
                "billing_frequency_code": "OTH",
                "other_frequency_note": "Synthetic cadence: tenth business day.",
            },
        )
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        immutable = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
        ]
        frozen = issue.read(immutable)
        with self.assertRaisesRegex(UserError, "frozen UAE"):
            invoice.write({"l10n_ae_billing_frequency_note": "Changed cadence"})
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), frozen[0]["xml_content"])
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        self.assertEqual(issue.read(immutable), frozen)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertFalse(issue.attempt_ids or issue.event_ids)

    @requires_normative_resources("1.0.4")
    def test_reviewed_monthly_term_preserves_supply_period_and_native_due_date(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        with Form(
            self.env["account.payment.term"]
            .with_user(self.simple_accountman)
            .with_context(default_company_id=self.env.company.id)
        ) as form:
            form.name = "Synthetic monthly billing with sixty-day payment"
            with form.line_ids.edit(0) as line:
                line.nb_days = 60
                line.delay_type = "days_after"
            form.l10n_ae_billing_frequency_code = "MTH"
            term = form.save()
        self.assertEqual(term.company_id, self.env.company)
        binding = self._reviewed_binding(
            {
                "name": "Synthetic monthly billing frequency",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_reviewed_monthly_term_preserv"
            "es_supply_period_and_native_due_date.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        with Form(invoice) as form:
            form.invoice_date = "2026-01-31"
            form.invoice_payment_term_id = term
            form.preferred_payment_method_line_id = self.env[
                "account.payment.method.line"
            ]
            form.partner_bank_id = self.env["res.partner.bank"]
            form.l10n_ae_summary = True
            form.l10n_ae_invoicing_period_start = "2026-01-01"
            form.l10n_ae_invoicing_period_end = "2026-01-31"
            form.l10n_ae_binding_id = binding
            self.assertEqual(form.l10n_ae_billing_frequency_code, "MTH")
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(str(invoice.invoice_date_due), "2026-04-01")
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
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
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        invoice.action_l10n_ae_validate()
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        tree = etree.fromstring(issue.xml_content.encode())
        periods = tree.findall("cac:InvoicePeriod", namespaces=NS)
        self.assertEqual(len(periods), 1)
        self.assertEqual(
            periods[0].findtext("cbc:StartDate", namespaces=NS), "2026-01-01"
        )
        self.assertEqual(
            periods[0].findtext("cbc:EndDate", namespaces=NS), "2026-01-31"
        )
        self.assertEqual(
            periods[0].findtext("cbc:DescriptionCode", namespaces=NS), "MTH"
        )
        self.assertIsNone(periods[0].find("cbc:Description", namespaces=NS))
        self.assertEqual(tree.findtext("cbc:DueDate", namespaces=NS), "2026-04-01")
        self.assertEqual(
            json.loads(issue.payload)["invoicing_period"],
            {
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
                "billing_frequency_code": "MTH",
            },
        )
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        immutable = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
        ]
        frozen = issue.read(immutable)
        with Form(term.with_user(self.simple_accountman)) as form:
            form.l10n_ae_billing_frequency_code = "WKY"
        self.assertEqual(invoice.l10n_ae_billing_frequency_code, "MTH")
        with self.assertRaisesRegex(UserError, "frozen UAE"):
            invoice.write({"l10n_ae_billing_frequency_code": "WKY"})
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), frozen[0]["xml_content"])
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        self.assertEqual(issue.read(immutable), frozen)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertFalse(issue.attempt_ids or issue.event_ids)

    @requires_normative_resources("1.0.4")
    def test_unclassified_native_method_reviews_then_freezes_online_service(self):
        journal = self.company_data["default_journal_credit"]
        method = journal.inbound_payment_method_line_ids.filtered(
            lambda line: line.code == "manual"
        )[:1]
        self.assertTrue(method)
        self.assertFalse(method.l10n_ae_edi_payment_means_code)
        binding = self._reviewed_binding(
            {
                "name": "Synthetic unclassified native instrument",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_unclassified_native_method_re"
            "views_then_freezes_online_service.",
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
            form.partner_bank_id = self.env["res.partner.bank"]
            form.payment_reference = "SYNTH-ONLINE-REVIEW-2026"
            form.l10n_ae_binding_id = binding
        with self.assertRaisesRegex(UserError, "UAE Payment Instrument"):
            invoice.action_l10n_ae_validate()
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.l10n_ae_preparation_state, "review")
        self.assertIn("UAE Payment Instrument", invoice.l10n_ae_preparation_error)
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
        with self.assertRaisesRegex(UserError, "UAE Payment Instrument"):
            invoice.action_l10n_ae_prepare()
        index = journal.inbound_payment_method_line_ids.ids.index(method.id)
        with (
            Form(journal.with_user(self.simple_accountman)) as form,
            form.inbound_payment_method_line_ids.edit(index) as line,
        ):
            line.l10n_ae_edi_payment_means_code = "68"
        self.assertEqual(
            (method.code, method.payment_type, journal.type),
            ("manual", "inbound", "credit"),
        )
        invoice.action_l10n_ae_validate()
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        tree = etree.fromstring(issue.xml_content.encode())
        means = tree.findall("cac:PaymentMeans", namespaces=NS)
        self.assertEqual(len(means), 1)
        self.assertEqual(means[0].findtext("cbc:PaymentMeansCode", namespaces=NS), "68")
        self.assertEqual(
            means[0].findtext("cbc:PaymentID", namespaces=NS),
            "SYNTH-ONLINE-REVIEW-2026",
        )
        for group in ("PayeeFinancialAccount", "CardAccount", "PaymentMandate"):
            self.assertIsNone(means[0].find(f"cac:{group}", namespaces=NS))
        snapshot = json.loads(issue.payload)["payment_means"]
        self.assertEqual(snapshot["code"], "68")
        self.assertEqual(snapshot["configured_code"], "68")
        self.assertEqual(snapshot["method_line_id"], method.id)
        self.assertEqual(snapshot["method_code"], "manual")
        self.assertEqual(snapshot["journal_type"], "credit")
        self.assertIsNone(snapshot["account_identifier"])
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        immutable = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
        ]
        frozen = issue.read(immutable)
        with (
            Form(journal.with_user(self.simple_accountman)) as form,
            form.inbound_payment_method_line_ids.edit(index) as line,
        ):
            line.l10n_ae_edi_payment_means_code = False
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), frozen[0]["xml_content"])
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        self.assertEqual(issue.read(immutable), frozen)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertFalse(issue.attempt_ids or issue.event_ids)

    @requires_normative_resources("1.0.4")
    def test_reviewed_native_journal_cheque_instruction_preserves_original(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        journal = self.company_data["default_journal_bank"]
        method = journal.inbound_payment_method_line_ids.filtered(
            lambda line: line.code == "manual"
        )[:1]
        self.assertTrue(method)
        index = journal.inbound_payment_method_line_ids.ids.index(method.id)
        with (
            Form(journal.with_user(self.simple_accountman)) as form,
            form.inbound_payment_method_line_ids.edit(index) as line,
        ):
            line.l10n_ae_edi_payment_means_code = "20"
        self.assertEqual(method.code, "manual")
        self.assertEqual(method.payment_type, "inbound")
        binding = self._reviewed_binding(
            {
                "name": "Synthetic reviewed cheque instruction",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_reviewed_native_journal_chequ"
            "e_instruction_preserves_original.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        with Form(invoice) as form:
            form.preferred_payment_method_line_id = method
            form.partner_bank_id = self.env["res.partner.bank"]
            form.payment_reference = "SYNTH-CHEQUE-2026"
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
        self.assertEqual(means[0].findtext("cbc:PaymentMeansCode", namespaces=NS), "20")
        self.assertEqual(
            means[0].findtext("cbc:PaymentID", namespaces=NS), "SYNTH-CHEQUE-2026"
        )
        for group in ("PayeeFinancialAccount", "CardAccount", "PaymentMandate"):
            self.assertIsNone(means[0].find(f"cac:{group}", namespaces=NS))
        snapshot = json.loads(issue.payload)["payment_means"]
        self.assertEqual(snapshot["code"], "20")
        self.assertEqual(snapshot["configured_code"], "20")
        self.assertEqual(snapshot["name"], "Cheque")
        self.assertEqual(snapshot["method_line_id"], method.id)
        self.assertEqual(snapshot["method_code"], "manual")
        self.assertEqual(snapshot["journal_id"], journal.id)
        self.assertIsNone(snapshot["account_id"])
        self.assertIsNone(snapshot["account_identifier"])
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        immutable = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
        ]
        frozen = issue.read(immutable)
        with (
            Form(journal.with_user(self.simple_accountman)) as form,
            form.inbound_payment_method_line_ids.edit(index) as line,
        ):
            line.l10n_ae_edi_payment_means_code = "30"
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), frozen[0]["xml_content"])
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        self.assertEqual(issue.read(immutable), frozen)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertEqual(method.code, "manual")
        self.assertFalse(issue.attempt_ids or issue.event_ids)

    def test_check_uae_data_refuses_native_buyer_country_constraint(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        self.partner_a.country_id = False
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "tax_ids",
            "write_date",
        ]
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        with self.assertRaisesRegex(UserError, "IBR-011"):
            invoice.preview_l10n_ae_pint()
        with self.assertRaisesRegex(UserError, "IBR-011"):
            invoice.action_l10n_ae_validate()
        self.assertEqual(invoice.state, "draft")
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.ubl_cii_xml_id or invoice.invoice_pdf_report_id)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)

    def test_commercial_check_refuses_native_buyer_country_constraint(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        outside_scope = self.tax_sale_a.copy(
            {
                "name": "Synthetic commercial Check outside scope",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=outside_scope
        ).with_user(self.simple_accountman)
        invoice.l10n_ae_document_category = "commercial"
        self.partner_a.country_id = False
        self.assertFalse(invoice.env.su)
        with self.assertRaisesRegex(UserError, "IBR-011"):
            invoice.action_l10n_ae_validate()
        with self.assertRaisesRegex(UserError, "IBR-011"):
            invoice.preview_l10n_ae_pint()
        self.assertEqual(invoice.state, "draft")
        self.assertEqual(invoice.amount_total, 100)
        self.assertFalse(
            invoice.l10n_ae_fiscal_uuid or invoice.l10n_ae_transmission_ids
        )

    def test_unprepared_preview_builds_one_native_xml_without_creating_an_issue(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        calls = []
        previous_profiler = sys.getprofile()

        def observe_native_export(frame, event, argument):
            if (
                event == "call"
                and frame.f_code.co_name == "_export_invoice"
                and frame.f_code.co_filename.endswith(
                    "/account_edi_ubl_cii/models/account_edi_ubl.py"
                )
            ):
                calls.append(frame.f_code.co_filename)
            if previous_profiler:
                previous_profiler(frame, event, argument)

        # Observe real Python calls, without replacing a product collaborator.
        sys.setprofile(observe_native_export)
        try:
            content = invoice.preview_l10n_ae_pint()
        finally:
            sys.setprofile(previous_profiler)
        _logger.info("UAE_PUBLIC_PREVIEW_NATIVE_EXPORTS=%s", len(calls))
        tree = etree.fromstring(content.encode())
        self.assertEqual(
            tree.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=NS),
            "105.00",
        )
        self.assertEqual(len(calls), 1, "One public preview must build one native XML")
        self.assertEqual(invoice.state, "draft")
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.ubl_cii_xml_id or invoice.invoice_pdf_report_id)

    def test_prepared_native_send_refuses_explicit_conflicting_format(self):
        binding = self._reviewed_binding(
            {"name": "Synthetic native-send conflict sandbox"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_prepared_native_send_refuses_"
            "explicit_conflicting_format.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.partner_id.invoice_sending_method = "manual"
        invoice = invoice.with_user(self.simple_accountman)
        invoice.l10n_ae_binding_id = binding
        self.assertFalse(invoice.env.su)
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id

        def frozen_facts():
            invoice.invalidate_recordset()
            issue.invalidate_recordset()
            return {
                "issue_id": invoice.l10n_ae_transmission_id.id,
                "payload_hash": issue.payload_hash,
                "xml_hash": issue.xml_hash,
                "xml_content": issue.xml_content,
                "idempotency_key": issue.idempotency_key,
                "submission_state": issue.submission_state,
                "attempt_ids": issue.attempt_ids.ids,
                "event_ids": issue.event_ids.ids,
                "ledger": invoice.line_ids.read(
                    [
                        "account_id",
                        "partner_id",
                        "currency_id",
                        "debit",
                        "credit",
                        "balance",
                        "amount_currency",
                        "tax_ids",
                    ]
                ),
            }

        original = frozen_facts()
        invoice_write_date = invoice.write_date
        self.assertFalse(invoice.ubl_cii_xml_id)
        self.assertFalse(invoice.invoice_pdf_report_id)
        # Changing live master data must not reinterpret a frozen original when
        # opening Send. The original still carries its reviewed DXB address.
        self.partner_a.state_id = False
        artifacts_before = (
            self.env["ir.attachment"]
            .search([("res_model", "=", "account.move"), ("res_id", "=", invoice.id)])
            .ids
        )
        wizard = (
            self.env["account.move.send.wizard"]
            .with_user(self.simple_accountman)
            .create({"move_id": invoice.id})
        )
        self.assertFalse(
            [key for key in (wizard.alerts or {}) if key.startswith("l10n_ae_edi_")]
        )
        self.assertFalse(wizard.sending_methods)
        # Even a direct RPC/ORM mode write cannot override a preserved original.
        wizard.write({"l10n_ae_send_format": "pdf_only"})
        self.env.flush_all()
        wizard.invalidate_recordset()
        self.assertFalse(wizard.l10n_ae_can_choose_send_format)
        self.assertEqual(wizard.invoice_edi_format, "kmee_ae_pint")
        self.assertEqual(frozen_facts(), original)
        wizard.write({"invoice_edi_format": "facturx"})
        self.assertEqual(wizard.invoice_edi_format, "facturx")
        with (
            self.cr.savepoint(),
            self.assertRaisesRegex(
                UserError,
                "The selected eInvoice format conflicts with "
                "the frozen UAE PINT-AE issue\\.",
            ),
        ):
            wizard.action_send_and_print()
        self.assertEqual(frozen_facts(), original)
        self.assertEqual(invoice.write_date, invoice_write_date)
        self.assertFalse(invoice.ubl_cii_xml_id)
        self.assertFalse(invoice.invoice_pdf_report_id)
        self.assertEqual(
            self.env["ir.attachment"]
            .search([("res_model", "=", "account.move"), ("res_id", "=", invoice.id)])
            .ids,
            artifacts_before,
        )
        self.assertEqual(
            self.env["l10n_ae.edi.transmission"].search_count(
                [("move_id", "=", invoice.id)]
            ),
            1,
        )

        # Public native Generate still associates exactly the frozen issue XML.
        generate_wizard = (
            self.env["account.move.send.wizard"]
            .with_user(self.simple_accountman)
            .create({"move_id": invoice.id})
        )
        self.assertEqual(generate_wizard.invoice_edi_format, "kmee_ae_pint")
        self.assertFalse(generate_wizard.sending_methods)
        generate_wizard.action_send_and_print()
        self.assertEqual(frozen_facts(), original)
        self.assertEqual(
            invoice.ubl_cii_xml_id.raw.content, original["xml_content"].encode("utf-8")
        )
        self.assertEqual(
            self.env["l10n_ae.edi.transmission"].search_count(
                [("move_id", "=", invoice.id)]
            ),
            1,
        )

    @requires_normative_resources("1.0.3", "1.0.4")
    def test_emitted_commercial_invoice_preserves_package_across_official_cycle(self):
        # Independent worksheet: one AED100 supply, category O/0%, VAT0,
        # exclusive/inclusive/payable100. No allowance, advance or refund.
        self.env.company.partner_id.vat = False
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic corpus O",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        binding = self._reviewed_binding(
            {"name": "Synthetic emitted corpus sandbox"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_emitted_commercial_invoice_pr"
            "eserves_package_across_official_cycle.",
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=tax)
        invoice.write(
            {
                "l10n_ae_document_category": "commercial",
                "l10n_ae_binding_id": binding.id,
                "l10n_ae_fiscal_uuid": "abababac-1111-4111-8111-111111111111",
            }
        )
        invoice = invoice.with_user(self.simple_accountman).with_context(
            allowed_company_ids=[self.env.company.id]
        )
        self.assertFalse(invoice.env.su)
        packages = [
            ("kmee-pint-ae-1.0.3-ubl-2.1", self.normative_directories["1.0.3"]),
            ("kmee-pint-ae-1.0.4-ubl-2.1", self.normative_directories["1.0.4"]),
        ]
        with patch.dict(
            os.environ,
            {
                "KMEE_UAE_NORMATIVE_RULESET": packages[0][0],
                "KMEE_UAE_NORMATIVE_DIR": packages[0][1],
            },
        ):
            invoice.action_post()
            invoice.action_l10n_ae_prepare()
            issue = invoice.l10n_ae_transmission_id
            public_a = invoice.validate_l10n_ae_pint()
            self.assertTrue(public_a["valid"], public_a)
            self.assertEqual(public_a["profile"], "billing")
            self.assertEqual(public_a["ruleset"], packages[0][0])
            original = (
                issue.id,
                issue.payload,
                issue.xml_content,
                issue.idempotency_key,
            )
            ledger = invoice.line_ids.read(
                [
                    "account_id",
                    "partner_id",
                    "currency_id",
                    "debit",
                    "credit",
                    "balance",
                    "amount_currency",
                    "tax_ids",
                ]
            )
            self.assertEqual(
                json.loads(issue.payload)["normative"],
                {
                    "ruleset": packages[0][0],
                    "manifest_sha256": public_a["resources"]["manifest_sha256"],
                },
            )
            emitted = issue.xml_content.encode("utf-8")
            emitted_sha = hashlib.sha256(emitted).hexdigest()
            self.assertEqual(issue.xml_hash, emitted_sha)
            self.assertEqual(invoice.preview_l10n_ae_pint().encode(), emitted)
            xml = etree.fromstring(emitted)
            self.assertEqual(xml.findtext("cbc:InvoiceTypeCode", namespaces=NS), "480")
            self.assertEqual(
                xml.findtext("cbc:CustomizationID", namespaces=NS),
                "urn:peppol:pint:billing-1@ae-1",
            )
            self.assertEqual(
                xml.findtext("cbc:ProfileID", namespaces=NS), "urn:peppol:bis:billing"
            )
            for term in ["TaxExclusiveAmount", "TaxInclusiveAmount", "PayableAmount"]:
                self.assertEqual(
                    Decimal(
                        xml.findtext(
                            f"cac:LegalMonetaryTotal/cbc:{term}", namespaces=NS
                        )
                    ),
                    Decimal(100),
                )
            self.assertEqual(
                Decimal(xml.findtext("cac:TaxTotal/cbc:TaxAmount", namespaces=NS)), 0
            )
            subtotals = xml.findall("cac:TaxTotal/cac:TaxSubtotal", namespaces=NS)
            self.assertEqual(len(subtotals), 1)
            self.assertEqual(
                subtotals[0].findtext("cac:TaxCategory/cbc:ID", namespaces=NS), "O"
            )
            self.assertEqual(
                Decimal(subtotals[0].findtext("cbc:TaxableAmount", namespaces=NS)),
                Decimal(100),
            )
            self.assertEqual(
                Decimal(subtotals[0].findtext("cbc:TaxAmount", namespaces=NS)), 0
            )
            self.assertEqual(
                xml.findtext(
                    "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:ID",
                    namespaces=NS,
                ),
                "O",
            )
            self.assertEqual(
                (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
                (100, 0, 100),
            )
            reports = []
            for ruleset, directory in [packages[0], packages[1], packages[0]]:
                # Cross-check the exact frozen Odoo-emitted bytes at the public
                # validator boundary; never modify XML or official resources.
                report = OfflinePintValidator(directory, ruleset=ruleset).validate(
                    emitted
                )
                self.assertTrue(report["valid"], report)
                self.assertEqual(report["ruleset"], ruleset)
                self.assertEqual(report["profile"], "billing")
                self.assertEqual(report["xsd_errors"], [])
                reports.append(report)
            self.assertEqual(reports[0], reports[2])
            self.assertNotEqual(
                reports[0]["resources"]["manifest_sha256"],
                reports[1]["resources"]["manifest_sha256"],
            )
            with patch.dict(
                os.environ,
                {
                    "KMEE_UAE_NORMATIVE_RULESET": packages[1][0],
                    "KMEE_UAE_NORMATIVE_DIR": packages[1][1],
                },
            ):
                self.assertEqual(invoice.preview_l10n_ae_pint().encode(), emitted)
                self.assertEqual(invoice.l10n_ae_normative_ruleset, packages[0][0])
                with self.assertRaisesRegex(UserError, "(?i)pinned.*package"):
                    invoice.validate_l10n_ae_pint()
                self.assertEqual(invoice.line_ids.read(list(ledger[0].keys())), ledger)
            self.assertEqual(invoice.validate_l10n_ae_pint(), public_a)
            invoice.action_l10n_ae_prepare()
            self.assertEqual(invoice.l10n_ae_transmission_id, issue)
            self.assertEqual(
                (issue.id, issue.payload, issue.xml_content, issue.idempotency_key),
                original,
            )
            self.assertEqual(invoice.line_ids.read(list(ledger[0].keys())), ledger)
            self.assertFalse(issue.attempt_ids)
            self.assertFalse(issue.validation_receipt)
            _logger.info(
                "UAE_EMITTED_COMMERCIAL_VERSION=%s",
                json.dumps(
                    {
                        "code": "480",
                        "category": "O",
                        "amounts": [100, 0, 100],
                        "xml_sha256": emitted_sha,
                        "operator_sudo": invoice.env.su,
                        "reports": reports,
                        "public_a": public_a,
                        "public_drift_refused": True,
                        "ledger_preserved": True,
                        "scope": "One emitted vector; no full corpus/browser/ASP",
                    },
                    sort_keys=True,
                ),
            )

    @requires_normative_resources("1.0.3", "1.0.4")
    def test_new_commercial_issue_keeps_b_package_when_deployment_returns_to_a(self):
        # Three separate ordinary supplies: AED100/200/300, O/0%, VAT0;
        # exclusive/inclusive/payable equals each independently chosen amount.
        self.env.company.partner_id.vat = False
        tax = self.tax_sale_a.copy(
            {
                "name": "Synthetic reverse-cycle O",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        binding = self._reviewed_binding(
            {"name": "Synthetic new-document version cycle"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_new_commercial_issue_keeps_b_"
            "package_when_deployment_returns_to_a.",
        )
        packages = [
            ("kmee-pint-ae-1.0.3-ubl-2.1", self.normative_directories["1.0.3"]),
            ("kmee-pint-ae-1.0.4-ubl-2.1", self.normative_directories["1.0.4"]),
        ]
        originals = []
        cycle = []
        for index, (amount, package) in enumerate(
            [(100, packages[0]), (200, packages[1]), (300, packages[0])], start=1
        ):
            with patch.dict(
                os.environ,
                {
                    "KMEE_UAE_NORMATIVE_RULESET": package[0],
                    "KMEE_UAE_NORMATIVE_DIR": package[1],
                },
            ):
                move = self.init_invoice("out_invoice", amounts=[amount], taxes=tax)
                move.write(
                    {
                        "l10n_ae_document_category": "commercial",
                        "l10n_ae_binding_id": binding.id,
                        "l10n_ae_fiscal_uuid": f"abababac-2222-4222-8222-{index:012d}",
                    }
                )
                move = move.with_user(self.simple_accountman).with_context(
                    allowed_company_ids=[self.env.company.id]
                )
                self.assertFalse(move.env.su)
                move.action_post()
                move.action_l10n_ae_prepare()
                report = move.validate_l10n_ae_pint()
                self.assertTrue(report["valid"], report)
                self.assertEqual(report["profile"], "billing")
                self.assertEqual(report["ruleset"], package[0])
                issue = move.l10n_ae_transmission_id
                pin = json.loads(issue.payload)["normative"]
                self.assertEqual(
                    pin,
                    {
                        "ruleset": package[0],
                        "manifest_sha256": report["resources"]["manifest_sha256"],
                    },
                )
                self.assertEqual(
                    (move.amount_untaxed, move.amount_tax, move.amount_total),
                    (amount, 0, amount),
                )
                xml = etree.fromstring(issue.xml_content.encode())
                self.assertEqual(
                    xml.findtext("cbc:InvoiceTypeCode", namespaces=NS), "480"
                )
                for term in [
                    "TaxExclusiveAmount",
                    "TaxInclusiveAmount",
                    "PayableAmount",
                ]:
                    self.assertEqual(
                        Decimal(
                            xml.findtext(
                                f"cac:LegalMonetaryTotal/cbc:{term}", namespaces=NS
                            )
                        ),
                        Decimal(amount),
                    )
                self.assertEqual(
                    Decimal(xml.findtext("cac:TaxTotal/cbc:TaxAmount", namespaces=NS)),
                    0,
                )
                subtotals = xml.findall("cac:TaxTotal/cac:TaxSubtotal", namespaces=NS)
                self.assertEqual(len(subtotals), 1)
                self.assertEqual(
                    subtotals[0].findtext("cac:TaxCategory/cbc:ID", namespaces=NS), "O"
                )
                self.assertEqual(
                    Decimal(subtotals[0].findtext("cbc:TaxableAmount", namespaces=NS)),
                    Decimal(amount),
                )
                self.assertEqual(
                    Decimal(subtotals[0].findtext("cbc:TaxAmount", namespaces=NS)), 0
                )
                self.assertEqual(
                    xml.findtext(
                        "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:ID",
                        namespaces=NS,
                    ),
                    "O",
                )
                emitted_sha = hashlib.sha256(issue.xml_content.encode()).hexdigest()
                self.assertEqual(issue.xml_hash, emitted_sha)
                originals.append(
                    {
                        "move": move,
                        "issue": issue,
                        "report": report,
                        "amount": amount,
                        "pin": pin,
                        "facts": (
                            issue.id,
                            issue.payload,
                            issue.xml_content,
                            issue.idempotency_key,
                            issue.create_date,
                            issue.write_date,
                        ),
                        "ledger": move.line_ids.read(
                            [
                                "account_id",
                                "currency_id",
                                "debit",
                                "credit",
                                "balance",
                                "amount_currency",
                                "tax_ids",
                                "create_date",
                                "write_date",
                            ]
                        ),
                    }
                )
                refusals = []
                for prior in originals:
                    old_move, old_issue = prior["move"], prior["issue"]
                    if prior["pin"]["ruleset"] != package[0]:
                        with self.assertRaisesRegex(UserError, "(?i)pinned.*package"):
                            old_move.validate_l10n_ae_pint()
                        refusals.append(old_issue.id)
                    else:
                        self.assertEqual(
                            old_move.validate_l10n_ae_pint(), prior["report"]
                        )
                    self.assertEqual(
                        old_move.l10n_ae_normative_ruleset, prior["pin"]["ruleset"]
                    )
                    self.assertEqual(
                        old_move.l10n_ae_normative_manifest_sha256,
                        prior["pin"]["manifest_sha256"],
                    )
                    self.assertEqual(
                        old_move.preview_l10n_ae_pint(), old_issue.xml_content
                    )
                    self.assertEqual(
                        old_move.line_ids.read(list(prior["ledger"][0].keys())),
                        prior["ledger"],
                    )
                    self.assertEqual(
                        (
                            old_issue.id,
                            old_issue.payload,
                            old_issue.xml_content,
                            old_issue.idempotency_key,
                            old_issue.create_date,
                            old_issue.write_date,
                        ),
                        prior["facts"],
                    )
                    self.assertEqual(old_move.l10n_ae_transmission_id, old_issue)
                    self.assertFalse(old_issue.attempt_ids)
                    self.assertFalse(old_issue.validation_receipt)
                    self.assertFalse(old_issue.remote_id)
                cycle.append(
                    {
                        "package": package[0],
                        "new_issue": issue.id,
                        "amounts": [amount, 0, amount],
                        "pin": pin,
                        "xml_sha256": emitted_sha,
                        "refused_issues": refusals,
                    }
                )
        self.assertEqual(len({item["issue"].id for item in originals}), 3)
        self.assertEqual(len({item["issue"].idempotency_key for item in originals}), 3)
        self.assertEqual(cycle[1]["refused_issues"], [originals[0]["issue"].id])
        self.assertEqual(cycle[2]["refused_issues"], [originals[1]["issue"].id])
        _logger.info(
            "UAE_COMMERCIAL_REVERSE_VERSION=%s",
            json.dumps(
                {
                    "cycle": cycle,
                    "originals_preserved": True,
                    "ledger_preserved": True,
                    "scope": (
                        "Three new public commercial issues; "
                        "in-process configuration only"
                    ),
                },
                sort_keys=True,
            ),
        )

    def test_pinned_identity_cache_respects_selected_company_access(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_fiscal_uuid = "16161616-1616-4616-8616-161616161616"
        invoice.l10n_ae_binding_id = self._reviewed_binding(
            {"name": "Company-sensitive pinned package review"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_pinned_identity_cache_respect"
            "s_selected_company_access.",
        )
        invoice.action_l10n_ae_prepare()
        other_company = self.env["res.company"].create(
            {"name": "Synthetic other package review company"}
        )
        self.simple_accountman.write({"company_ids": [(4, other_company.id)]})
        reviewed = invoice.with_user(self.simple_accountman).with_context(
            allowed_company_ids=[invoice.company_id.id]
        )
        self.assertEqual(
            reviewed.l10n_ae_normative_ruleset, "kmee-pint-ae-1.0.4-ubl-2.1"
        )
        restricted = reviewed.with_context(allowed_company_ids=[other_company.id])
        with self.assertRaises(AccessError):
            self.assertTrue(restricted.l10n_ae_normative_ruleset)
        with self.assertRaises(AccessError):
            self.assertTrue(restricted.l10n_ae_normative_manifest_sha256)
        self.assertEqual(
            reviewed.l10n_ae_normative_ruleset, "kmee-pint-ae-1.0.4-ubl-2.1"
        )
        self.assertFalse(invoice.l10n_ae_transmission_id.attempt_ids)

    @requires_normative_resources("1.0.4")
    def test_prepared_packages_survive_configuration_swap_and_rollback(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_fiscal_uuid = "13131313-1313-4313-8313-131313131313"
        binding = self._reviewed_binding(
            {"name": "Synthetic normative rollback binding"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_prepared_packages_survive_con"
            "figuration_swap_and_rollback.",
        )
        invoice.l10n_ae_binding_id = binding
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        original = (issue.payload, issue.xml_content, issue.idempotency_key)
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"])
        with patch.dict(
            os.environ,
            {
                "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.3-ubl-2.1",
                "KMEE_UAE_NORMATIVE_DIR": self.normative_directories["1.0.3"],
            },
        ):
            with self.assertRaisesRegex(UserError, "(?i)pinned.*package"):
                invoice.validate_l10n_ae_pint()
            older = self.init_invoice(
                "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
            )
            older.l10n_ae_fiscal_uuid = "14141414-1414-4414-8414-141414141414"
            older.l10n_ae_binding_id = binding
            older.action_l10n_ae_prepare()
            older_issue = older.l10n_ae_transmission_id
            older_original = (
                older_issue.payload,
                older_issue.xml_content,
                older_issue.idempotency_key,
            )
            older_report = older.validate_l10n_ae_pint()
            self.assertTrue(older_report["valid"])
            self.assertEqual(older_report["ruleset"], "kmee-pint-ae-1.0.3-ubl-2.1")
            self.assertEqual(
                json.loads(older_issue.payload)["normative"],
                {
                    "ruleset": "kmee-pint-ae-1.0.3-ubl-2.1",
                    "manifest_sha256": older_report["resources"]["manifest_sha256"],
                },
            )
        self.assertEqual(invoice.validate_l10n_ae_pint(), report)
        with self.assertRaisesRegex(UserError, "(?i)pinned.*package"):
            older.validate_l10n_ae_pint()
        for current, original_values in (
            (issue, original),
            (older_issue, older_original),
        ):
            self.assertEqual(
                (current.payload, current.xml_content, current.idempotency_key),
                original_values,
            )
            self.assertFalse(current.attempt_ids)
            self.assertFalse(current.validation_receipt)
            self.assertEqual(current.move_id.amount_total, 105)
            self.assertEqual(current.move_id.amount_tax, 5)

    def test_preparation_pins_normative_identity_before_transport(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_fiscal_uuid = "12121212-1212-4212-8212-121212121212"
        invoice.l10n_ae_binding_id = self._reviewed_binding(
            {"name": "Synthetic pinned normative package"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_preparation_pins_normative_id"
            "entity_before_transport.",
        )
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        snapshot = json.loads(issue.payload)
        self.assertEqual(
            snapshot.get("normative"),
            {
                "ruleset": "kmee-pint-ae-1.0.4-ubl-2.1",
                "manifest_sha256": (
                    "4f225c8095a2f487c5813b4d0e6515c9e7f986ba1a36e34dca7ae305b7899d76"
                ),
            },
        )
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.validation_receipt)
        self.assertEqual(issue.submission_state, "queued")
        self.assertEqual(invoice.amount_untaxed, 100)
        self.assertEqual(invoice.amount_tax, 5)
        self.assertEqual(invoice.amount_total, 105)
        self.assertEqual(invoice.preview_l10n_ae_pint(), issue.xml_content)

    @requires_normative_resources("1.0.4")
    def test_export_with_an_existing_foreign_participant_keeps_exchange(self):
        self.partner_a.write(
            {
                "country_id": self.env.ref("base.au").id,
                "state_id": self.env.ref("base.state_au_2").id,
                "street": "Synthetic integrated export buyer street",
                "city": "Sydney",
                "vat": False,
                "routing_scheme": "0151",
                "routing_endpoint": "53004085616",
                "additional_identifiers": {"AU_ACN": "004085616"},
                "l10n_ae_registration_type": False,
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_exports": True,
                "l10n_ae_fiscal_uuid": "bbbbbbbf-3333-4333-8333-333333333333",
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Integrated foreign export buyer"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_export_with_an_existi"
                    "ng_foreign_participant_keeps_exchange.",
                ).id,
            }
        )
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        self.assertEqual(issue.exchange_mode, "exchange")
        self.assertEqual(issue.delivery_c3_state, "unknown")
        self.assertEqual(issue.reporting_c3_state, "unknown")
        self.assertIsNone(json.loads(issue.payload)["destination"])
        self.assertIsNone(json.loads(issue.payload)["buyer_onboarding_review"])
        endpoint = etree.fromstring(issue.xml_content.encode()).find(
            "cac:AccountingCustomerParty/cac:Party/cbc:EndpointID", NS
        )
        self.assertEqual(
            (endpoint.get("schemeID"), endpoint.text), ("0151", "53004085616")
        )
        self.assertEqual(invoice.amount_total, 105)
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])

    @requires_normative_resources("1.0.4")
    def test_explicit_foreign_vat_is_preserved_and_requires_formal_review(self):
        self.partner_a.write(
            {
                "country_id": self.env.ref("base.au").id,
                "state_id": self.env.ref("base.state_au_2").id,
                "street": "Synthetic export buyer street",
                "city": "Sydney",
                "vat": "53004085616",
                "routing_endpoint": False,
                "additional_identifiers": {"AU_ACN": "004085616"},
                "l10n_ae_registration_type": False,
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_exports": True,
                "l10n_ae_export_buyer_no_peppol": True,
                "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                "l10n_ae_buyer_onboarding_review_reference": "SYNTH-EXPORT-VAT-REVIEW",
                "l10n_ae_fiscal_uuid": "bbbbbbbf-4444-4444-8444-444444444444",
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Explicit foreign VAT review"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_explicit_foreign_vat_"
                    "is_preserved_and_requires_formal_review.",
                ).id,
            }
        )
        tree = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        tax = tree.find("cac:AccountingCustomerParty/cac:Party/cac:PartyTaxScheme", NS)
        self.assertIsNotNone(tax)
        self.assertEqual(tax.findtext("cac:TaxScheme/cbc:ID", namespaces=NS), "GST")
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        with self.assertRaisesRegex(UserError, "(?i)ibr-133-ae"):
            invoice.action_l10n_ae_validate_pint()
        self.assertEqual(self.partner_a.vat, "53004085616")
        self.assertEqual(json.loads(issue.payload)["buyer"]["vat"], "53004085616")
        self.assertEqual(invoice.preview_l10n_ae_pint(), issue.xml_content)
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.event_ids)
        self.assertEqual(issue.validation_c2_state, "unknown")
        self.assertEqual((invoice.state, invoice.amount_total), ("posted", 105))

    def test_selfbilling_export_review_cannot_replace_the_actual_company_buyer(self):
        bill, _agreement = self._selfbilling_bill_with_agreement()
        self.partner_a.routing_endpoint = False
        bill.write(
            {
                "l10n_ae_exports": True,
                "l10n_ae_export_buyer_no_peppol": True,
                "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                "l10n_ae_buyer_onboarding_review_reference": "SYNTH-SELFBILL-EXPORT",
            }
        )
        with self.assertRaisesRegex(UserError, "Review the existing Participant ID"):
            bill.action_l10n_ae_validate()
        self.assertEqual(bill.company_id.partner_id.routing_endpoint, "1000000001")
        self.assertFalse(self.partner_a.routing_endpoint)
        self.assertFalse(bill.l10n_ae_transmission_id)

    def test_commercial_exports_require_category_review_before_destination_selection(
        self,
    ):
        outside_scope = self.tax_sale_a.copy(
            {
                "name": "Synthetic export commercial outside-scope tax",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
                "ubl_cii_tax_exemption_reason": "Outside scope",
            }
        )
        for endpoint, declaration in (("1000000002", False), (False, True)):
            with self.subTest(endpoint=endpoint):
                self.partner_a.routing_endpoint = endpoint
                invoice = self.init_invoice(
                    "out_invoice", amounts=[100], taxes=outside_scope, post=False
                )
                invoice.write(
                    {
                        "l10n_ae_document_category": "commercial",
                        "l10n_ae_exports": True,
                        "l10n_ae_export_buyer_no_peppol": declaration,
                        "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                        "l10n_ae_buyer_onboarding_review_reference": "SYNTH-REVIEW",
                    }
                )
                with self.assertRaisesRegex(
                    UserError, "Exports.*commercial invoice or credit note"
                ):
                    invoice.action_l10n_ae_validate()
                self.assertEqual(invoice.l10n_ae_document_category, "commercial")
                self.assertTrue(invoice.l10n_ae_exports)
                self.assertEqual(self.partner_a.routing_endpoint, endpoint)
                self.assertEqual((invoice.state, invoice.amount_total), ("draft", 100))
                self.assertFalse(invoice.l10n_ae_transmission_id)

    @requires_normative_resources("1.0.4")
    def test_prepared_export99_preserves_review_foreign_identity_and_free_zone(self):
        self.partner_a.write(
            {
                "name": "Synthetic Australian Export Buyer",
                "country_id": self.env.ref("base.au").id,
                "state_id": self.env.ref("base.state_au_2").id,
                "street": "Synthetic export buyer street",
                "city": "Sydney",
                "vat": False,
                "routing_endpoint": False,
                "additional_identifiers": {"AU_ACN": "004085616"},
                "l10n_ae_registration_type": False,
                "l10n_ae_registration_authority": "Synthetic Australian Registry",
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_exports": True,
                "l10n_ae_export_buyer_no_peppol": True,
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000003",
                "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                "l10n_ae_buyer_onboarding_review_reference": "SYNTH-EXPORT-REVIEW",
                "l10n_ae_fiscal_uuid": "bbbbbbbf-2222-4222-8222-222222222222",
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Reviewed foreign export destination"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_prepared_export99_pre"
                    "serves_review_foreign_identity_and_free_zone.",
                ).id,
            }
        )
        invoice.action_l10n_ae_prepare()
        transmission = invoice.l10n_ae_transmission_id
        payload, xml = transmission.payload, transmission.xml_content
        snapshot = json.loads(payload)
        self.assertEqual(
            snapshot.get("buyer_onboarding_review"),
            {
                "status": "not_registered_in_peppol",
                "date": "2019-01-01",
                "source": "SYNTH-EXPORT-REVIEW",
            },
        )
        self.assertEqual(
            snapshot["destination"],
            {
                "scheme": "0235",
                "endpoint": "9900000099",
                "reason": "export_buyer_no_peppol",
            },
        )
        buyer = snapshot["buyer"]
        self.assertIsNone(buyer["endpoint"])
        self.assertIsNone(buyer["vat"])
        self.assertEqual(buyer["name"], "Synthetic Australian Export Buyer")
        self.assertEqual(buyer["country"], "AU")
        self.assertEqual(buyer["street"], "Synthetic export buyer street")
        self.assertEqual(buyer["legal_registration"], "004085616")
        self.assertEqual(buyer["legal_registration_type"], "AU_ACN")
        self.assertEqual(
            buyer["legal_registration_authority"], "Synthetic Australian Registry"
        )
        self.assertEqual(
            buyer.get("additional_identifiers"),
            {"AU_ACN": "004085616", "AU_ABN": "53004085616"},
        )
        self.assertEqual(snapshot["transaction_flags"], "10000001")
        self.assertEqual(snapshot["beneficiary_id"], "1000000003")
        self.assertIsNone(snapshot["alternative_delivery"])
        self.assertEqual(transmission.exchange_mode, "reporting_only")
        self.assertEqual(transmission.confirmation_state, "pending")
        self.assertEqual(transmission.delivery_c3_state, "not_applicable")
        self.assertEqual(transmission.reporting_c3_state, "not_applicable")
        self.assertFalse(transmission.attempt_ids)
        self.assertFalse(transmission.event_ids)
        for field, value in (
            ("l10n_ae_export_buyer_no_peppol", False),
            ("l10n_ae_buyer_onboarding_review_date", "2019-01-02"),
            ("l10n_ae_buyer_onboarding_review_reference", "REPLACED-REVIEW"),
            ("l10n_ae_exports", False),
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(UserError, "frozen UAE invoice"),
            ):
                invoice.write({field: value})
        self.partner_a.write(
            {
                "name": "Later buyer name",
                "street": "Later buyer address",
                "additional_identifiers": {"AU_ACN": "000000019"},
                "l10n_ae_registration_authority": "Later authority",
            }
        )
        self.assertEqual(invoice.preview_l10n_ae_pint(), xml)
        self.assertEqual(transmission.payload, payload)
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )

    def test_export99_refuses_conflicting_declarations_and_existing_participant(self):
        for values, endpoint, diagnostic in (
            (
                {"l10n_ae_exports": False},
                False,
                "Select Exports before confirming a non-Peppol export buyer",
            ),
            ({}, "1000000002", "Review the existing Participant ID"),
            (
                {"l10n_ae_deemed_supply": True},
                False,
                "combined export and deemed-supply routing policy",
            ),
            (
                {"l10n_ae_buyer_not_implemented": True},
                False,
                "conflicting buyer status declarations",
            ),
        ):
            with self.subTest(values=values, endpoint=endpoint):
                self.partner_a.routing_endpoint = endpoint
                invoice = self.init_invoice(
                    "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=False
                )
                invoice.write(
                    {
                        "l10n_ae_exports": True,
                        "l10n_ae_export_buyer_no_peppol": True,
                        "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                        "l10n_ae_buyer_onboarding_review_reference": "SYNTH-REVIEW",
                        **values,
                    }
                )
                with self.assertRaisesRegex(UserError, diagnostic):
                    invoice.action_l10n_ae_validate()
                self.assertEqual(self.partner_a.routing_endpoint, endpoint)
                self.assertFalse(invoice.l10n_ae_fiscal_uuid)
                self.assertFalse(invoice.l10n_ae_transmission_id)
                self.assertEqual(invoice.state, "draft")

    def test_export99_posts_but_requires_dated_recipient_review_before_preparation(
        self,
    ):
        self.env.company.l10n_ae_edi_auto_prepare = True
        self.partner_a.routing_endpoint = False
        binding = self._reviewed_binding(
            {"name": "Reviewed export recipient"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_export99_posts_but_requires_d"
            "ated_recipient_review_before_preparation.",
        )
        for declaration, date, reference, diagnostic in (
            (False, False, False, "Review the export buyer.*Peppol status"),
            (True, False, "SYNTH-REVIEW", "export buyer status review date and source"),
            (True, "2019-01-01", False, "export buyer status review date and source"),
            (True, "2019-01-01", "  ", "export buyer status review date and source"),
        ):
            with self.subTest(declaration=declaration, date=date, reference=reference):
                invoice = self.init_invoice(
                    "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=False
                )
                invoice.write(
                    {
                        "l10n_ae_exports": True,
                        "l10n_ae_export_buyer_no_peppol": declaration,
                        "l10n_ae_buyer_onboarding_review_date": date,
                        "l10n_ae_buyer_onboarding_review_reference": reference,
                        "l10n_ae_binding_id": binding.id,
                        "l10n_ae_fiscal_uuid": str(uuid.uuid4()),
                    }
                )
                invoice.action_post()
                self.assertEqual(invoice.state, "posted")
                self.assertEqual(invoice.l10n_ae_preparation_state, "review")
                self.assertRegex(invoice.l10n_ae_preparation_error, diagnostic)
                self.assertFalse(invoice.l10n_ae_transmission_ids)
                with self.assertRaisesRegex(UserError, diagnostic):
                    invoice.action_l10n_ae_prepare()

    @requires_normative_resources("1.0.4")
    def test_reviewed_non_peppol_export_routes_to_99_preserving_the_buyer(self):
        self.partner_a.write(
            {
                "name": "Synthetic Australian Export Buyer",
                "country_id": self.env.ref("base.au").id,
                "state_id": self.env.ref("base.state_au_2").id,
                "street": "Synthetic export buyer street",
                "city": "Sydney",
                "vat": False,
                "routing_endpoint": False,
                "additional_identifiers": {"AU_ACN": "004085616"},
                "l10n_ae_registration_authority": "Synthetic Australian Registry",
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_exports": True,
                "l10n_ae_export_buyer_no_peppol": True,
                "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                "l10n_ae_buyer_onboarding_review_reference": (
                    "SYNTH-EXPORT-BUYER-PEPPOL-REVIEW"
                ),
                "l10n_ae_fiscal_uuid": "bbbbbbbf-1111-4111-8111-111111111111",
            }
        )
        tree = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        buyer = tree.find("cac:AccountingCustomerParty/cac:Party", NS)
        endpoint = buyer.find("cbc:EndpointID", NS)
        self.assertEqual(
            (endpoint.get("schemeID"), endpoint.text), ("0235", "9900000099")
        )
        self.assertEqual(
            tree.findtext("cbc:ProfileExecutionID", namespaces=NS), "00000001"
        )
        self.assertEqual(
            buyer.findtext("cac:PartyLegalEntity/cbc:RegistrationName", namespaces=NS),
            "Synthetic Australian Export Buyer",
        )
        self.assertEqual(
            buyer.findtext("cac:PostalAddress/cbc:StreetName", namespaces=NS),
            "Synthetic export buyer street",
        )
        self.assertEqual(
            buyer.findtext(
                "cac:PostalAddress/cac:Country/cbc:IdentificationCode", namespaces=NS
            ),
            "AU",
        )
        self.assertEqual(
            buyer.findtext("cac:PartyLegalEntity/cbc:CompanyID", namespaces=NS),
            "004085616",
        )
        self.assertFalse(
            buyer.findall("cac:PartyTaxScheme", NS),
            self.partner_a.read(["vat", "additional_identifiers"]),
        )
        self.assertFalse(self.partner_a.routing_endpoint)
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_disclosed_agent_invalid_principal_posts_with_preparation_diagnostic(
        self,
    ):
        self.env.company.l10n_ae_edi_auto_prepare = True
        for principal, diagnostic in (
            (False, "IBR-137-AE"),
            ("100000000100003", "IBR-176-AE"),
            ("synthetic-principal", "Principal ID.*15 ASCII"),
            ("١٠٠٠٠٠٠٠٠٣٠٠٠٠٣", "Principal ID.*15 ASCII"),
        ):
            with self.subTest(principal=principal):
                invoice = self.init_invoice(
                    "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=False
                )
                invoice.write(
                    {
                        "l10n_ae_disclosed_agent": True,
                        "l10n_ae_principal_id": principal,
                    }
                )
                invoice.action_post()
                self.assertEqual(invoice.state, "posted")
                self.assertEqual(invoice.l10n_ae_preparation_state, "review")
                self.assertRegex(invoice.l10n_ae_preparation_error, diagnostic)
                self.assertFalse(invoice.l10n_ae_transmission_ids)
                with self.assertRaisesRegex(UserError, diagnostic):
                    invoice.action_l10n_ae_prepare()

    @requires_normative_resources("1.0.4")
    def test_prepared_disclosed_agent_issue_freezes_principal_and_scenario(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_disclosed_agent": True,
                "l10n_ae_principal_id": "100000000300003",
                "l10n_ae_fiscal_uuid": "aaaaaaaf-2222-4222-8222-222222222222",
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Disclosed agent preserved issue"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_prepared_disclosed_ag"
                    "ent_issue_freezes_principal_and_scenario.",
                ).id,
            }
        )
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        self.assertEqual(
            json.loads(issue.payload).get("principal_id"), "100000000300003"
        )
        original = issue.xml_content
        for values in (
            {"l10n_ae_principal_id": "100000000400003"},
            {"l10n_ae_disclosed_agent": False},
        ):
            with self.assertRaisesRegex(UserError, "frozen"):
                invoice.write(values)
        self.env.company.partner_id.street = "Changed supplier master data"
        self.assertEqual(invoice.preview_l10n_ae_pint(), original)
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])

    def test_principal_without_agent_classification_requires_review(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.l10n_ae_principal_id = "100000000300003"
        with self.assertRaisesRegex(UserError, "disclosed-agent"):
            invoice.action_l10n_ae_validate()
        self.assertEqual(invoice.state, "draft")
        self.assertFalse(invoice.l10n_ae_transmission_id)

    @requires_normative_resources("1.0.4")
    def test_disclosed_agent_invoice_preserves_principal_and_supplier_vat(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_disclosed_agent": True,
                "l10n_ae_principal_id": "100000000300003",
                "l10n_ae_fiscal_uuid": "aaaaaaaf-1111-4111-8111-111111111111",
            }
        )
        xml = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        self.assertEqual(
            xml.findtext("cbc:ProfileExecutionID", namespaces=NS), "00000100"
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
                "cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID",
                namespaces=NS,
            ),
            "100000000100003",
        )
        self.assertEqual((invoice.amount_tax, invoice.amount_total), (5, 105))
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_identity_package_refuses_hidden_original_decision_retention_or_hold(self):
        binding = self._reviewed_binding(
            {"name": "Complete identity evidence access"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"]
        first = receiver.receive_controlled_document(
            binding.id, "identity-archive-access", b"<first/>"
        )
        selected = receiver.receive_controlled_document(
            binding.id, "identity-archive-access", b"<second/>"
        )
        decision = selected.action_select_identity_variant(
            "Reviewed both synthetic originals.", "SYNTH-ARCHIVE-ACCESS-001"
        )
        policy = self.env["l10n_ae.edi.retention"].create(
            {
                "name": "Synthetic identity archive basis",
                "inbox_id": first.id,
                "record_class": "evidence",
                "tax_purpose": "vat",
                "anchor_kind": "tax_period_end",
                "period_start": "2026-01-01",
                "anchor_date": "2026-12-31",
                "retention_years": 5,
                "source_reference": "Synthetic source, not ratified",
                "policy_version": "synthetic-access-1",
                "review_note": "Illustrative review",
            }
        )
        policy.action_approve()
        hold = self.env["l10n_ae.edi.retention.hold"].create(
            {
                "retention_id": policy.id,
                "reason": "Synthetic retained audit",
                "source_reference": "Synthetic audit evidence",
            }
        )
        auditor = new_test_user(
            self.env,
            login="synthetic-complete-identity-auditor",
            groups="l10n_ae_edi.group_fiscal_auditor,base.group_allow_export",
            company_id=self.env.company.id,
            company_ids=self.env.company.ids,
        )
        audit = selected.with_user(auditor)
        before = audit.evidence_archive.content
        for hidden in (first, decision, policy, hold):
            restriction = self.env["ir.access"].create(
                {
                    "name": "Synthetic required identity evidence access",
                    "model_id": self.env["ir.model"]._get(hidden._name).id,
                    "operation": "r",
                    "domain": f"[('id', '!=', {hidden.id})]",
                }
            )
            try:
                self.env.invalidate_all()
                for original in (audit, audit.sudo()):
                    with self.assertRaises(AccessError):
                        original.read(["evidence_archive"])
            finally:
                restriction.unlink()
                self.env.invalidate_all()
            self.assertEqual(audit.evidence_archive.content, before)
        self.assertFalse((first | selected).move_id)

    def test_identity_package_refuses_a_corrupt_retained_family_hash(self):
        binding = self._reviewed_binding(
            {"name": "Identity archive integrity"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"]
        first = receiver.receive_controlled_document(
            binding.id, "identity-archive-integrity", b"<first/>"
        )
        selected = receiver.receive_controlled_document(
            binding.id, "identity-archive-integrity", b"<second/>"
        )
        selected.action_select_identity_variant(
            "Compared both synthetic originals.", "SYNTH-ARCHIVE-INTEGRITY-001"
        )
        self.assertTrue(selected.evidence_archive.content)
        # Explicit disposable-storage fault; ordinary evidence CRUD remains refused.
        self.env.cr.execute(
            "UPDATE l10n_ae_edi_inbox SET xml_hash=%s WHERE id=%s", ["0" * 64, first.id]
        )
        self.env.invalidate_all()
        with self.assertRaises(UserError):
            selected.read(["evidence_archive"])
        self.assertFalse((first | selected).move_id)

    def test_identity_review_projection_does_not_share_a_managers_decision_cache(self):
        binding = self._reviewed_binding(
            {"name": "Identity review actor cache"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        receiver.receive_controlled_document(
            binding.id, "identity-actor-cache", b"<first/>"
        )
        selected = receiver.receive_controlled_document(
            binding.id, "identity-actor-cache", b"<second/>"
        )
        decision = selected.action_select_identity_variant(
            "Reviewed both originals.", "SYNTH-ACTOR-CACHE-001"
        )
        auditor = new_test_user(
            self.env,
            login="synthetic-identity-restricted-auditor",
            groups="l10n_ae_edi.group_fiscal_auditor,base.group_allow_export",
            company_id=self.env.company.id,
            company_ids=self.env.company.ids,
        )
        restriction = self.env["ir.access"].create(
            {
                "name": "Synthetic decision visibility by actual user",
                "model_id": self.env["ir.model"]._get("l10n_ae.edi.inbox.decision").id,
                "operation": "r",
                "domain": (
                    f"[('id', '!=', {decision.id})] if user.id == {auditor.id} else []"
                ),
            }
        )
        try:
            self.env.invalidate_all()
            values = selected.read(
                ["identity_review_pending", "identity_decision_ids"]
            )[0]
            self.assertFalse(values["identity_review_pending"])
            self.assertEqual(values["identity_decision_ids"], decision.ids)
            restricted = selected.with_user(auditor).read(
                ["identity_review_pending", "identity_decision_ids"]
            )[0]
            self.assertTrue(restricted["identity_review_pending"])
            self.assertEqual(restricted["identity_decision_ids"], [])
            current = selected.read(
                ["identity_review_pending", "identity_decision_ids"]
            )[0]
            self.assertFalse(current["identity_review_pending"])
            self.assertEqual(current["identity_decision_ids"], decision.ids)
        finally:
            restriction.unlink()
            self.env.invalidate_all()

    @requires_normative_resources("1.0.4")
    def test_identity_selection_requires_reading_every_linked_accounting_document(self):
        binding = self._reviewed_binding(
            {"name": "Hidden linked accounting"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        tree = self._received_test_xml()
        first = receiver.receive_controlled_document(
            binding.id, "hidden-linked-accounting", etree.tostring(tree)
        )
        self.assertTrue(first.action_process())
        tree.insert(0, etree.Comment("Synthetic hidden linked variant"))
        other = receiver.receive_controlled_document(
            binding.id, "hidden-linked-accounting", etree.tostring(tree)
        )
        first.action_select_identity_variant(
            "Retained existing accounting link.", "SYNTH-LINKED-AUTHORITY-001"
        )
        restriction = self.env["ir.access"].create(
            {
                "name": "Synthetic hidden linked accounting document",
                "model_id": self.env["ir.model"]._get("account.move").id,
                "operation": "r",
                "domain": f"[('id', '!=', {first.move_id.id})]",
            }
        )
        try:
            self.env.invalidate_all()
            self.assertTrue(first.identity_review_pending)
            for original in (first, other):
                with self.assertRaises(AccessError), self.cr.savepoint():
                    original.sudo().action_select_identity_variant(
                        "Review cannot read linked accounting.",
                        "SYNTH-LINKED-AUTHORITY-002",
                    )
        finally:
            restriction.unlink()
            self.env.invalidate_all()
        self.assertFalse(first.identity_review_pending)
        self.assertEqual(len(first.identity_decision_ids), 1)
        self.assertFalse(other.move_id)

    @requires_normative_resources("1.0.4")
    def test_cached_identity_package_refreshes_after_another_original_is_processed(
        self,
    ):
        binding = self._reviewed_binding(
            {"name": "Identity package freshness"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        tree = self._received_test_xml()
        first = receiver.receive_controlled_document(
            binding.id, "identity-package-freshness", etree.tostring(tree)
        )
        tree.insert(0, etree.Comment("Synthetic freshness variant"))
        selected = receiver.receive_controlled_document(
            binding.id, "identity-package-freshness", etree.tostring(tree)
        )
        selected.action_select_identity_variant(
            "Reviewed complete current set.", "SYNTH-FRESHNESS-001"
        )
        auditor = new_test_user(
            self.env,
            login="synthetic-identity-cache-auditor",
            groups="l10n_ae_edi.group_fiscal_auditor,base.group_allow_export",
            company_id=self.env.company.id,
            company_ids=self.env.company.ids,
        )
        audit = first.with_user(auditor)
        before = audit.evidence_archive.content
        self.assertTrue(selected.action_process())
        after = audit.evidence_archive.content
        self.assertNotEqual(before, after)
        with (
            zipfile.ZipFile(io.BytesIO(after)) as archive,
            zipfile.ZipFile(
                io.BytesIO(archive.read(f"identity-originals/{selected.id}.zip"))
            ) as nested,
        ):
            receipt = json.loads(nested.read("receipt.json"))
            history = json.loads(nested.read("processing-history.json"))
            self.assertEqual(receipt["move_id"], selected.move_id.id)
            self.assertEqual(receipt["state"], "processed")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["outcome"], "processed")
            self.assertTrue(history[0]["validation_available"])
        self.assertFalse(first.move_id)
        self.assertEqual(selected.move_id.amount_total, 105)

    def test_retained_review_form_cannot_omit_or_expose_a_hidden_original(self):
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        binding = self._reviewed_binding(
            {"name": "Review form authority"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        first = receiver.receive_controlled_document(
            binding.id, "review-form-authority", b"<first/>"
        )
        selected = receiver.receive_controlled_document(
            binding.id, "review-form-authority", b"<second/>"
        )
        action = selected.with_context(
            default_original_ids=[],
            default_review_snapshot=[],
            default_justification="Forged reason",
        ).action_review_identity_variant()
        wizard = (
            self.env[action["res_model"]]
            .with_user(self.simple_accountman)
            .browse(action["res_id"])
        )
        self.assertEqual(wizard.original_ids, first | selected)
        self.assertEqual(len(wizard.review_snapshot), 2)
        self.assertFalse(wizard.justification)
        for values in (
            {"inbox_id": first.id},
            {"original_ids": [(5, 0, 0)]},
            {"review_snapshot": []},
        ):
            with self.assertRaises(UserError), self.cr.savepoint():
                wizard.write(values)
        wizard.write(
            {
                "justification": "Reviewed both originals.",
                "review_reference": "SYNTHETIC-FORM-AUTHORITY-001",
            }
        )
        restriction = self.env["ir.access"].create(
            {
                "name": "Synthetic hidden original in retained assistant",
                "model_id": self.env["ir.model"]._get("l10n_ae.edi.inbox").id,
                "operation": "r",
                "domain": f"[('id', '!=', {first.id})]",
            }
        )
        try:
            self.env.invalidate_all()
            self.assertFalse(wizard.search([("id", "=", wizard.id)]))
            with self.assertRaises(AccessError):
                wizard.read(["review_snapshot"])
            with self.assertRaises(AccessError), self.cr.savepoint():
                wizard.sudo().action_confirm()
        finally:
            restriction.unlink()
            self.env.invalidate_all()
        self.assertFalse(selected.identity_decision_ids)
        wizard.action_confirm()
        self.assertEqual(len(selected.identity_decision_ids), 1)
        self.assertFalse(selected.move_id)

    def test_open_review_form_refuses_a_later_original(self):
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        binding = self._reviewed_binding(
            {"name": "Open review changes"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        first = receiver.receive_controlled_document(
            binding.id, "open-review-changes", b"<first/>"
        )
        selected = receiver.receive_controlled_document(
            binding.id, "open-review-changes", b"<second/>"
        )
        action = selected.action_review_identity_variant()
        with Form(
            self.env[action["res_model"]]
            .with_user(self.simple_accountman)
            .browse(action["res_id"])
            .with_context(**action["context"]),
            view="l10n_ae_edi_pint.edi_inbox_selection_form",
        ) as form:
            form.justification = "Reviewed the two displayed originals."
            form.review_reference = "SYNTHETIC-OPEN-REVIEW-001"
        wizard = form.record
        self.assertEqual(wizard.original_ids, first | selected)
        third = receiver.receive_controlled_document(
            binding.id, "open-review-changes", b"<third/>"
        )
        with self.assertRaises(UserError), self.cr.savepoint():
            wizard.action_confirm()
        self.assertFalse(selected.identity_decision_ids)
        self.assertFalse((first | selected | third).move_id)
        action = selected.action_review_identity_variant()
        fresh = (
            self.env[action["res_model"]]
            .with_user(self.simple_accountman)
            .browse(action["res_id"])
        )
        self.assertEqual(fresh.original_ids, first | selected | third)
        fresh.write(
            {
                "justification": "Reviewed the three current originals.",
                "review_reference": "SYNTHETIC-OPEN-REVIEW-002",
            }
        )
        fresh.action_confirm()
        self.assertEqual(len(selected.identity_decision_ids), 1)

    @requires_normative_resources("1.0.4")
    def test_manager_selects_original_through_native_review_form(self):
        tree = self._received_test_xml()
        binding = self._reviewed_binding(
            {"name": "Native selection form"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        first = receiver.receive_controlled_document(
            binding.id, "native-selection", etree.tostring(tree)
        )
        tree.insert(0, etree.Comment("Synthetic native form variant"))
        selected = receiver.receive_controlled_document(
            binding.id, "native-selection", etree.tostring(tree)
        )
        action = selected.with_context(
            default_inbox_id=first.id, default_justification="Forged caller reason"
        ).action_review_identity_variant()
        with Form(
            self.env[action["res_model"]]
            .with_user(self.simple_accountman)
            .browse(action["res_id"])
            .with_context(**action["context"]),
            view="l10n_ae_edi_pint.edi_inbox_selection_form",
        ) as form:
            self.assertEqual(form.inbox_id, selected)
            self.assertFalse(form.justification)
            form.justification = "Compared both synthetic XML originals."
            form.review_reference = "SYNTHETIC-NATIVE-FORM-001"
        wizard = form.record
        self.assertEqual(wizard.original_ids, first | selected)
        self.assertEqual(wizard.xml_hash, selected.xml_hash)
        result = wizard.action_confirm()
        self.assertEqual(result["res_model"], "l10n_ae.edi.inbox")
        self.assertEqual(result["res_id"], selected.id)
        self.assertEqual(len(selected.identity_decision_ids), 1)
        self.assertFalse(selected.identity_review_pending)
        self.assertFalse(selected.move_id)
        self.assertTrue(selected.action_process(), selected.diagnostic)
        self.assertEqual(selected.move_id.amount_total, 105)
        self.assertFalse(first.move_id)

    def test_elevated_manager_cannot_process_a_hidden_original(self):
        binding = self._reviewed_binding(
            {"name": "Actual processing authority"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id,
                "hidden-processing-authority",
                etree.tostring(self._received_test_xml()),
            )
        )
        restriction = self.env["ir.access"].create(
            {
                "name": "Synthetic hidden original for elevated processing",
                "model_id": self.env["ir.model"]._get("l10n_ae.edi.inbox").id,
                "operation": "r",
                "domain": f"[('id', '!=', {inbox.id})]",
            }
        )
        try:
            self.env.invalidate_all()
            with self.assertRaises(AccessError), self.cr.savepoint():
                inbox.sudo().action_process()
        finally:
            restriction.unlink()
            self.env.invalidate_all()
        self.assertFalse(inbox.move_id)
        self.assertEqual(inbox.state, "received")
        self.assertFalse(inbox.processing_history_ids)

    def test_selection_history_requires_every_original_and_current_internal_manager(
        self,
    ):
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        binding = self._reviewed_binding(
            {"name": "Selection access"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        first = receiver.receive_controlled_document(
            binding.id, "selection-access", b"<first/>"
        )
        second = receiver.receive_controlled_document(
            binding.id, "selection-access", b"<second/>"
        )
        reader = new_test_user(
            self.env,
            login="synthetic-selection-reader",
            groups="account.group_account_readonly",
            company_id=self.env.company.id,
            company_ids=[(6, 0, self.env.company.ids)],
        )
        for elevated in (False, True):
            with (
                self.subTest(elevated=elevated),
                self.assertRaises(AccessError),
                self.cr.savepoint(),
            ):
                second.with_user(reader).sudo(elevated).action_select_identity_variant(
                    "Reviewed.", "SYNTHETIC-ACL-001"
                )
        for reason, reference in ((" ", "ref"), ("reason", " "), (False, "ref")):
            with self.assertRaises(UserError), self.cr.savepoint():
                second.action_select_identity_variant(reason, reference)
        decision = second.with_context(
            default_operator_id=reader.id,
            default_occurred_at="2000-01-01 00:00:00",
            default_member_snapshot=[],
            default_company_id=False,
        ).action_select_identity_variant(
            "Reviewed both originals.", "SYNTHETIC-ACL-002"
        )
        self.assertEqual(decision.operator_id, self.simple_accountman)
        self.assertEqual(decision.company_id, self.env.company)
        self.assertEqual(len(decision.member_snapshot), 2)
        self.assertEqual(
            decision.with_user(reader).read(["selected_xml_hash"])[0][
                "selected_xml_hash"
            ],
            second.xml_hash,
        )
        for operation in (
            lambda: decision.write({"justification": "changed"}),
            decision.copy,
            decision.unlink,
        ):
            with self.assertRaises(UserError), self.cr.savepoint():
                operation()
        restriction = self.env["ir.access"].create(
            {
                "name": "Synthetic hidden selection member",
                "model_id": self.env["ir.model"]._get("l10n_ae.edi.inbox").id,
                "operation": "r",
                "domain": f"[('id', '!=', {first.id})]",
            }
        )
        try:
            self.env.invalidate_all()
            for actor in (reader, self.simple_accountman):
                history = self.env["l10n_ae.edi.inbox.decision"].with_user(actor)
                self.assertFalse(history.search([("id", "=", decision.id)]))
                with self.assertRaises(AccessError):
                    history.browse(decision.id).read(["member_snapshot"])
            self.assertTrue(second.identity_review_pending)
            self.assertFalse(second.identity_decision_ids)
            for elevated in (False, True):
                with self.assertRaises(AccessError), self.cr.savepoint():
                    second.sudo(elevated).action_select_identity_variant(
                        "Reviewed.", "SYNTHETIC-ACL-003"
                    )
            self.assertFalse(second.action_process())
        finally:
            restriction.unlink()
            self.env.invalidate_all()
        self.assertFalse(second.identity_review_pending)
        self.assertEqual(second.identity_decision_ids, decision)
        self.assertEqual(first.original_xml.content, b"<first/>")

    def test_new_original_invalidates_selection_without_rewriting_its_history(self):
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        binding = self._reviewed_binding(
            {"name": "Selection revision"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        first = receiver.receive_controlled_document(
            binding.id, "selection-revision", b"<first/>"
        )
        second = receiver.receive_controlled_document(
            binding.id, "selection-revision", b"<second/>"
        )
        decision = second.action_select_identity_variant(
            "Reviewed two originals.", "SYNTHETIC-REVISION-001"
        )
        snapshot = decision.member_snapshot
        self.assertFalse(second.identity_review_pending)
        self.assertEqual(
            receiver.receive_controlled_document(
                binding.id, "selection-revision", b"<second/>"
            ),
            second,
        )
        self.assertFalse(second.identity_review_pending)
        third = receiver.receive_controlled_document(
            binding.id, "selection-revision", b"<third/>"
        )
        self.assertTrue(second.identity_review_pending)
        self.assertTrue(third.identity_review_pending)
        self.assertFalse(second.action_process())
        revised = second.action_select_identity_variant(
            "Reviewed all three originals.", "SYNTHETIC-REVISION-002"
        )
        self.assertNotEqual(revised, decision)
        self.assertEqual(revised.member_ids, first | second | third)
        self.assertEqual(decision.member_snapshot, snapshot)
        self.assertEqual(decision.member_ids, first | second)
        self.assertFalse(second.identity_review_pending)
        self.assertEqual(len(second.identity_decision_ids), 2)
        self.assertFalse((first | second | third).move_id)

    @requires_normative_resources("1.0.4")
    def test_selection_keeps_the_linked_original_and_restores_internal_resolution(self):
        tree = self._received_test_xml()
        content = etree.tostring(tree)
        binding = self._reviewed_binding(
            {"name": "Pinned identity review"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        first = receiver.receive_controlled_document(
            binding.id, "pinned-identity", content
        )
        self.assertTrue(first.action_process(), first.diagnostic)
        bill = first.move_id
        tree.insert(0, etree.Comment("Synthetic later original"))
        second = receiver.receive_controlled_document(
            binding.id, "pinned-identity", etree.tostring(tree)
        )
        self.env.company.l10n_ae_inbox_reviewer_id = self.simple_accountman
        action = first.action_request_review()
        task = (
            self.env[action["res_model"]]
            .with_user(self.simple_accountman)
            .browse(action["res_id"])
        )
        with self.assertRaises(UserError), self.cr.savepoint():
            second.action_select_identity_variant(
                "Reviewed both originals.", "SYNTHETIC-PIN-001"
            )
        first.action_select_identity_variant(
            "Reviewed existing bill and both originals.", "SYNTHETIC-PIN-002"
        )
        self.assertTrue(first.action_process())
        self.assertEqual(first.state, "processed")
        self.assertTrue(task.action_resolve_review())
        self.assertEqual(task.state, "resolved")
        self.assertEqual(first.move_id, bill)
        self.assertFalse(second.move_id)
        self.assertEqual(len(first.processing_history_ids), 1)
        self.assertTrue(first.identity_conflict)

    @requires_normative_resources("1.0.4")
    def test_reviewed_identity_variant_creates_only_its_one_draft(self):
        tree = self._received_test_xml()
        first_content = etree.tostring(tree)
        tree.insert(0, etree.Comment("Synthetic variant for explicit human review"))
        selected_content = etree.tostring(tree)
        binding = self._reviewed_binding(
            {"name": "Human identity review"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        first = receiver.receive_controlled_document(
            binding.id, "human-reviewed-variants", first_content
        )
        selected = receiver.receive_controlled_document(
            binding.id, "human-reviewed-variants", selected_content
        )
        self.assertFalse(first.action_process())
        self.assertFalse(selected.action_process())
        decision = selected.action_select_identity_variant(
            "Compared both retained originals with the controlled source.",
            "SYNTHETIC-IDENTITY-REVIEW-001",
        )
        self.assertEqual(decision.selected_inbox_id, selected)
        self.assertEqual(decision.operator_id, self.simple_accountman)
        self.assertEqual(decision.member_ids, first | selected)
        self.assertTrue(selected.identity_conflict)
        self.assertFalse(selected.identity_review_pending)
        self.assertTrue(first.identity_review_pending)
        self.assertFalse(first.action_process())
        self.assertTrue(selected.action_process(), selected.diagnostic)
        bill = selected.move_id
        self.assertEqual(bill.move_type, "in_invoice")
        self.assertEqual(bill.state, "draft")
        self.assertEqual(bill.amount_untaxed, 100)
        self.assertEqual(bill.amount_tax, 5)
        self.assertEqual(bill.amount_total, 105)
        repeated = selected.action_select_identity_variant(
            decision.justification, decision.review_reference
        )
        self.assertEqual(repeated, decision)
        self.assertTrue(selected.action_process())
        self.assertEqual(selected.move_id, bill)
        self.assertEqual(len(selected.processing_history_ids), 1)
        self.assertFalse(first.move_id)
        self.assertEqual(first.original_xml.content, first_content)
        self.assertEqual(selected.original_xml.content, selected_content)

    def test_internal_review_preserves_authority_and_hidden_original_access(self):
        reader = new_test_user(
            self.env,
            login="synthetic-internal-review-reader",
            groups="account.group_account_readonly",
            company_id=self.env.company.id,
            company_ids=[(6, 0, self.env.company.ids)],
        )
        content = etree.tostring(self._received_test_xml())
        binding = self._reviewed_binding(
            {"name": "Review source access"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "review-access-original", content)
        )
        self.env.company.l10n_ae_inbox_reviewer_id = self.simple_accountman
        action = inbox.with_context(
            default_state="resolved",
            default_requested_by_id=reader.id,
            default_assigned_to_id=reader.id,
            default_resolved_by_id=reader.id,
            default_resolved_at="2000-01-01 00:00:00",
        ).action_request_review()
        task = self.env[action["res_model"]].browse(action["res_id"])
        self.assertEqual(task.requested_by_id, self.simple_accountman)
        self.assertEqual(task.assigned_to_id, self.simple_accountman)
        self.assertEqual(task.state, "open")
        self.assertFalse(task.resolved_by_id)
        self.assertFalse(task.resolved_at)
        self.assertEqual(
            task.with_user(reader).read(["xml_hash"])[0]["xml_hash"], inbox.xml_hash
        )
        for elevated in (False, True):
            current = task.with_user(reader).sudo(elevated)
            with (
                self.subTest(elevated=elevated),
                self.assertRaises(AccessError),
                self.cr.savepoint(),
            ):
                current.action_resolve_review()
            with (
                self.subTest(elevated=elevated),
                self.assertRaises(AccessError),
                self.cr.savepoint(),
            ):
                inbox.with_user(reader).sudo(elevated).action_request_review()
        for values in (
            {"state": "resolved"},
            {"requested_by_id": reader.id},
            {"next_check_at": "2000-01-01 00:00:00"},
        ):
            with self.assertRaises(UserError), self.cr.savepoint():
                task.write(values)
        with self.assertRaises(UserError), self.cr.savepoint():
            task.copy()
        with self.assertRaises(UserError), self.cr.savepoint():
            task.unlink()
        restriction = self.env["ir.access"].create(
            {
                "name": "Synthetic hidden original for internal review",
                "model_id": self.env["ir.model"]._get("l10n_ae.edi.inbox").id,
                "operation": "r",
                "domain": f"[('id', '!=', {inbox.id})]",
            }
        )
        try:
            self.env.invalidate_all()
            with self.assertRaises(AccessError):
                task.with_user(self.simple_accountman).read(["xml_hash"])
            self.assertFalse(
                task.with_user(self.simple_accountman).search([("id", "=", task.id)])
            )
            with self.assertRaises(AccessError), self.cr.savepoint():
                task.with_user(self.simple_accountman).sudo().action_resolve_review()
        finally:
            restriction.unlink()
            self.env.invalidate_all()
        self.assertEqual(inbox.original_xml.content, content)

    def test_revoked_assigned_reviewer_requires_attention_without_reassignment(self):
        reviewer = new_test_user(
            self.env,
            login="synthetic-revoked-incoming-reviewer",
            groups="account.group_account_manager",
            company_id=self.env.company.id,
            company_ids=[(6, 0, self.env.company.ids)],
        )
        content = etree.tostring(self._received_test_xml())
        binding = self._reviewed_binding(
            {"name": "Revoked review owner"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "revoked-review-owner", content)
        )
        self.env.company.l10n_ae_inbox_reviewer_id = reviewer
        action = inbox.action_request_review()
        task = self.env[action["res_model"]].browse(action["res_id"])
        assignment = (task.assigned_to_id.id, task.assigned_by_id.id, task.assigned_at)
        activity_ids = task.activity_ids.ids
        reviewer.active = False
        self.env.company.l10n_ae_inbox_reviewer_id = self.simple_accountman
        with self.assertRaises(ValidationError), self.cr.savepoint():
            inbox.action_request_review()
        self.assertEqual(
            (task.assigned_to_id.id, task.assigned_by_id.id, task.assigned_at),
            assignment,
        )
        self.assertEqual(task.activity_ids.ids, activity_ids)
        self.assertEqual(task.state, "open")
        self.assertEqual(inbox.original_xml.content, content)
        self.assertFalse(inbox.move_id)

    @requires_normative_resources("1.0.4")
    def test_processed_original_without_a_request_cannot_open_a_new_review(self):
        content = etree.tostring(self._received_test_xml())
        binding = self._reviewed_binding(
            {"name": "Already processed original"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id, "already-processed-original", content
            )
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.env.company.l10n_ae_inbox_reviewer_id = self.simple_accountman
        with self.assertRaises(UserError), self.cr.savepoint():
            inbox.action_request_review()
        self.assertFalse(inbox.internal_review_task_ids)
        self.assertEqual(inbox.original_xml.content, content)

    @requires_normative_resources("1.0.4")
    def test_supplier_recovery_resolves_internal_review_without_erasing_history(self):
        content = etree.tostring(self._received_test_xml())
        binding = self._reviewed_binding(
            {"name": "Synthetic incoming review resolution"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        self.partner_a.active = False
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id, "supplier-review-resolution", content
            )
        )
        self.assertFalse(inbox.action_process())
        self.env.company.l10n_ae_inbox_reviewer_id = self.simple_accountman
        action = inbox.action_request_review()
        task = (
            self.env[action["res_model"]]
            .with_user(self.simple_accountman)
            .browse(action["res_id"])
        )
        with self.assertRaises(UserError), self.cr.savepoint():
            task.action_resolve_review()
        requested = (task.requested_by_id.id, task.requested_at)
        assigned = (task.assigned_to_id.id, task.assigned_by_id.id, task.assigned_at)
        unrelated = task.activity_schedule(
            "mail.mail_activity_data_todo",
            user_id=self.simple_accountman.id,
            summary="Separate follow-up requiring a human decision",
        )
        self.partner_a.active = True
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertTrue(task.action_resolve_review())
        self.assertEqual(task.state, "resolved")
        self.assertEqual(task.resolved_by_id, self.simple_accountman)
        self.assertTrue(task.resolved_at)
        resolved = task.resolved_at
        self.assertEqual(task.activity_ids, unrelated)
        self.assertTrue(task.action_resolve_review())
        self.assertEqual(task.resolved_at, resolved)
        self.assertEqual((task.requested_by_id.id, task.requested_at), requested)
        self.assertEqual(
            (task.assigned_to_id.id, task.assigned_by_id.id, task.assigned_at), assigned
        )
        self.assertEqual(inbox.move_id.move_type, "in_invoice")
        self.assertEqual(inbox.move_id.state, "draft")
        self.assertEqual(inbox.move_id.amount_total, 105)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(
            inbox.processing_history_ids.mapped("outcome"), ["review", "processed"]
        )

    @requires_normative_resources("1.0.4")
    def test_archived_supplier_review_requests_one_internal_activity(self):
        content = etree.tostring(self._received_test_xml())
        binding = self._reviewed_binding(
            {"name": "Synthetic incoming review request"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox_model = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        self.partner_a.active = False
        inbox = inbox_model.receive_controlled_document(
            binding.id, "synthetic-review-notification", content
        )
        self.assertFalse(inbox.action_process())
        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)
        self.env.company.write({"l10n_ae_inbox_reviewer_id": self.simple_accountman.id})
        first = inbox.action_request_review()
        repeated = inbox.action_request_review()
        self.assertEqual(first["res_id"], repeated["res_id"])
        self.assertEqual(first["res_model"], "l10n_ae.edi.inbox.task")
        task = self.env[first["res_model"]].browse(first["res_id"])
        self.assertEqual(task.inbox_id, inbox)
        self.assertEqual(task.company_id, self.env.company)
        self.assertEqual(task.requested_by_id, self.simple_accountman)
        self.assertTrue(task.requested_at)
        self.assertEqual(task.xml_hash, inbox.xml_hash)
        self.assertEqual(task.assigned_to_id, self.simple_accountman)
        self.assertEqual(task.assigned_by_id, self.simple_accountman)
        self.assertTrue(task.assigned_at)
        self.assertEqual(len(task.activity_ids), 1)
        self.assertEqual(task.activity_ids.user_id, self.simple_accountman)
        self.assertEqual(task.activity_ids.summary, "Review received UAE document")
        replayed = inbox_model.receive_controlled_document(
            binding.id, "synthetic-review-notification", content
        )
        self.assertEqual(replayed, inbox)
        self.assertEqual(replayed.action_request_review()["res_id"], task.id)
        self.assertEqual(len(task.activity_ids), 1)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)

        assigned_at = task.assigned_at
        task.activity_ids.with_user(self.simple_accountman).action_done()
        self.assertFalse(task.activity_ids)
        self.assertEqual(replayed.action_request_review()["res_id"], task.id)
        self.assertFalse(task.activity_ids)
        self.assertEqual(task.assigned_at, assigned_at)
        self.assertEqual(task.state, "open")
        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)

    @requires_normative_resources("1.0.4")
    def test_combined_free_zone_ecommerce_requires_both_declared_groups(self):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        source.write(
            {
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000004",
                "l10n_ae_fiscal_uuid": "eeeeeee9-1111-4111-8111-111111111111",
            }
        )
        binding = self._reviewed_binding(
            {"name": "Combined Free Zone e-commerce missing declarations"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        for index, missing in enumerate(
            ("Beneficiary", "StreetName", "CityName", "CountrySubentity")
        ):
            with self.subTest(missing=missing):
                tree = self._received_test_xml(source)
                tree.find("cbc:ProfileExecutionID", NS).text = "10000010"
                fiscal_uuid = f"eeeeeeeb-000{index}-4000-8000-111111111111"
                tree.find("cbc:UUID", NS).text = fiscal_uuid
                address = tree.find("cac:Delivery/cac:DeliveryLocation/cac:Address", NS)
                self.assertIsNotNone(address)
                for name, value in (
                    ("StreetName", "Synthetic combined incomplete street"),
                    ("CityName", "Dubai"),
                    ("CountrySubentity", "DXB"),
                ):
                    node = address.find(f"cbc:{name}", NS)
                    self.assertIsNotNone(node)
                    node.text = value
                if missing == "Beneficiary":
                    tree.remove(tree.find("cac:BuyerCustomerParty", NS))
                    rule = "ibr-007-ae"
                else:
                    address.remove(address.find(f"cbc:{missing}", NS))
                    rule = "ibr-142-ae"
                content = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
                inbox = receiver.receive_controlled_document(
                    binding.id, f"combined-missing-{index}", content
                )
                self.assertFalse(inbox.action_process())
                self.assertEqual(inbox.state, "review")
                self.assertFalse(inbox.move_id)
                self.assertFalse(inbox.delivery_snapshot)
                self.assertEqual(inbox.original_xml.content, content)
                self.assertEqual(inbox.xml_hash, hashlib.sha256(content).hexdigest())
                report = json.loads(inbox.processing_history_ids.validation_report)
                self.assertTrue(report["xsd_valid"], report)
                self.assertFalse(report["valid"], report)
                self.assertIn(rule, inbox.diagnostic.lower())
                self.assertEqual(len(inbox.processing_history_ids), 1)
                self.assertEqual(
                    self.env["account.move"].search_count(
                        [
                            ("company_id", "=", self.env.company.id),
                            ("l10n_ae_received_document", "=", True),
                            ("l10n_ae_fiscal_uuid", "=", fiscal_uuid),
                        ]
                    ),
                    0,
                )

    @requires_normative_resources("1.0.4")
    def test_repeated_credit_delivery_keeps_one_refund_and_original_reference(self):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        source.l10n_ae_fiscal_uuid = "bbbbbbbc-1111-4111-8111-111111111111"
        binding = self._reviewed_binding(
            {"name": "Credit delivery events"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        original = receiver.receive_controlled_document(
            binding.id,
            "credit-original",
            etree.tostring(self._received_test_xml(source)),
        )
        self.assertTrue(original.action_process(), original.diagnostic)
        credit = self.init_invoice("out_refund", amounts=[40], taxes=self.tax_sale_a)
        credit.write(
            {
                "reversed_entry_id": source.id,
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "bbbbbbbc-2222-4222-8222-222222222222",
            }
        )
        credit.action_post()
        content = etree.tostring(self._received_test_xml(credit))
        inbox = receiver.receive_controlled_document(
            binding.id, "credit-delivery", content, event_id="credit-event-one"
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        refund = inbox.move_id
        for event_id in ("credit-event-one", "credit-event-two", "credit-event-two"):
            replay = receiver.receive_controlled_document(
                binding.id, "credit-delivery", content, event_id=event_id
            )
            self.assertEqual(replay, inbox)
            self.assertTrue(replay.action_process(), replay.diagnostic)
            self.assertEqual(replay.move_id, refund)
        self.assertEqual(
            (
                refund.move_type,
                refund.state,
                refund.amount_untaxed,
                refund.amount_tax,
                refund.amount_total,
            ),
            ("in_refund", "draft", 40, 2, 42),
        )
        self.assertEqual(refund.reversed_entry_id, original.move_id)
        self.assertEqual(refund.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertEqual(
            inbox.credit_reference_snapshot,
            [
                {
                    "number": source.name,
                    "issue_date": str(source.invoice_date),
                    "move_id": original.move_id.id,
                }
            ],
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(
            inbox.ingestion_event_ids.mapped("event_id"),
            ["credit-event-one", "credit-event-two"],
        )
        self.assertEqual(len(inbox.processing_history_ids), 1)
        self.assertFalse(refund.l10n_ae_transmission_id)

    def test_delivery_evidence_cannot_be_forged_by_defaults_crud_or_elevation(self):
        binding = self._reviewed_binding(
            {"name": "Protected event evidence"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        model = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        original = model.with_context(
            default_event_id="forged-event",
            default_operator_id=1,
            default_occurred_at="2000-01-01 00:00:00",
            default_disposition="collision",
            default_ingestion_conflict=True,
        ).receive_controlled_document(
            binding.id,
            "protected-event-source",
            b"<original/>",
            event_id="actual-event",
        )
        event = original.ingestion_event_ids
        self.assertEqual(event.event_id, "actual-event")
        self.assertEqual(event.operator_id, self.simple_accountman)
        self.assertNotEqual(str(event.occurred_at), "2000-01-01 00:00:00")
        self.assertEqual(event.disposition, "retained")
        self.assertFalse(original.ingestion_conflict)
        values = event.read(
            ["event_id", "inbox_id", "operator_id", "occurred_at", "disposition"]
        )
        for elevated in (False, True):
            target = event.sudo() if elevated else event
            ledger = target.env["l10n_ae.edi.inbox.ingestion"]
            for operation in (
                lambda ledger=ledger: ledger.create({"event_id": "forged"}),
                lambda ledger=ledger: ledger.with_context(
                    default_event_id="forged"
                ).create({}),
                lambda target=target: target.write({"event_id": "forged"}),
                lambda target=target: target.copy(),
                lambda target=target: target.unlink(),
            ):
                with self.env.cr.savepoint(), self.assertRaises(UserError):
                    operation()
        self.assertEqual(
            event.read(
                ["event_id", "inbox_id", "operator_id", "occurred_at", "disposition"]
            ),
            values,
        )
        for invalid in (False, 1, "", "x" * 257, {}):
            with self.subTest(event_id=invalid), self.assertRaises(UserError):
                model.receive_controlled_document(
                    binding.id, "invalid-event", b"<original/>", event_id=invalid
                )
        legacy = model.receive_controlled_document(
            binding.id, "unknown-event", b"<legacy/>"
        )
        self.assertFalse(legacy.ingestion_event_ids)

    def test_event_collision_with_changed_bytes_cannot_be_overridden_by_variant_review(
        self,
    ):
        binding = self._reviewed_binding(
            {"name": "Event versus document identity"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        model = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        tree = self._received_test_xml()
        first = model.receive_controlled_document(
            binding.id, "same-document", etree.tostring(tree), event_id="same-event"
        )
        tree.insert(0, etree.Comment("Retained conflicting delivery bytes"))
        second = model.receive_controlled_document(
            binding.id, "same-document", etree.tostring(tree), event_id="same-event"
        )
        self.assertTrue(first.identity_conflict and second.identity_conflict)
        self.assertTrue(first.ingestion_conflict and second.ingestion_conflict)
        second.action_select_identity_variant(
            "Compared both document variants.", "SYNTH-EVENT-REVIEW"
        )
        self.assertFalse(second.identity_review_pending)
        for original in (first, second):
            self.assertFalse(original.action_process())
            self.assertFalse(original.move_id)
            self.assertFalse(original.processing_history_ids)
        self.assertEqual(first.ingestion_event_ids.disposition, "retained")
        self.assertEqual(second.ingestion_event_ids.disposition, "collision")

    @requires_normative_resources("1.0.4")
    def test_reused_delivery_event_preserves_associations_and_blocks_processing(self):
        binding = self._reviewed_binding(
            {"name": "Conflicting event"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        content = etree.tostring(self._received_test_xml())
        first = receiver.receive_controlled_document(
            binding.id, "original-reference", content, event_id="reused-event"
        )
        self.assertTrue(first.action_process(), first.diagnostic)
        bill = first.move_id
        event = first.ingestion_event_ids
        association = event.read(
            [
                "event_id",
                "inbox_id",
                "remote_id",
                "xml_hash",
                "operator_id",
                "occurred_at",
                "disposition",
            ]
        )
        second = receiver.receive_controlled_document(
            binding.id, "other-reference", content, event_id="reused-event"
        )
        self.assertNotEqual(first, second)
        self.assertTrue(first.ingestion_conflict)
        self.assertTrue(second.ingestion_conflict)
        self.assertEqual((first.state, second.state), ("review", "review"))
        self.assertEqual(
            event.read(
                [
                    "event_id",
                    "inbox_id",
                    "remote_id",
                    "xml_hash",
                    "operator_id",
                    "occurred_at",
                    "disposition",
                ]
            ),
            association,
        )
        self.assertEqual(second.ingestion_event_ids.disposition, "collision")
        for original in (first, second):
            self.assertFalse(original.action_process())
            self.assertEqual(original.original_xml.content, content)
            self.assertEqual(original.ingestion_event_ids.event_id, "reused-event")
        self.assertEqual(first.move_id, bill)
        self.assertFalse(second.move_id)
        self.assertEqual(
            (bill.move_type, bill.state, bill.amount_total),
            ("in_invoice", "draft", 105),
        )
        replay = receiver.receive_controlled_document(
            binding.id, "other-reference", content, event_id="reused-event"
        )
        self.assertEqual(replay, second)
        self.assertEqual(len(second.ingestion_event_ids), 1)
        self.assertEqual(len(first.processing_history_ids), 1)
        self.assertFalse(second.processing_history_ids)

    @requires_normative_resources("1.0.4")
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
        binding = self._reviewed_binding(
            {"name": "Ambiguous received beneficiary"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
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
            inbox = (
                self.env["l10n_ae.edi.inbox"]
                .with_user(self.simple_accountman)
                .receive_controlled_document(
                    binding.id, f"ambiguous-beneficiary-{index}", content
                )
            )
            with self.subTest(path=path):
                self.assertFalse(inbox.action_process())
                self.assertEqual(inbox.state, "review")
                self.assertIn("beneficiary", inbox.diagnostic)
                self.assertFalse(inbox.move_id)
                self.assertEqual(inbox.original_xml.content, content)
                history = inbox.processing_history_ids
                self.assertEqual(len(history), 1)
                report = json.loads(history.validation_report)
                self.assertEqual(
                    history.validation_report_hash,
                    hashlib.sha256(history.validation_report.encode()).hexdigest(),
                )
                if path != "cac:BuyerCustomerParty/cac:Party/cac:PartyIdentification":
                    self.assertFalse(report["xsd_valid"])
                    self.assertFalse(report["valid"])
                self.assertEqual(
                    self.env["account.move"].search_count(
                        [("move_type", "=", "in_invoice")]
                    ),
                    before,
                )

    @requires_normative_resources("1.0.4")
    def test_non_vat_supplier_with_a_different_license_stays_in_review(self):
        source, _purchase_tax, tree = self._commercial_received_fixture()
        tree.find(
            "cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:CompanyID",
            NS,
        ).text = "UNMAPPED-LICENSE-0042"
        content = etree.tostring(tree)
        binding = self._reviewed_binding(
            {"name": "Incoming unknown license"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "unknown-license", content)
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

    def test_unsafe_received_xml_is_preserved_without_creating_a_bill(self):
        content = b'<!DOCTYPE Invoice [<!ENTITY ex SYSTEM "file:///etc/passwd">]><Invoice>&ex;</Invoice>'
        binding = self._reviewed_binding(
            {"name": "Incoming XML check"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "unsafe-xml", content)
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
        binding = self._reviewed_binding(
            {"name": "Incoming access check"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        reader = new_test_user(
            self.env,
            login="uae-inbox-readonly",
            groups="account.group_account_readonly",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "read-only", b"<Invoice/>")
        )
        with self.assertRaises(AccessError):
            inbox.with_user(reader).action_process()
        with self.assertRaises(AccessError):
            self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman).with_user(
                reader
            ).receive_controlled_document(binding.id, "read-only-2", b"<Invoice/>")

    def test_check_refuses_missing_or_unknown_emirate_without_blocking_post(self):
        binding = self._reviewed_binding(
            {
                "name": "Synthetic emirate review",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_check_refuses_missing_or_unkn"
            "own_emirate_without_blocking_post.",
        )
        self.env.company.write(
            {"l10n_ae_edi_auto_prepare": True, "l10n_ae_edi_binding_id": binding.id}
        )
        unknown = self.env["res.country.state"].create(
            {
                "name": "Synthetic unrecognized emirate",
                "code": "ZZ",
                "country_id": self.env.ref("base.ae").id,
            }
        )
        for role, state in (
            ("buyer", self.env["res.country.state"]),
            ("supplier", self.env["res.country.state"]),
            ("buyer", unknown),
        ):
            with self.subTest(role=role, state=state.code):
                party = (
                    self.partner_a if role == "buyer" else self.env.company.partner_id
                )
                party.state_id = state
                invoice = self.init_invoice(
                    "out_invoice", amounts=[100], taxes=self.tax_sale_a
                ).with_user(self.simple_accountman)
                self.assertFalse(invoice.env.su)
                with self.assertRaisesRegex(UserError, "IBR-128-AE"):
                    invoice.action_l10n_ae_validate()
                self.assertFalse(invoice.l10n_ae_transmission_id)
                self.assertFalse(invoice.l10n_ae_fiscal_uuid)
                invoice.action_post()
                self.assertEqual(invoice.state, "posted")
                self.assertEqual(invoice.l10n_ae_preparation_state, "review")
                self.assertIn("IBR-128-AE", invoice.l10n_ae_preparation_error)
                self.assertFalse(invoice.l10n_ae_transmission_id)
                self.assertFalse(invoice.l10n_ae_fiscal_uuid)
                ledger_fields = [
                    "account_id",
                    "debit",
                    "credit",
                    "amount_currency",
                    "tax_ids",
                    "write_date",
                ]
                ledger = invoice.line_ids.sorted("id").read(ledger_fields)
                with self.assertRaisesRegex(UserError, "IBR-128-AE"):
                    invoice.action_l10n_ae_prepare()
                party.state_id = self.env.ref("base.state_ae_du")
                invoice.action_l10n_ae_validate()
                invoice.action_l10n_ae_prepare()
                self.assertEqual(invoice.l10n_ae_preparation_state, "ready")
                self.assertEqual(invoice.amount_total, 105)
                self.assertEqual(invoice.amount_tax, 5)
                self.assertEqual(
                    invoice.line_ids.sorted("id").read(ledger_fields), ledger
                )
                self.assertFalse(invoice.l10n_ae_transmission_id.attempt_ids)
                self.assertFalse(invoice.l10n_ae_transmission_id.event_ids)

    @requires_normative_resources("1.0.4")
    def test_uae_vat_point_date_is_reviewed_in_native_invoice_form(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        binding = self._reviewed_binding(
            {
                "name": "Synthetic native VAT point date review",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_uae_vat_point_date_is_reviewe"
            "d_in_native_invoice_form.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        with Form(invoice) as form:
            form.taxable_supply_date = "2018-12-31"
            form.l10n_ae_binding_id = binding
        self.assertEqual(str(invoice.taxable_supply_date), "2018-12-31")
        invoice.action_post()
        with self.assertRaisesRegex(AssertionError, "readonly field"):
            Form(invoice).taxable_supply_date = "2019-01-02"
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        self.assertEqual(json.loads(issue.payload)["tax_point_date"], "2018-12-31")
        tree = etree.fromstring(issue.xml_content.encode())
        self.assertEqual(tree.findtext("cbc:TaxPointDate", namespaces=NS), "2018-12-31")
        self.assertEqual(invoice.amount_total, 105)
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])

    def test_manual_quote_review_preserves_posted_line_audit_actor(self):
        usd = self.env.ref("base.USD")
        usd.active = True
        self.env["res.currency.rate"].create(
            {
                "currency_id": usd.id,
                "company_id": self.env.company.id,
                "name": "2018-12-31",
                "rate": 1 / 3.6725,
            }
        )
        self.env.company.l10n_ae_edi_auto_prepare = False
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd
        ).with_user(self.simple_accountman)
        invoice.write(
            {"invoice_date": "2019-01-02", "taxable_supply_date": "2019-01-01"}
        )
        with Form(invoice) as form:
            form.l10n_ae_tax_exchange_rate = "3.672500"
            form.l10n_ae_tax_exchange_date = "2019-01-02"
            form.l10n_ae_tax_exchange_source = "SYNTHETIC-UNVERIFIED-QUOTE"
        invoice.action_post()
        # Finish the posting transaction's deferred ORM writes before comparing
        # a separate manager's header edit, as the browser's next request does.
        invoice.env.flush_all()
        audit_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "tax_ids",
            "name",
            "create_date",
            "write_date",
            "write_uid",
        ]
        original = invoice.line_ids.read(audit_fields)
        reviewer = new_test_user(
            self.env,
            login="uae-quote-source-date-reviewer",
            groups="account.group_account_manager,base.group_multi_currency",
        )
        reviewed = invoice.with_user(reviewer)
        self.assertFalse(reviewed.env.su)
        with Form(reviewed) as form:
            form.l10n_ae_tax_exchange_date = "2019-01-01"
        reviewed.env.flush_all()
        self.assertEqual(reviewed.l10n_ae_tax_exchange_date.isoformat(), "2019-01-01")
        self.assertEqual(invoice.line_ids.read(audit_fields), original)
        self.assertEqual(invoice.amount_total_signed, 385.61)
        self.assertFalse(invoice.l10n_ae_transmission_id)

    @requires_normative_resources("1.0.4")
    def test_future_manual_quote_posts_in_review_until_date_repaired(
        self,
    ):
        usd = self.env.ref("base.USD")
        usd.active = True
        self.env["res.currency.rate"].create(
            {
                "currency_id": usd.id,
                "company_id": self.env.company.id,
                "name": "2018-12-31",
                "rate": 1 / 3.6725,
            }
        )
        binding = self._reviewed_binding(
            {
                "name": "Synthetic manual quote source date review",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_future_manual_quote_posts_in_"
            "review_until_date_repaired.",
        )
        self.env.company.write(
            {"l10n_ae_edi_auto_prepare": True, "l10n_ae_edi_binding_id": binding.id}
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        invoice.write(
            {"invoice_date": "2019-01-02", "taxable_supply_date": "2019-01-01"}
        )
        with Form(invoice) as form:
            form.l10n_ae_tax_exchange_rate = "3.672500"
            form.l10n_ae_tax_exchange_date = "2019-01-02"
            form.l10n_ae_tax_exchange_source = "SYNTHETIC-UNVERIFIED-QUOTE"
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.l10n_ae_preparation_state, "review")
        self.assertIn("after the VAT point date", invoice.l10n_ae_preparation_error)
        self.assertFalse(invoice.l10n_ae_transmission_id)
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        displayed = Form(invoice)
        self.assertEqual(displayed.l10n_ae_effective_tax_exchange_origin, "review")
        self.assertIn(
            "after the VAT point date",
            displayed.l10n_ae_effective_tax_exchange_diagnostic,
        )
        for action in (invoice.action_l10n_ae_validate, invoice.action_l10n_ae_prepare):
            with self.assertRaisesRegex(UserError, "after the VAT point date"):
                action()
        ledger = invoice.line_ids.read(
            ["account_id", "debit", "credit", "amount_currency", "tax_ids"]
        )
        with Form(invoice) as form:
            form.l10n_ae_tax_exchange_date = "2019-01-01"
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.l10n_ae_preparation_state, "ready")
        issue = invoice.l10n_ae_transmission_id
        exchange = json.loads(issue.payload)["tax_exchange"]
        self.assertEqual(exchange["rate"], "3.672500")
        self.assertEqual(exchange["date"], "2019-01-01")
        self.assertEqual(exchange["source"], "SYNTHETIC-UNVERIFIED-QUOTE")
        self.assertEqual(json.loads(issue.payload).get("tax_point_date"), "2019-01-01")
        prepared = Form(invoice)
        self.assertEqual(
            prepared.l10n_ae_effective_tax_exchange_origin, "manual_unverified"
        )
        tree = etree.fromstring(issue.xml_content.encode())
        self.assertEqual(tree.findtext("cbc:TaxPointDate", namespaces=NS), "2019-01-01")
        self.assertEqual(
            tree.findtext("cac:TaxExchangeRate/cbc:CalculationRate", namespaces=NS),
            "3.672500",
        )
        self.assertEqual(invoice.amount_total, 105)
        self.assertEqual(invoice.amount_tax_signed, 18.36)
        self.assertEqual(invoice.amount_total_signed, 385.61)
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        frozen = issue.read(["payload", "payload_hash", "xml_content", "xml_hash"])
        for value in ("2019-01-02", False):
            with self.assertRaisesRegex(UserError, "frozen UAE"):
                invoice.write({"taxable_supply_date": value})
        invoice.action_l10n_ae_prepare()
        self.assertEqual(
            issue.read(["payload", "payload_hash", "xml_content", "xml_hash"]), frozen
        )
        self.assertEqual(
            invoice.line_ids.read(
                ["account_id", "debit", "credit", "amount_currency", "tax_ids"]
            ),
            ledger,
        )

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
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "FX quote"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_prepared_foreign_invo"
                    "ice_freezes_exchange_value_date_source_and_xml.",
                ).id,
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

    @requires_normative_resources("1.0.4")
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

    @requires_normative_resources("1.0.4")
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

    @requires_normative_resources("1.0.4")
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

    @requires_normative_resources("1.0.4")
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

    @requires_normative_resources("1.0.4")
    def test_formal_action_reports_missing_uuid_as_a_normative_diagnostic(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        with self.assertRaisesRegex(UserError, "ibr-193-ae"):
            invoice.action_l10n_ae_validate_pint()

    @requires_normative_resources("1.0.4")
    def test_controlled_preparation_assigns_one_uuid_without_preseeded_identity(self):
        binding = self._reviewed_binding(
            {
                "name": "Explicit UUID simulation",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_controlled_preparation_assign"
            "s_one_uuid_without_preseeded_identity.",
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
                "l10n_ae_binding_id": self._reviewed_binding(
                    {
                        "name": "UUID failure simulation",
                        "fiscal_uuid_policy": "controlled_local",
                    },
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_failed_controlled_pre"
                    "paration_leaves_no_assigned_uuid.",
                ).id,
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

    @requires_normative_resources("1.0.4")
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

    @requires_normative_resources("1.0.4")
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

    @requires_normative_resources("1.0.4")
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
        binding = self._reviewed_binding(
            {"name": "Allocated credit test"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_prepared_credit_protects_its_"
            "allocated_original_references.",
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

    @requires_normative_resources("1.0.4")
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

    def test_commercial_standard_vat_posts_but_is_refused_before_freezing(self):
        binding = self._reviewed_binding(
            {
                "name": "Invalid commercial simulation",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_commercial_standard_vat_posts"
            "_but_is_refused_before_freezing.",
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
                move.action_post()
                self.assertEqual(move.state, "posted")
                self.assertEqual(move.l10n_ae_preparation_state, "review")
                self.assertIn("IBR-122-AE", move.l10n_ae_preparation_error)
                with self.assertRaisesRegex(UserError, "IBR-122-AE"):
                    move.action_l10n_ae_prepare()
                self.assertEqual((move.amount_tax, move.amount_total), (5, 105))
                self.assertFalse(move.l10n_ae_fiscal_uuid)
                self.assertFalse(move.l10n_ae_transmission_id)

    @requires_normative_resources("1.0.4")
    def test_registered_supplier_commercial_credit_preserves_outside_scope_category(
        self,
    ):
        self.assertTrue(self.env.company.partner_id.vat)
        self._assert_native_partial_commercial_credit()

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
                # Codes380/381 require an actual item VAT category outside E/O
                # (IBR-151-AE). Keep commercial/self-billing type boundaries
                # separate; a taxless380/381 is not a valid positive fixture.
                taxes = self.tax_sale_a if code in {"380", "381"} else False
                invoice = self.init_invoice(move_type, amounts=[100], taxes=taxes)
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
                if code in {"380", "381"}:
                    self.assertEqual(
                        xml.findtext(
                            f"cac:{root}Line/cac:Item/cac:ClassifiedTaxCategory/cbc:ID",
                            namespaces=NS,
                        ),
                        "S",
                    )
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

    @requires_normative_resources("1.0.4")
    def test_publisher_posting_batch_isolates_missing_agreement_evidence_and_repairs(
        self,
    ):
        binding = self._reviewed_binding(
            {
                "name": "Synthetic publisher batch sandbox",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_publisher_posting_batch_isola"
            "tes_missing_agreement_evidence_and_repairs.",
        )
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": binding.id,
            }
        )
        operator = new_test_user(
            self.env,
            login="synthetic-publisher-batch-writer",
            groups="base.group_user,account.group_account_invoice",
            company_id=self.env.company.id,
            company_ids=[(6, 0, self.env.company.ids)],
        )
        bills = self.env["account.move"]
        agreements = self.env["l10n_ae.selfbilling.agreement"]
        # Independent worksheet: purchases100/200/300 at5% have VAT5/10/15
        # and gross105/210/315. The distinct publishers share the local buyer.
        publishers = [
            ("Alpha", "100000001000003", "1000000010", 100),
            ("Beta", "100000001100003", "1000000011", 200),
            ("Gamma", "100000001200003", "1000000012", 300),
        ]
        for name, vat, endpoint, amount in publishers:
            partner = self.partner_a.copy(
                {
                    "name": f"Synthetic Publisher {name}",
                    "vat": vat,
                    "routing_endpoint": endpoint,
                    "l10n_ae_registration_type": "TL",
                    "l10n_ae_registration_authority": "Synthetic Publisher Authority",
                    "additional_identifiers": {"AE_TL": f"SYNTH-PUBLISHER-{name}-TL"},
                }
            )
            bill = self.init_invoice(
                "in_invoice",
                partner=partner,
                amounts=[amount],
                taxes=self.tax_purchase_a,
            )
            bill.journal_id.is_self_billing = True
            agreement = self.env["l10n_ae.selfbilling.agreement"].create(
                {
                    "name": f"SYNTH-PUBLISHER-{name.upper()}",
                    "partner_id": partner.id,
                    "start_date": bill.invoice_date,
                    "scope": "   " if name == "Beta" else "Reviewed synthetic services",
                    "onboarding_reference": f"SYNTH-ONBOARD-{name.upper()}",
                }
            )
            bill.l10n_ae_selfbilling_agreement_id = agreement
            bills |= bill
            agreements |= agreement
        actor = bills.with_user(operator)
        self.assertFalse(actor.env.su)
        self.assertFalse(actor.env.user.has_group("base.group_system"))
        self.assertFalse(actor.env.user.has_group("account.group_account_manager"))
        actor.action_post()
        self.assertEqual(actor.mapped("state"), ["posted"] * 3)
        self.assertEqual(actor.mapped("amount_total"), [105, 210, 315])
        self.assertEqual(
            actor.mapped("l10n_ae_preparation_state"), ["ready", "review", "ready"]
        )
        invalid = actor[1]
        self.assertIn("self-billing agreement", invalid.l10n_ae_preparation_error)
        self.assertTrue(invalid.l10n_ae_preparation_review_at)
        self.assertFalse(invalid.l10n_ae_transmission_ids)
        self.assertFalse(invalid.l10n_ae_fiscal_uuid)
        with self.assertRaisesRegex(UserError, "self-billing agreement"):
            invalid.action_l10n_ae_prepare()
        ledger = actor.line_ids.read(
            [
                "account_id",
                "partner_id",
                "debit",
                "credit",
                "amount_currency",
                "tax_ids",
            ]
        )
        originals = actor.mapped("l10n_ae_transmission_ids")
        self.assertEqual(len(originals), 2)
        preserved = originals.read(
            ["payload", "payload_hash", "xml_content", "xml_hash", "idempotency_key"]
        )
        with self.assertRaises(AccessError):
            agreements[1].with_user(operator).write({"scope": "Unauthorized repair"})
        agreements[1].with_user(
            self.simple_accountman
        ).scope = "Reviewed synthetic services"
        invalid.action_l10n_ae_prepare()
        invalid.action_l10n_ae_prepare()
        self.assertEqual(invalid.l10n_ae_preparation_state, "ready")
        self.assertFalse(invalid.l10n_ae_preparation_error)
        self.assertFalse(invalid.l10n_ae_preparation_review_at)
        self.assertEqual(len(invalid.l10n_ae_transmission_ids), 1)
        issues = actor.mapped("l10n_ae_transmission_ids")
        self.assertEqual(len(issues), 3)
        self.assertFalse(issues.mapped("attempt_ids"))
        self.assertFalse(issues.mapped("event_ids"))
        self.assertEqual(len(set(issues.mapped("idempotency_key"))), 3)
        self.assertEqual(len(set(actor.mapped("l10n_ae_fiscal_uuid"))), 3)
        for bill, (name, vat, endpoint, _amount), totals in zip(
            actor,
            publishers,
            [
                ("100.00", "5.00", "105.00"),
                ("200.00", "10.00", "210.00"),
                ("300.00", "15.00", "315.00"),
            ],
            strict=True,
        ):
            snapshot = json.loads(bill.l10n_ae_transmission_id.payload)
            self.assertEqual(snapshot["issuer_role"], "buyer")
            self.assertEqual(snapshot["supplier"]["vat"], vat)
            self.assertEqual(snapshot["buyer"]["vat"], "100000000100003")
            self.assertEqual(
                snapshot["selfbilling_agreement"]["reference"],
                f"SYNTH-PUBLISHER-{name.upper()}",
            )
            tree = etree.fromstring(bill.l10n_ae_transmission_id.xml_content.encode())
            for path, expected in {
                "cbc:InvoiceTypeCode": "389",
                "cbc:CustomizationID": "urn:peppol:pint:selfbilling-1@ae-1",
                "cbc:ProfileID": "urn:peppol:bis:selfbilling",
                "cac:AccountingSupplierParty/cac:Party/cbc:EndpointID": endpoint,
                "cac:AccountingCustomerParty/cac:Party/cbc:EndpointID": "1000000001",
                "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount": totals[0],
                "cac:TaxTotal/cbc:TaxAmount": totals[1],
                "cac:LegalMonetaryTotal/cbc:PayableAmount": totals[2],
            }.items():
                self.assertEqual(tree.findtext(path, namespaces=NS), expected, path)
            report = bill.validate_l10n_ae_pint()
            self.assertTrue(report["valid"], report)
            self.assertEqual(report["profile"], "selfbilling")
        self.assertEqual(
            actor.line_ids.read(
                [
                    "account_id",
                    "partner_id",
                    "debit",
                    "credit",
                    "amount_currency",
                    "tax_ids",
                ]
            ),
            ledger,
        )
        self.assertEqual(
            originals.read(
                [
                    "payload",
                    "payload_hash",
                    "xml_content",
                    "xml_hash",
                    "idempotency_key",
                ]
            ),
            preserved,
        )
        all_frozen = issues.read(
            ["payload", "payload_hash", "xml_content", "xml_hash", "idempotency_key"]
        )
        agreements.with_user(self.simple_accountman).write(
            {"scope": "Edited after all preparation", "active": False}
        )
        for bill in actor:
            bill.action_l10n_ae_prepare()
        self.assertEqual(
            issues.read(
                [
                    "payload",
                    "payload_hash",
                    "xml_content",
                    "xml_hash",
                    "idempotency_key",
                ]
            ),
            all_frozen,
        )

    def test_selfbilling_requires_nonblank_agreement_reference_and_onboarding(self):
        bill, agreement = self._selfbilling_bill_with_agreement()
        bill = bill.with_user(self.simple_accountman)
        self.assertFalse(bill.env.su)
        for name in ("name", "onboarding_reference"):
            with self.subTest(field=name):
                original = agreement[name]
                agreement.with_user(self.simple_accountman).write({name: " \t\n "})
                with self.assertRaisesRegex(UserError, "self-billing agreement"):
                    bill.action_l10n_ae_validate()
                self.assertFalse(bill.l10n_ae_transmission_ids)
                agreement.with_user(self.simple_accountman).write({name: original})
                bill.action_l10n_ae_validate()

    @requires_normative_resources("1.0.4")
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

    def test_self_billing_posts_with_review_when_agreement_is_outside_its_term(self):
        bill, agreement = self._selfbilling_bill_with_agreement()
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": bill.l10n_ae_binding_id.id,
            }
        )
        agreement.write({"start_date": "2000-01-01", "end_date": "2000-12-31"})
        bill.action_post()
        self.assertEqual(bill.state, "posted")
        self.assertEqual(bill.l10n_ae_preparation_state, "review")
        self.assertIn("self-billing agreement", bill.l10n_ae_preparation_error)
        with self.assertRaisesRegex(UserError, "self-billing agreement"):
            bill.action_l10n_ae_prepare()
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

    @requires_normative_resources("1.0.4")
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
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Deemed supply destination"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_prepared_deemed_suppl"
                    "y_preserves_effective_and_master_destinations.",
                ).id,
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

    @requires_normative_resources("1.0.4")
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
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Buyer98 reviewed destination"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_prepared_buyer98_free"
                    "zes_review_and_separate_delivery_obligation.",
                ).id,
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
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Buyer98 conventional presentation"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_prepared_buyer98_pres"
                    "erves_native_invoice_html_after_buyer_changes.",
                ).id,
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
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Preserved PDF invoice association"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_pdf_issue_cannot_be_a"
                    "ttached_to_another_invoice.",
                ).id,
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
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Frozen XML invoice association"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_xml_issue_cannot_be_a"
                    "ttached_to_another_invoice.",
                ).id,
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
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Protected buyer98 review"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_prepared_buyer98_revi"
                    "ew_cannot_be_rewritten_by_an_administrator.",
                ).id,
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
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Deemed reporting-only obligations"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_prepared_deemed_suppl"
                    "y_declares_reporting_only_without_c3_obligations.",
                ).id,
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

    @requires_normative_resources("1.0.4")
    def test_prepared_free_zone_document_freezes_scenario_and_beneficiary_facts(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000003",
                "l10n_ae_fiscal_uuid": "aaaaaaad-1111-4111-8111-111111111111",
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Free Zone snapshot"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_prepared_free_zone_do"
                    "cument_freezes_scenario_and_beneficiary_facts.",
                ).id,
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
        binding = self._reviewed_binding(
            {"name": "Synthetic sandbox"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_prepared_xml_survives_later_c"
            "ustomer_address_changes.",
        )
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

    @requires_normative_resources("1.0.4")
    def test_formal_validation_reads_frozen_xml_after_master_data_changes(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_fiscal_uuid = "22222222-2222-4222-8222-222222222222"
        invoice.l10n_ae_binding_id = self._reviewed_binding(
            {"name": "Synthetic frozen validation sandbox"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_formal_validation_reads_froze"
            "n_xml_after_master_data_changes.",
        )
        invoice.action_l10n_ae_prepare()
        self.partner_a.write({"street": False, "city": False})
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
