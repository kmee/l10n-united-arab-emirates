# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import hashlib
import io
import json
import logging
import os
import uuid
import zipfile
from decimal import Decimal
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.l10n_ae_edi.tests.common import (
    CONTROLLED_CAPABILITIES,
)

from ..tools.validation import OfflinePintValidator
from .common import NS, UaePintTestCommon, requires_normative_resources

_logger = logging.getLogger(__name__)


@tagged("post_install", "-at_install")
class TestUaeInvoiceXmlConformance(UaePintTestCommon):
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

    @requires_normative_resources("1.0.4")
    def test_registered_supplier_commercial_credit_preserves_outside_scope_category(
        self,
    ):
        self.assertTrue(self.env.company.partner_id.vat)
        self._assert_native_partial_commercial_credit()

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
