# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
import logging
import os
from decimal import Decimal
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from odoo.addons.l10n_ae_edi.tests.common import (
    CONTROLLED_CAPABILITIES,
)

from ..tools.validation import OfflinePintValidator
from .common import NS, UaePintTestCommon, requires_normative_resources

_logger = logging.getLogger(__name__)


@tagged("post_install", "-at_install")
class TestUaeInvoiceAmounts(UaePintTestCommon):
    @requires_normative_resources("1.0.4")
    def test_summary_native_partial_credit_preserves_reviewed_period_and_original(self):
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
                "name": "Synthetic native Summary partial credit",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_summary_native_partial_credit"
            "_preserves_reviewed_period_and_original.",
        )
        term = self.env["account.payment.term"].create(
            {
                "name": "Synthetic Summary credit monthly sixty-day term",
                "company_id": self.env.company.id,
                "l10n_ae_billing_frequency_code": "MTH",
                "line_ids": [
                    (0, 0, {"value": "percent", "value_amount": 100, "nb_days": 60})
                ],
            }
        )
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(original.env.su)
        original.write(
            {
                "invoice_date": "2026-01-31",
                "invoice_payment_term_id": term.id,
                "l10n_ae_billing_frequency_code": "MTH",
                "l10n_ae_summary": True,
                "l10n_ae_binding_id": binding.id,
                "preferred_payment_method_line_id": False,
                "partner_bank_id": False,
            }
        )
        original.invoice_line_ids.write(
            {"deferred_start_date": "2026-01-01", "deferred_end_date": "2026-01-31"}
        )
        original.action_post()
        original.action_l10n_ae_validate()
        original.action_l10n_ae_prepare()
        self.assertTrue(original.validate_l10n_ae_pint()["valid"])
        immutable = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
        ]
        original_issue = original.l10n_ae_transmission_id
        original_frozen = original_issue.read(immutable)
        wizard = (
            self.env["account.move.reversal"]
            .with_user(self.simple_accountman)
            .with_context(active_model="account.move", active_ids=original.ids)
            .create(
                {
                    "move_ids": [(6, 0, original.ids)],
                    "journal_id": original.journal_id.id,
                    "date": "2026-02-01",
                    "reason": "Synthetic reviewed January partial Summary credit",
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                }
            )
        )
        wizard.refund_moves()
        credit = wizard.new_move_ids
        self.assertEqual(credit.move_type, "out_refund")
        self.assertEqual(credit.reversed_entry_id, original)
        self.assertTrue(credit.l10n_ae_summary)
        self.assertFalse(credit.l10n_ae_invoicing_period_start)
        self.assertFalse(credit.l10n_ae_invoicing_period_end)
        self.assertFalse(credit.l10n_ae_transmission_id)
        self.assertFalse(credit.l10n_ae_fiscal_uuid)
        self.assertFalse(credit.l10n_ae_binding_id)
        self.assertEqual(str(credit.invoice_line_ids.deferred_start_date), "2026-01-01")
        self.assertEqual(str(credit.invoice_line_ids.deferred_end_date), "2026-01-31")
        with Form(credit) as form:
            with form.invoice_line_ids.edit(0) as line:
                line.price_unit = 40
            form.l10n_ae_binding_id = binding
            form.l10n_ae_billing_frequency_code = "MTH"
            form.preferred_payment_method_line_id = self.env[
                "account.payment.method.line"
            ]
            form.partner_bank_id = self.env["res.partner.bank"]
        credit.invoice_line_ids.write({"deferred_end_date": "2026-01-15"})
        credit.action_post()
        self.assertEqual(
            (original.amount_untaxed, original.amount_tax, original.amount_total),
            (100, 5, 105),
        )
        self.assertEqual(
            (credit.amount_untaxed, credit.amount_tax, credit.amount_total), (40, 2, 42)
        )
        self.assertEqual((original.amount_residual, credit.amount_residual), (63, 0))
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
        # Native reversal reconciliation is accounting behavior. UAE replay is
        # compared with the complete ledgers after that native action.
        ledgers = [
            move.line_ids.sorted("id").read(ledger_fields)
            for move in (original, credit)
        ]
        credit.action_l10n_ae_validate()
        credit.action_l10n_ae_prepare()
        issue = credit.l10n_ae_transmission_id
        self.assertNotEqual(issue, original_issue)
        self.assertNotEqual(credit.l10n_ae_fiscal_uuid, original.l10n_ae_fiscal_uuid)
        tree = etree.fromstring(issue.xml_content.encode())
        expected = {
            "cbc:CreditNoteTypeCode": "381",
            "cbc:ProfileExecutionID": "00010000",
            "cac:InvoicePeriod/cbc:StartDate": "2026-01-01",
            "cac:InvoicePeriod/cbc:EndDate": "2026-01-15",
            "cac:InvoicePeriod/cbc:DescriptionCode": "MTH",
            "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID": original.name,
            "cac:BillingReference/cac:InvoiceDocumentReference/cbc:IssueDate": (
                "2026-01-31"
            ),
            "cac:DiscrepancyResponse/cbc:ResponseCode": "DL8.61.1.C",
            "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount": "40.00",
            "cac:TaxTotal/cbc:TaxAmount": "2.00",
            "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount": "42.00",
            "cac:LegalMonetaryTotal/cbc:PrepaidAmount": "42.00",
            "cac:LegalMonetaryTotal/cbc:PayableAmount": "0.00",
        }
        for path, value in expected.items():
            self.assertEqual(tree.findtext(path, namespaces=NS), value, path)
        payload = json.loads(issue.payload)
        self.assertEqual(
            payload["invoicing_period_origin"],
            {
                "source": "native_deferred_lines",
                "lines": [
                    {
                        "id": credit.invoice_line_ids.id,
                        "start_date": "2026-01-01",
                        "end_date": "2026-01-15",
                    }
                ],
            },
        )
        self.assertTrue(credit.validate_l10n_ae_pint()["valid"])
        frozen = issue.read(immutable)
        with self.assertRaisesRegex(UserError, "frozen UAE"):
            credit.invoice_line_ids.write({"deferred_end_date": "2026-01-31"})
        term.l10n_ae_billing_frequency_code = "WKY"
        for move in (original, credit):
            move.action_l10n_ae_prepare()
            self.assertTrue(move.validate_l10n_ae_pint()["valid"])
        self.assertEqual(issue.read(immutable), frozen)
        self.assertEqual(original_issue.read(immutable), original_frozen)
        self.assertEqual(str(original.invoice_line_ids.deferred_end_date), "2026-01-31")
        self.assertEqual(
            [
                move.line_ids.sorted("id").read(ledger_fields)
                for move in (original, credit)
            ],
            ledgers,
        )
        self.assertFalse(issue.attempt_ids or original_issue.attempt_ids)
        self.assertFalse(issue.event_ids or original_issue.event_ids)

    @requires_normative_resources("1.0.4")
    def test_summary_uses_native_deferred_lines_without_inventing_due_dates(self):
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
        term = self.env["account.payment.term"].create(
            {
                "name": "Synthetic deferred Summary sixty-day payment",
                "company_id": self.env.company.id,
                "line_ids": [
                    (0, 0, {"value": "percent", "value_amount": 100, "nb_days": 60})
                ],
            }
        )
        binding = self._reviewed_binding(
            {
                "name": "Synthetic native Summary period",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_summary_uses_native_deferred_"
            "lines_without_inventing_due_dates.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100, 200], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        invoice.write(
            {
                "invoice_date": "2026-01-31",
                "invoice_payment_term_id": term.id,
                "l10n_ae_summary": True,
                "l10n_ae_binding_id": binding.id,
                "preferred_payment_method_line_id": False,
                "partner_bank_id": False,
            }
        )
        first, second = invoice.invoice_line_ids.sorted("id")
        first.write(
            {"deferred_start_date": "2026-01-01", "deferred_end_date": "2026-01-15"}
        )
        second.write(
            {"deferred_start_date": "2026-01-16", "deferred_end_date": "2026-01-31"}
        )
        self.assertFalse(invoice.l10n_ae_invoicing_period_start)
        self.assertFalse(invoice.l10n_ae_invoicing_period_end)
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(str(invoice.invoice_date_due), "2026-04-01")
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (300, 15, 315),
        )
        self.assertFalse(invoice.deferred_move_ids)
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
        periods = tree.findall("cac:InvoicePeriod", NS)
        self.assertEqual(len(periods), 1)
        self.assertEqual(
            periods[0].findtext("cbc:StartDate", namespaces=NS), "2026-01-01"
        )
        self.assertEqual(
            periods[0].findtext("cbc:EndDate", namespaces=NS), "2026-01-31"
        )
        self.assertEqual(tree.findtext("cbc:DueDate", namespaces=NS), "2026-04-01")
        self.assertEqual(
            tree.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=NS),
            "315.00",
        )
        payload = json.loads(issue.payload)
        self.assertEqual(
            payload["invoicing_period"],
            {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        )
        self.assertEqual(
            payload["invoicing_period_origin"],
            {
                "source": "native_deferred_lines",
                "lines": [
                    {
                        "id": first.id,
                        "start_date": "2026-01-01",
                        "end_date": "2026-01-15",
                    },
                    {
                        "id": second.id,
                        "start_date": "2026-01-16",
                        "end_date": "2026-01-31",
                    },
                ],
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
            first.write({"deferred_end_date": "2026-02-15"})
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), frozen[0]["xml_content"])
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        self.assertEqual(issue.read(immutable), frozen)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertFalse(issue.attempt_ids or issue.event_ids)

    @requires_normative_resources("1.0.4")
    def test_reviewed_nine_codes_render_native_aed_invoice_instructions(self):
        # Independent literals from the published AE list; these are ordinary
        # 380/AED representations, not nine complete economic-direction matrices.
        cases = (
            ("1", "Instrument not defined"),
            ("10", "In cash"),
            ("20", "Cheque"),
            ("21", "Banker's draft"),
            ("30", "Credit transfer"),
            ("49", "Direct debit"),
            ("54", "Credit card"),
            ("55", "Debit card"),
            ("68", "Online payment service"),
        )
        self.env.company.l10n_ae_edi_auto_prepare = False
        journal = self.company_data["default_journal_bank"]
        method = journal.inbound_payment_method_line_ids.filtered(
            lambda line: line.code == "manual"
        )[:1]
        self.assertTrue(method)
        index = journal.inbound_payment_method_line_ids.ids.index(method.id)
        account = self.env["res.partner.bank"].create(
            {
                "partner_id": self.env.company.partner_id.id,
                "company_id": self.env.company.id,
                "account_number": "SYNTH NINE 01",
                "allow_out_payment": True,
            }
        )
        binding = self._reviewed_binding(
            {
                "name": "Synthetic reviewed nine-code representation",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_reviewed_nine_codes_render_na"
            "tive_aed_invoice_instructions.",
        )
        immutable = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
        ]
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
        originals = []
        for code, name in cases:
            with self.subTest(code=code):
                with (
                    Form(journal.with_user(self.simple_accountman)) as form,
                    form.inbound_payment_method_line_ids.edit(index) as line,
                ):
                    line.l10n_ae_edi_payment_means_code = code
                invoice = self.init_invoice(
                    "out_invoice", amounts=[100], taxes=self.tax_sale_a
                ).with_user(self.simple_accountman)
                self.assertFalse(invoice.env.su)
                reference = f"SYNTH-CODE-{code}-2026"
                with Form(invoice) as form:
                    form.preferred_payment_method_line_id = method
                    form.partner_bank_id = (
                        account if code == "30" else self.env["res.partner.bank"]
                    )
                    form.payment_reference = reference
                    form.l10n_ae_binding_id = binding
                invoice.action_post()
                self.assertEqual(invoice.state, "posted")
                self.assertEqual(
                    (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
                    (100, 5, 105),
                )
                ledger = invoice.line_ids.sorted("id").read(ledger_fields)
                invoice.action_l10n_ae_validate()
                invoice.action_l10n_ae_prepare()
                issue = invoice.l10n_ae_transmission_id
                tree = etree.fromstring(issue.xml_content.encode())
                self.assertEqual(
                    tree.findtext("cbc:InvoiceTypeCode", namespaces=NS), "380"
                )
                self.assertEqual(
                    tree.findtext("cbc:DocumentCurrencyCode", namespaces=NS), "AED"
                )
                means = tree.findall("cac:PaymentMeans", namespaces=NS)
                self.assertEqual(len(means), 1)
                self.assertEqual(
                    means[0].findtext("cbc:PaymentMeansCode", namespaces=NS), code
                )
                self.assertEqual(
                    means[0].findtext("cbc:PaymentID", namespaces=NS), reference
                )
                if code == "30":
                    self.assertEqual(
                        means[0].findtext(
                            "cac:PayeeFinancialAccount/cbc:ID", namespaces=NS
                        ),
                        "SYNTHNINE01",
                    )
                else:
                    self.assertIsNone(
                        means[0].find("cac:PayeeFinancialAccount", namespaces=NS)
                    )
                for group in ("CardAccount", "PaymentMandate"):
                    self.assertIsNone(means[0].find(f"cac:{group}", namespaces=NS))
                snapshot = json.loads(issue.payload)["payment_means"]
                self.assertEqual(snapshot["code"], code)
                self.assertEqual(snapshot["configured_code"], code)
                self.assertEqual(snapshot["name"], name)
                self.assertEqual(snapshot["method_line_id"], method.id)
                self.assertEqual(snapshot["method_code"], "manual")
                self.assertEqual(snapshot["journal_id"], journal.id)
                self.assertEqual(snapshot["journal_type"], "bank")
                self.assertEqual(
                    snapshot["account_identifier"],
                    "SYNTHNINE01" if code == "30" else None,
                )
                self.assertEqual(
                    (method.code, method.payment_type), ("manual", "inbound")
                )
                self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
                self.assertEqual(
                    invoice.line_ids.sorted("id").read(ledger_fields), ledger
                )
                originals.append((invoice, issue, issue.read(immutable), ledger))
        with (
            Form(journal.with_user(self.simple_accountman)) as form,
            form.inbound_payment_method_line_ids.edit(index) as line,
        ):
            line.l10n_ae_edi_payment_means_code = False
        for invoice, issue, frozen, ledger in originals:
            invoice.action_l10n_ae_prepare()
            self.assertEqual(invoice.preview_l10n_ae_pint(), frozen[0]["xml_content"])
            self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
            self.assertEqual(issue.read(immutable), frozen)
            self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
            self.assertFalse(issue.attempt_ids or issue.event_ids)

    @requires_normative_resources("1.0.4")
    def test_no_selected_method_freezes_undefined_payment_instruction(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        binding = self._reviewed_binding(
            {
                "name": "Synthetic undefined payment instruction",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_no_selected_method_freezes_un"
            "defined_payment_instruction.",
        )
        account = self.env["res.partner.bank"].create(
            {
                "partner_id": self.env.company.partner_id.id,
                "company_id": self.env.company.id,
                "account_number": "SYNTH UNUSED ACCOUNT 01",
                "allow_out_payment": True,
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        with Form(invoice) as form:
            form.preferred_payment_method_line_id = self.env[
                "account.payment.method.line"
            ]
            form.partner_bank_id = account
            form.payment_reference = "SYNTH-UNDEFINED-2026"
            form.l10n_ae_binding_id = binding
        self.assertFalse(invoice.preferred_payment_method_line_id)
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
        self.assertEqual(means[0].findtext("cbc:PaymentMeansCode", namespaces=NS), "1")
        self.assertEqual(
            means[0].findtext("cbc:PaymentID", namespaces=NS), "SYNTH-UNDEFINED-2026"
        )
        for group in ("PayeeFinancialAccount", "CardAccount", "PaymentMandate"):
            self.assertIsNone(means[0].find(f"cac:{group}", namespaces=NS))
        self.assertEqual(
            json.loads(issue.payload)["payment_means"],
            {
                "code": "1",
                "name": "Instrument not defined",
                "payment_reference": "SYNTH-UNDEFINED-2026",
                "method_line_id": None,
                "method_code": None,
                "journal_id": None,
                "journal_type": None,
                "account_id": None,
                "account_identifier": None,
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
        cash = self.company_data["default_journal_cash"]
        method = cash.inbound_payment_method_line_ids.filtered(
            lambda line: line.code == "manual"
        )[:1]
        self.partner_a.property_inbound_payment_method_line_id = method
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), frozen[0]["xml_content"])
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        self.assertEqual(issue.read(immutable), frozen)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertFalse(issue.attempt_ids or issue.event_ids)

    @requires_normative_resources("1.0.3", "1.0.4")
    def test_emitted_tax_credit_exposes_real_official_payment_rule_delta(self):
        # Independent worksheet, no reconciliation: original100+5=105;
        # partial consideration credit40+2=42. Both stay fully open.
        binding = self._reviewed_binding(
            {"name": "Synthetic emitted tax-credit corpus"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_emitted_tax_credit_exposes_re"
            "al_official_payment_rule_delta.",
        )
        packages = [
            ("kmee-pint-ae-1.0.3-ubl-2.1", self.normative_directories["1.0.3"]),
            ("kmee-pint-ae-1.0.4-ubl-2.1", self.normative_directories["1.0.4"]),
        ]
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(original.env.su)
        original.action_post()
        credit = self.init_invoice("out_refund", amounts=[40], taxes=self.tax_sale_a)
        credit.write(
            {
                "l10n_ae_binding_id": binding.id,
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "abababac-3333-4333-8333-333333333333",
                "l10n_ae_credit_reference_ids": [
                    (
                        0,
                        0,
                        {
                            "invoice_id": original.id,
                            "allocated_net": 40,
                            "allocated_tax": 2,
                        },
                    )
                ],
            }
        )
        credit = credit.with_user(self.simple_accountman).with_context(
            allowed_company_ids=[self.env.company.id]
        )
        self.assertFalse(original.env.su)
        self.assertFalse(credit.env.su)
        with patch.dict(
            os.environ,
            {
                "KMEE_UAE_NORMATIVE_RULESET": packages[1][0],
                "KMEE_UAE_NORMATIVE_DIR": packages[1][1],
            },
        ):
            credit.action_post()
            credit.action_l10n_ae_prepare()
            issue = credit.l10n_ae_transmission_id
            public_b = credit.validate_l10n_ae_pint()
            self.assertTrue(public_b["valid"], public_b)
            self.assertEqual(public_b["ruleset"], packages[1][0])
            self.assertEqual(public_b["profile"], "billing")
            self.assertEqual(
                (
                    original.amount_untaxed,
                    original.amount_tax,
                    original.amount_total,
                    original.amount_residual,
                ),
                (100, 5, 105, 105),
            )
            self.assertEqual(
                (
                    credit.amount_untaxed,
                    credit.amount_tax,
                    credit.amount_total,
                    credit.amount_residual,
                ),
                (40, 2, 42, 42),
            )
            frozen = (
                issue.id,
                issue.payload,
                issue.xml_content,
                issue.idempotency_key,
                issue.create_date,
                issue.write_date,
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
            ledgers = [move.line_ids.read(ledger_fields) for move in (original, credit)]
            emitted = issue.xml_content.encode()
            emitted_sha = hashlib.sha256(emitted).hexdigest()
            self.assertEqual(issue.xml_hash, emitted_sha)
            xml = etree.fromstring(emitted)
            self.assertEqual(etree.QName(xml).localname, "CreditNote")
            self.assertEqual(
                xml.findtext("cbc:CreditNoteTypeCode", namespaces=NS), "381"
            )
            self.assertEqual(
                xml.findtext("cbc:ProfileExecutionID", namespaces=NS), "00000000"
            )
            self.assertEqual(
                xml.findtext("cbc:CustomizationID", namespaces=NS),
                "urn:peppol:pint:billing-1@ae-1",
            )
            self.assertEqual(
                xml.findtext("cac:DiscrepancyResponse/cbc:ResponseCode", namespaces=NS),
                "DL8.61.1.C",
            )
            self.assertEqual(
                xml.findtext(
                    "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID",
                    namespaces=NS,
                ),
                original.name,
            )
            self.assertTrue(
                xml.findall("cac:PaymentMeans/cbc:PaymentMeansCode", namespaces=NS)
            )
            for term, expected in [
                ("TaxExclusiveAmount", 40),
                ("TaxInclusiveAmount", 42),
                ("PayableAmount", 42),
            ]:
                self.assertEqual(
                    Decimal(
                        xml.findtext(
                            f"cac:LegalMonetaryTotal/cbc:{term}", namespaces=NS
                        )
                    ),
                    Decimal(expected),
                )
            self.assertEqual(
                Decimal(xml.findtext("cac:TaxTotal/cbc:TaxAmount", namespaces=NS)),
                Decimal(2),
            )
            subtotals = xml.findall("cac:TaxTotal/cac:TaxSubtotal", namespaces=NS)
            self.assertEqual(len(subtotals), 1)
            self.assertEqual(
                subtotals[0].findtext("cac:TaxCategory/cbc:ID", namespaces=NS), "S"
            )
            self.assertEqual(
                Decimal(subtotals[0].findtext("cbc:TaxableAmount", namespaces=NS)),
                Decimal(40),
            )
            self.assertEqual(
                Decimal(subtotals[0].findtext("cbc:TaxAmount", namespaces=NS)),
                Decimal(2),
            )
            self.assertEqual(
                Decimal(
                    subtotals[0].findtext("cac:TaxCategory/cbc:Percent", namespaces=NS)
                ),
                Decimal(5),
            )
            self.assertEqual(
                xml.findtext(
                    "cac:CreditNoteLine/cac:Item/cac:ClassifiedTaxCategory/cbc:ID",
                    namespaces=NS,
                ),
                "S",
            )
            self.assertEqual(
                Decimal(
                    xml.findtext(
                        "cac:CreditNoteLine/cac:ItemPriceExtension/cbc:Amount",
                        namespaces=NS,
                    )
                ),
                Decimal(42),
            )
            reports = []
            for ruleset, directory in [packages[1], packages[0], packages[1]]:
                # Compare the exact native output; never remove PaymentMeans
                # or modify either official package to make the old one pass.
                report = OfflinePintValidator(directory, ruleset=ruleset).validate(
                    emitted
                )
                self.assertTrue(report["xsd_valid"], report)
                self.assertEqual(report["xsd_errors"], [])
                self.assertEqual(report["profile"], "billing")
                self.assertEqual(report["ruleset"], ruleset)
                failures = {
                    failure["code"]
                    for layer in report["layers"]
                    for failure in layer["failures"]
                }
                if ruleset == packages[0][0]:
                    self.assertFalse(report["valid"], report)
                    self.assertEqual(failures, {"ibr-191-ae"}, report)
                else:
                    self.assertTrue(report["valid"], report)
                    self.assertEqual(failures, set())
                reports.append(report)
            self.assertEqual(reports[0], reports[2])
            self.assertEqual(public_b, reports[0])
            self.assertNotEqual(
                reports[0]["resources"]["manifest_sha256"],
                reports[1]["resources"]["manifest_sha256"],
            )
            with patch.dict(
                os.environ,
                {
                    "KMEE_UAE_NORMATIVE_RULESET": packages[0][0],
                    "KMEE_UAE_NORMATIVE_DIR": packages[0][1],
                },
            ):
                with self.assertRaisesRegex(UserError, "(?i)pinned.*package"):
                    credit.validate_l10n_ae_pint()
                self.assertEqual(credit.preview_l10n_ae_pint().encode(), emitted)
                self.assertEqual(credit.l10n_ae_normative_ruleset, packages[1][0])
            self.assertEqual(credit.validate_l10n_ae_pint(), public_b)
            self.assertEqual(
                json.loads(issue.payload)["normative"],
                {
                    "ruleset": packages[1][0],
                    "manifest_sha256": public_b["resources"]["manifest_sha256"],
                },
            )
            credit.action_l10n_ae_prepare()
            self.assertEqual(credit.l10n_ae_transmission_id, issue)
            self.assertEqual(
                (
                    issue.id,
                    issue.payload,
                    issue.xml_content,
                    issue.idempotency_key,
                    issue.create_date,
                    issue.write_date,
                ),
                frozen,
            )
            self.assertEqual(
                [move.line_ids.read(ledger_fields) for move in (original, credit)],
                ledgers,
            )
            self.assertFalse(issue.attempt_ids)
            self.assertFalse(issue.validation_receipt)
            self.assertFalse(issue.remote_id)
            _logger.info(
                "UAE_EMITTED_TAX_CREDIT_VERSION=%s",
                json.dumps(
                    {
                        "code": "381",
                        "category": "S",
                        "original_amounts": [100, 5, 105],
                        "credit_amounts": [40, 2, 42],
                        "credit_payable": 42,
                        "original_residual": 105,
                        "credit_residual": 42,
                        "payment_means_present": True,
                        "xml_sha256": emitted_sha,
                        "operator_sudo": False,
                        "reports": reports,
                        "public_drift_refused": True,
                        "ledger_preserved": True,
                        "scope": "One native tax credit; in-process B/A/B only",
                    },
                    sort_keys=True,
                ),
            )

    @requires_normative_resources("1.0.4")
    def test_usd_invoice_prepares_from_dated_native_rates_without_manual_quote(self):
        usd = self.env.ref("base.USD")
        usd.active = True
        quote = self.env["res.currency.rate"].create(
            {
                "currency_id": usd.id,
                "company_id": self.env.company.id,
                "name": "2018-12-31",
                "rate": 1 / 3.6725,
            }
        )
        binding = self._reviewed_binding(
            {
                "name": "Synthetic dated native FX",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_usd_invoice_prepares_from_dat"
            "ed_native_rates_without_manual_quote.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd
        )
        invoice.l10n_ae_binding_id = binding
        invoice = invoice.with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        invoice.action_post()
        self.assertFalse(invoice.l10n_ae_tax_exchange_rate)
        self.assertFalse(invoice.l10n_ae_tax_exchange_date)
        self.assertFalse(invoice.l10n_ae_tax_exchange_source)
        ledger = invoice.line_ids.read(
            ["account_id", "debit", "credit", "amount_currency", "tax_ids"]
        )
        self.assertEqual(invoice.amount_total, 105)
        self.assertEqual(invoice.amount_tax_signed, 18.36)
        self.assertEqual(invoice.amount_total_signed, 385.61)
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        exchange = json.loads(issue.payload)["tax_exchange"]
        self.assertEqual(exchange["rate"], "3.672500")
        self.assertEqual(exchange["source_currency"], "USD")
        self.assertEqual(exchange["target_currency"], "AED")
        self.assertEqual(exchange["date"], "2018-12-31")
        self.assertIn("Odoo", exchange["source"])
        evidence = exchange["evidence"]
        self.assertEqual(evidence["kind"], "odoo_currency_table")
        self.assertEqual(evidence["conversion_date"], "2019-01-01")
        self.assertEqual(evidence["company_currency"], "AED")
        self.assertFalse(evidence["cbuae_origin_verified"])
        usd_fact = next(f for f in evidence["rates"] if f["currency"] == "USD")
        self.assertEqual(usd_fact["record_id"], quote.id)
        self.assertEqual(usd_fact["date"], "2018-12-31")
        self.assertEqual(usd_fact["company_id"], invoice.company_id.id)
        self.assertEqual(usd_fact["value"], "0.27229407760381213")
        self.assertRegex(evidence["source_sha256"], r"^[0-9a-f]{64}$")
        tree = etree.fromstring(issue.xml_content.encode())
        self.assertEqual(
            tree.findtext("cac:TaxExchangeRate/cbc:CalculationRate", namespaces=NS),
            "3.672500",
        )
        self.assertEqual(
            tree.findtext(
                "cac:InvoiceLine/cac:ItemPriceExtension/cbc:Amount", namespaces=NS
            ),
            "385.6125",
        )
        self.assertEqual(
            tree.findtext(
                "cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']", namespaces=NS
            ),
            "18.36",
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        original = issue.read(["payload", "payload_hash", "xml_content", "xml_hash"])
        quote.rate = 1 / 3.7
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), original[0]["xml_content"])
        self.assertEqual(invoice.l10n_ae_transmission_id, issue)
        self.assertEqual(
            issue.read(["payload", "payload_hash", "xml_content", "xml_hash"]), original
        )
        self.assertEqual(
            invoice.line_ids.read(
                ["account_id", "debit", "credit", "amount_currency", "tax_ids"]
            ),
            ledger,
        )

    @requires_normative_resources("1.0.4")
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
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "FX mismatch"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_an_inverted_quote_can"
                    "not_prepare_a_different_aed_accounting_obligation.",
                ).id,
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

    @requires_normative_resources("1.0.4")
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

    @requires_normative_resources("1.0.4")
    def test_usd_line_discount_preserves_eur_ledger_and_reviewed_aed_quote(self):
        # Independent worksheet: USD100 less10% gives USD90 net, USD4.50 VAT
        # and USD94.50 gross. The accounting quote USD1=EUR0.90 gives EUR81,
        # EUR4.05 and EUR85.05. The separately reviewed AED3.672500/USD quote
        # gives AED330.525 net, AED16.52625 VAT and AED347.05125 gross.
        eur = self.env.ref("base.EUR")
        usd = self.env.ref("base.USD")
        eur.active = usd.active = True
        data = self.setup_other_company(
            name="Synthetic EUR discount ledger company",
            currency_id=eur.id,
        )
        company = data["company"]
        company.write({"l10n_ae_tin": "1000000008", "l10n_ae_tin_confirmed": True})
        company.partner_id.write(
            {
                "vat": "100000000800003",
                "street": "Synthetic EUR discount company street",
                "city": "Dubai",
                "state_id": self.env.ref("base.state_ae_du").id,
                "routing_scheme": "0235",
                "routing_endpoint": "1000000008",
                "l10n_ae_registration_type": "TL",
                "l10n_ae_registration_authority": ("Synthetic EUR Discount Authority"),
                "additional_identifiers": {"AE_TL": "SYNTH-EUR-DISCOUNT-8"},
            }
        )
        self.simple_accountman.write({"company_ids": [(4, company.id)]})
        self.env["res.currency.rate"].create(
            {
                "currency_id": usd.id,
                "company_id": company.id,
                "name": "2019-01-01",
                "rate": 1 / 0.90,
            }
        )
        binding = self._reviewed_binding(
            {
                "name": "Synthetic EUR discount ledger binding",
                "company_id": company.id,
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_usd_line_discount_preserves_e"
            "ur_ledger_and_reviewed_aed_quote.",
        )
        manager_moves = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_company(company)
            .with_context(allowed_company_ids=[company.id])
        )
        invoice = manager_moves.create(
            {
                "move_type": "out_invoice",
                "partner_id": self.partner_a.id,
                "journal_id": data["default_journal_sale"].id,
                "currency_id": usd.id,
                "invoice_date": "2019-01-01",
                "l10n_ae_binding_id": binding.id,
                "l10n_ae_fiscal_uuid": "eeeeeeee-4444-4444-8444-444444444444",
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": "2019-01-01",
                "l10n_ae_tax_exchange_source": "SYNTH-SEPARATE-FISCAL-QUOTE",
                "invoice_line_ids": [
                    (
                        0,
                        0,
                        {
                            "name": "Synthetic EUR-ledger discounted service",
                            "quantity": 1,
                            "price_unit": 100,
                            "discount": 10,
                            "account_id": data["default_account_revenue"].id,
                            "tax_ids": [(6, 0, company.account_sale_tax_id.ids)],
                        },
                    )
                ],
            }
        )
        self.assertFalse(invoice.env.su)
        invoice.action_post()
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (90, 4.50, 94.50),
        )
        self.assertEqual(
            (
                invoice.amount_untaxed_signed,
                invoice.amount_tax_signed,
                invoice.amount_total_signed,
            ),
            (81, 4.05, 85.05),
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
        # First preparation assigns the native transmission link. Preserve the
        # posted ledger through it, then freeze move metadata for validation/replay.
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        move_write_date = invoice.write_date
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
        self.assertEqual(
            json.loads(issue.payload)["tax_exchange"],
            {
                "source_currency": "USD",
                "target_currency": "AED",
                "rate": "3.672500",
                "date": "2019-01-01",
                "source": "SYNTH-SEPARATE-FISCAL-QUOTE",
            },
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["xsd_valid"], report)
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        self.assertEqual(issue.read(evidence_fields), evidence)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertEqual(invoice.write_date, move_write_date)

        xml = etree.fromstring(issue.xml_content.encode())
        self.assertEqual(xml.findtext("cbc:DocumentCurrencyCode", namespaces=NS), "USD")
        self.assertEqual(xml.findtext("cbc:TaxCurrencyCode", namespaces=NS), "AED")
        self.assertIsNone(xml.find("cac:TaxTotal/cbc:TaxAmount[@currencyID='EUR']", NS))
        self.assertEqual(
            xml.findtext("cac:TaxExchangeRate/cbc:CalculationRate", namespaces=NS),
            "3.672500",
        )
        line = xml.find("cac:InvoiceLine", NS)
        self.assertEqual(len(line.findall("cac:AllowanceCharge", NS)), 1)
        allowance = line.find("cac:AllowanceCharge", NS)
        self.assertEqual(
            allowance.findtext("cbc:ChargeIndicator", namespaces=NS), "false"
        )
        self.assertEqual(
            allowance.findtext("cbc:AllowanceChargeReasonCode", namespaces=NS), "95"
        )
        self.assertEqual(
            Decimal(line.findtext("cac:Price/cbc:PriceAmount", namespaces=NS)),
            Decimal(100),
        )
        self.assertEqual(
            Decimal(allowance.findtext("cbc:BaseAmount", namespaces=NS)),
            Decimal(100),
        )
        self.assertEqual(
            Decimal(allowance.findtext("cbc:Amount", namespaces=NS)), Decimal(10)
        )
        self.assertEqual(allowance.find("cbc:Amount", NS).get("currencyID"), "USD")
        for path, expected in [
            ("cbc:LineExtensionAmount", "90"),
            ("cac:ItemPriceExtension/cbc:Amount", "347.05125"),
            (
                "cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount",
                "16.52625",
            ),
        ]:
            self.assertEqual(
                Decimal(line.findtext(path, namespaces=NS)), Decimal(expected)
            )
            self.assertEqual(
                line.find(path, NS).get("currencyID"),
                "USD" if path == "cbc:LineExtensionAmount" else "AED",
            )
        for path, expected, currency in [
            ("cac:TaxTotal/cbc:TaxAmount[@currencyID='USD']", "4.50", "USD"),
            ("cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']", "16.53", "AED"),
            (
                (
                    "cac:AdditionalDocumentReference[cbc:DocumentTypeCode='aedtotal-incl-vat']"
                    "/cbc:DocumentDescription"
                ),
                "347.05",
                None,
            ),
            ("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", "90", "USD"),
            ("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "94.50", "USD"),
            ("cac:LegalMonetaryTotal/cbc:PayableAmount", "94.50", "USD"),
        ]:
            node = xml.find(path, NS)
            self.assertIsNotNone(node, path)
            self.assertEqual(Decimal(node.text), Decimal(expected))
            if currency:
                self.assertEqual(node.get("currencyID"), currency)

        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), issue.xml_content)
        self.assertEqual(issue.read(evidence_fields), evidence)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertEqual(invoice.write_date, move_write_date)
        self.assertFalse(
            issue.attempt_ids
            or issue.event_ids
            or issue.remote_id
            or issue.validation_receipt
        )

    @requires_normative_resources("1.0.4")
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

    @requires_normative_resources("1.0.4")
    def test_fractional_usd_uses_native_aed_totals_without_a_rounding_patch(
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
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        invoice.action_post()
        invoice.write(
            {
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": invoice.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-FRACTIONAL-ROUNDING-REVIEW",
                "l10n_ae_fiscal_uuid": "ddddddda-6666-4666-8666-666666666666",
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Fractional VAT review"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_fractional_usd_vat_di"
                    "vergence_requires_review_without_a_rounding_patch.",
                ).id,
            }
        )
        # Three separate lines: source gross3.87 *3.6725 =14.212575,
        # but native cent-allocated AED balances are VAT0.66 / gross14.22.
        self.assertEqual((invoice.amount_total, invoice.amount_tax), (3.87, 0.18))
        self.assertEqual(
            (invoice.amount_tax_signed, invoice.amount_total_signed), (0.66, 14.22)
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
        invoice.action_l10n_ae_validate()
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        xml = etree.fromstring(issue.xml_content.encode())
        self.assertEqual(
            xml.xpath(
                "./cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']/text()", namespaces=NS
            ),
            ["0.66"],
        )
        self.assertEqual(
            xml.xpath(
                "./cac:AdditionalDocumentReference[cbc:ID='AED']/"
                "cbc:DocumentDescription/text()",
                namespaces=NS,
            ),
            ["14.22"],
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        evidence_fields = [
            "payload",
            "xml_content",
            "idempotency_key",
            "create_date",
            "write_date",
        ]
        original = issue.read(evidence_fields)
        self.env["res.currency.rate"].search(
            [
                ("currency_id", "=", usd.id),
                ("company_id", "=", self.env.company.id),
            ]
        ).write({"rate": 1 / 3.70})
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), issue.xml_content)
        self.assertEqual(issue.read(evidence_fields), original)
        self.assertEqual(invoice.line_ids.read(ledger_fields), ledger)
        self.assertFalse(issue.attempt_ids or issue.event_ids or issue.remote_id)

    @requires_normative_resources("1.0.4")
    def test_small_usd_selfbilling_invoice_and_credit_keep_native_aed_signs(self):
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
        bill, agreement = self._selfbilling_bill_with_agreement()
        bill = bill.with_user(self.simple_accountman)
        bill.write(
            {
                "currency_id": usd.id,
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": bill.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-SELF-FX",
            }
        )
        bill.invoice_line_ids.write({"quantity": 3, "price_unit": 0.10})
        self.assertFalse(bill.env.su)
        bill.action_post()
        bill.action_l10n_ae_prepare()
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "create_date",
            "write_date",
        ]
        source = bill.l10n_ae_transmission_id.read(
            ["payload", "xml_content", "idempotency_key"]
        )
        reversal = (
            self.env["account.move.reversal"]
            .with_user(self.simple_accountman)
            .with_context(
                active_model="account.move",
                active_ids=bill.ids,
            )
            .create(
                {
                    "move_ids": [(6, 0, bill.ids)],
                    "journal_id": bill.journal_id.id,
                    "date": bill.invoice_date,
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                }
            )
        )
        reversal.refund_moves()
        credit = reversal.new_move_ids
        credit.write(
            {
                "l10n_ae_selfbilling_agreement_id": agreement.id,
                "l10n_ae_binding_id": bill.l10n_ae_binding_id.id,
                "l10n_ae_fiscal_uuid": "ddddddda-7777-4777-8777-777777777777",
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": credit.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-SELF-FX-CREDIT",
            }
        )
        credit.action_post()
        for move, kind, code, signed_total in (
            (bill, "Invoice", "389", -1.17),
            (credit, "CreditNote", "261", 1.17),
        ):
            with self.subTest(kind=kind):
                self.assertFalse(move.env.su)
                self.assertEqual(move.amount_total_signed, signed_total)
                ledger = move.line_ids.read(ledger_fields)
                move.action_l10n_ae_validate()
                move.action_l10n_ae_prepare()
                issue = move.l10n_ae_transmission_id
                xml = etree.fromstring(issue.xml_content.encode())
                self.assertEqual(etree.QName(xml).localname, kind)
                self.assertEqual(
                    xml.findtext("cbc:" + kind + "TypeCode", namespaces=NS), code
                )
                self.assertEqual(
                    xml.xpath(
                        "./cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']/text()",
                        namespaces=NS,
                    ),
                    ["0.07"],
                )
                self.assertEqual(
                    xml.xpath(
                        "./cac:AdditionalDocumentReference[cbc:ID='AED']/"
                        "cbc:DocumentDescription/text()",
                        namespaces=NS,
                    ),
                    ["1.17"],
                )
                report = move.validate_l10n_ae_pint()
                self.assertTrue(report["valid"], report)
                evidence = issue.read(
                    ["payload", "xml_content", "idempotency_key", "write_date"]
                )
                move.action_l10n_ae_prepare()
                self.assertEqual(move.preview_l10n_ae_pint(), issue.xml_content)
                self.assertEqual(
                    issue.read(
                        ["payload", "xml_content", "idempotency_key", "write_date"]
                    ),
                    evidence,
                )
                self.assertEqual(move.line_ids.read(ledger_fields), ledger)
        self.assertEqual(
            bill.l10n_ae_transmission_id.read(
                ["payload", "xml_content", "idempotency_key"]
            ),
            source,
        )

    @requires_normative_resources("1.0.4")
    def test_reversed_summary_period_is_refused_before_an_issue_is_frozen(self):
        invoice = (
            self.init_invoice(
                "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
            )
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
        )
        self.assertFalse(invoice.env.su)
        binding = self._reviewed_binding(
            {"name": "Synthetic Summary reversed-period review"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_reversed_summary_period_is_re"
            "fused_before_an_issue_is_frozen.",
        )
        invoice.write(
            {
                "l10n_ae_summary": True,
                "l10n_ae_invoicing_period_start": "2019-01-31",
                "l10n_ae_invoicing_period_end": "2019-01-01",
                "l10n_ae_binding_id": binding.id,
                "l10n_ae_fiscal_uuid": "cccccccc-1111-4111-8111-111111111111",
            }
        )
        invoice_fields = [
            "state",
            "l10n_ae_fiscal_uuid",
            "l10n_ae_invoicing_period_start",
            "l10n_ae_invoicing_period_end",
            "write_date",
        ]
        ledger_fields = [
            "account_id",
            "partner_id",
            "currency_id",
            "date",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "write_date",
        ]
        before = invoice.read(invoice_fields)
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        with self.assertRaisesRegex(UserError, "IBR-029.*end.*start"):
            invoice.action_l10n_ae_prepare()
        self.assertFalse(invoice.l10n_ae_transmission_id)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertEqual(invoice.read(invoice_fields), before)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        invoice.l10n_ae_invoicing_period_end = "2019-01-31"
        invoice.action_l10n_ae_prepare()
        self.assertEqual(len(invoice.l10n_ae_transmission_ids), 1)
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )

    @requires_normative_resources("1.0.4")
    def test_summary_without_period_is_refused_before_an_issue_is_frozen(self):
        invoice = (
            self.init_invoice(
                "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
            )
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
        )
        self.assertFalse(invoice.env.su)
        binding = self._reviewed_binding(
            {"name": "Synthetic Summary missing-period review"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_summary_without_period_is_ref"
            "used_before_an_issue_is_frozen.",
        )
        invoice.write(
            {
                "l10n_ae_summary": True,
                "l10n_ae_binding_id": binding.id,
                "l10n_ae_fiscal_uuid": "cccccccb-1111-4111-8111-111111111111",
            }
        )
        self.assertFalse(invoice.l10n_ae_invoicing_period_start)
        self.assertFalse(invoice.l10n_ae_invoicing_period_end)
        invoice_fields = ["state", "l10n_ae_fiscal_uuid", "write_date"]
        ledger_fields = [
            "account_id",
            "partner_id",
            "currency_id",
            "date",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "write_date",
        ]
        before = invoice.read(invoice_fields)
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        with self.assertRaisesRegex(UserError, "IBR-138-AE.*invoicing period"):
            invoice.action_l10n_ae_prepare()
        self.assertFalse(invoice.l10n_ae_transmission_id)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertEqual(invoice.read(invoice_fields), before)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        invoice.write(
            {
                "l10n_ae_invoicing_period_start": "2019-01-01",
                "l10n_ae_invoicing_period_end": "2019-01-31",
            }
        )
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        self.assertEqual(len(invoice.l10n_ae_transmission_ids), 1)
        self.assertEqual(issue.submission_state, "queued")
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.event_ids)
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )

    @requires_normative_resources("1.0.4")
    def test_usd_billing_batch_preserves_native_fx_and_refuses_unreviewed_tax_date(
        self,
    ):
        company = self.env.company
        usd = self.env.ref("base.USD")
        usd.active = True
        quote = self.env["res.currency.rate"].create(
            {
                "currency_id": usd.id,
                "company_id": company.id,
                "name": "2018-12-31",
                "rate": 1 / 3.6725,
            }
        )
        binding = self._reviewed_binding(
            {
                "name": "Synthetic automatic USD billing",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_usd_billing_batch_preserves_"
            "native_fx_and_refuses_unreviewed_tax_date.",
        )
        billing = (
            self.env["l10n_ae.edi.billing"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[company.id])
        )
        self.assertFalse(billing.env.su)
        identity = {
            "company_id": company.id,
            "environment": "sandbox",
            "process": "billing",
            "origin_system": "SyntheticAutomaticUsdBatch",
        }
        journal = self.env["account.journal"].search(
            [("company_id", "=", company.id), ("type", "=", "sale")], limit=1
        )
        request = {
            "move_type": "out_invoice",
            "partner_id": self.partner_a.id,
            "journal_id": journal.id,
            "currency_id": usd.id,
            "invoice_date": "2019-01-01",
            "l10n_ae_binding_id": binding.id,
            "lines": [
                {
                    "name": "Synthetic dated USD supply",
                    "product_id": self.product_a.id,
                    "quantity": 1,
                    "price_unit": 100,
                    "tax_ids": self.tax_sale_a.ids,
                }
            ],
        }
        original_request = json.dumps(request, sort_keys=True)
        first = billing.submit_document(
            **identity, origin_document_id="USD-valid-first", invoice=request
        )
        with self.assertRaisesRegex(UserError, "VAT point date"):
            billing.submit_document(
                **identity,
                origin_document_id="USD-tax-date-review",
                invoice={**request, "taxable_supply_date": "2018-12-30"},
            )
        second = billing.submit_document(
            **identity, origin_document_id="USD-valid-second", invoice=request
        )
        self.assertNotEqual(
            first["accounting"]["move_id"], second["accounting"]["move_id"]
        )
        originals = []
        for result in (first, second):
            move = billing.env["account.move"].browse(result["accounting"]["move_id"])
            self.assertEqual(
                (move.state, move.amount_untaxed, move.amount_tax, move.amount_total),
                ("posted", 100, 5, 105),
            )
            self.assertEqual(
                (move.amount_tax_signed, move.amount_total_signed), (18.36, 385.61)
            )
            self.assertFalse(move.l10n_ae_tax_exchange_rate)
            self.assertFalse(move.l10n_ae_tax_exchange_date)
            self.assertFalse(move.l10n_ae_tax_exchange_source)
            issue = move.l10n_ae_transmission_id
            exchange = json.loads(issue.payload)["tax_exchange"]
            self.assertEqual(exchange["rate"], "3.672500")
            self.assertEqual(exchange["date"], "2018-12-31")
            self.assertEqual(exchange["evidence"]["conversion_date"], "2019-01-01")
            self.assertFalse(exchange["evidence"]["cbuae_origin_verified"])
            self.assertEqual(len(move.l10n_ae_transmission_ids), 1)
            self.assertFalse(issue.attempt_ids)
            self.assertFalse(issue.event_ids)
            artifact = billing.get_document_artifact(
                **identity, origin_document_id=issue.origin_document_id, revision=1
            )
            xml = etree.fromstring(artifact["content"].encode())
            self.assertEqual(
                xml.findtext("cac:TaxExchangeRate/cbc:CalculationRate", namespaces=NS),
                "3.672500",
            )
            self.assertEqual(
                xml.findtext(
                    "cac:InvoiceLine/cac:ItemPriceExtension/cbc:Amount", namespaces=NS
                ),
                "385.6125",
            )
            report = move.validate_l10n_ae_pint()
            self.assertTrue(report["valid"], report)
            originals.append(
                (
                    move,
                    result,
                    artifact,
                    move.line_ids.read(
                        ["account_id", "debit", "credit", "amount_currency", "tax_ids"]
                    ),
                )
            )
        domain = [
            ("company_id", "=", company.id),
            ("l10n_ae_origin_system", "=", identity["origin_system"]),
        ]
        self.assertEqual(billing.env["account.move"].search_count(domain), 2)
        receipts = billing.env["l10n_ae.edi.billing.request"].search(
            [
                ("company_id", "=", company.id),
                ("origin_system", "=", identity["origin_system"]),
            ]
        )
        self.assertEqual(len(receipts), 2)
        quote.rate = 1 / 3.7
        for move, result, artifact, ledger in originals:
            origin = move.l10n_ae_origin_document_id
            self.assertEqual(
                billing.submit_document(
                    **identity, origin_document_id=origin, invoice=request
                ),
                result,
            )
            self.assertEqual(
                billing.get_document_artifact(
                    **identity, origin_document_id=origin, revision=1
                ),
                artifact,
            )
            self.assertEqual(
                move.line_ids.read(
                    ["account_id", "debit", "credit", "amount_currency", "tax_ids"]
                ),
                ledger,
            )
        self.assertEqual(json.dumps(request, sort_keys=True), original_request)

    @requires_normative_resources("1.0.4")
    def test_billing_summary_period_survives_submission_replay_and_conflict(self):
        company = self.env.company
        journal = self.env["account.journal"].search(
            [("company_id", "=", company.id), ("type", "=", "sale")], limit=1
        )
        binding = self._reviewed_binding(
            {
                "name": "Synthetic Summary billing sandbox",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for "
            "test_billing_summary_period_survives_submission_replay_and_conflict.",
        )
        billing = (
            self.env["l10n_ae.edi.billing"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[company.id])
        )
        self.assertFalse(billing.env.su)
        identity = {
            "company_id": company.id,
            "environment": "sandbox",
            "process": "billing",
            "origin_system": "SyntheticSummaryBilling",
            "origin_document_id": "Reviewed-January-2019",
        }
        # Independent worksheet: AED40+60, VAT2+3, payable105.
        request = {
            "move_type": "out_invoice",
            "partner_id": self.partner_a.id,
            "journal_id": journal.id,
            "currency_id": company.currency_id.id,
            "invoice_date": "2019-01-31",
            "l10n_ae_binding_id": binding.id,
            "l10n_ae_summary": True,
            "l10n_ae_invoicing_period_start": "2019-01-01",
            "l10n_ae_invoicing_period_end": "2019-01-31",
            "lines": [
                {
                    "name": "Synthetic reviewed monthly supply",
                    "product_id": self.product_a.id,
                    "quantity": 1,
                    "price_unit": amount,
                    "tax_ids": self.tax_sale_a.ids,
                }
                for amount in (40, 60)
            ],
        }
        original_request = json.dumps(request, sort_keys=True)
        result = billing.submit_document(**identity, invoice=request)
        self.assertEqual(result["status"], "found")
        self.assertEqual(
            {
                key: result["accounting"][key]
                for key in (
                    "state",
                    "move_type",
                    "amount_untaxed",
                    "amount_tax",
                    "amount_total",
                )
            },
            {
                "state": "posted",
                "move_type": "out_invoice",
                "amount_untaxed": 100,
                "amount_tax": 5,
                "amount_total": 105,
            },
        )
        move = billing.env["account.move"].browse(result["accounting"]["move_id"])
        issue = move.l10n_ae_transmission_id
        self.assertEqual(len(move.l10n_ae_transmission_ids), 1)
        self.assertEqual(issue.submission_state, "queued")
        self.assertEqual(
            json.loads(issue.payload)["invoicing_period"],
            {"start_date": "2019-01-01", "end_date": "2019-01-31"},
        )
        move_fields = [
            "name",
            "state",
            "l10n_ae_fiscal_uuid",
            "l10n_ae_invoicing_period_start",
            "l10n_ae_invoicing_period_end",
            "amount_untaxed",
            "amount_tax",
            "amount_total",
            "create_date",
            "write_date",
        ]
        issue_fields = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
            "submission_state",
            "create_date",
            "write_date",
        ]
        ledger_fields = [
            "account_id",
            "partner_id",
            "currency_id",
            "date",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "write_date",
        ]
        frozen_move = move.read(move_fields)
        frozen_issue = issue.read(issue_fields)
        ledger = move.line_ids.sorted("id").read(ledger_fields)
        artifact = billing.get_document_artifact(**identity, revision=1)
        self.assertEqual(artifact["content"], issue.xml_content)
        self.assertEqual(artifact["sha256"], issue.xml_hash)
        xml = etree.fromstring(artifact["content"].encode())
        self.assertEqual(xml.findtext("cbc:InvoiceTypeCode", namespaces=NS), "380")
        self.assertEqual(
            xml.findtext("cbc:ProfileExecutionID", namespaces=NS), "00010000"
        )
        self.assertEqual(
            xml.findtext("cac:InvoicePeriod/cbc:StartDate", namespaces=NS), "2019-01-01"
        )
        self.assertEqual(
            xml.findtext("cac:InvoicePeriod/cbc:EndDate", namespaces=NS), "2019-01-31"
        )
        report = move.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        self.assertEqual(billing.submit_document(**identity, invoice=request), result)
        self.assertEqual(json.dumps(request, sort_keys=True), original_request)
        self.assertEqual(
            billing.get_document(**identity)["document"], result["document"]
        )
        with self.assertRaisesRegex(UserError, "conflicts with the original"):
            billing.submit_document(
                **identity,
                invoice={**request, "l10n_ae_invoicing_period_end": "2019-01-30"},
            )
        with self.assertRaisesRegex(UserError, "Invalid typed billing field"):
            billing.submit_document(
                **identity,
                invoice={**request, "l10n_ae_invoicing_period_start": "01/01/2019"},
            )
        self.assertEqual(move.read(move_fields), frozen_move)
        self.assertEqual(issue.read(issue_fields), frozen_issue)
        self.assertEqual(move.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertEqual(
            billing.get_document_artifact(**identity, revision=1), artifact
        )
        self.assertEqual(len(move.l10n_ae_transmission_ids), 1)
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.event_ids)
        self.assertFalse(issue.remote_id)

    @requires_normative_resources("1.0.4")
    def test_summary_tax_invoice_serializes_reviewed_invoice_period(self):
        company = self.env.company
        self.assertEqual((company.currency_id.name, self.tax_sale_a.amount), ("AED", 5))
        manager_moves = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[company.id])
        )
        self.assertFalse(manager_moves.env.su)
        journal = self.env["account.journal"].search(
            [("company_id", "=", company.id), ("type", "=", "sale")], limit=1
        )
        binding = self._reviewed_binding(
            {"name": "Synthetic summary invoice period"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic controlled contract for test_summary_tax_invoice_serialize"
            "s_reviewed_invoice_period.",
        )
        invoice = manager_moves.create(
            {
                "move_type": "out_invoice",
                "partner_id": self.partner_a.id,
                "journal_id": journal.id,
                "currency_id": company.currency_id.id,
                "invoice_date": "2019-01-31",
                "l10n_ae_summary": True,
                # These proposed public values make the baseline RED on unknown fields.
                "l10n_ae_invoicing_period_start": "2019-01-01",
                "l10n_ae_invoicing_period_end": "2019-01-31",
                "l10n_ae_binding_id": binding.id,
                "l10n_ae_fiscal_uuid": "ccccccca-1111-4111-8111-111111111111",
                "invoice_line_ids": [
                    (
                        0,
                        0,
                        {
                            "name": "Synthetic monthly supply forty",
                            "product_id": self.product_a.id,
                            "quantity": 1,
                            "price_unit": 40,
                            "tax_ids": [(6, 0, self.tax_sale_a.ids)],
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "name": "Synthetic monthly supply sixty",
                            "product_id": self.product_a.id,
                            "quantity": 1,
                            "price_unit": 60,
                            "tax_ids": [(6, 0, self.tax_sale_a.ids)],
                        },
                    ),
                ],
            }
        )
        self.assertFalse(invoice.env.su)
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
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "write_date",
        ]
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        self.assertEqual(
            json.loads(issue.payload)["invoicing_period"],
            {"start_date": "2019-01-01", "end_date": "2019-01-31"},
        )
        original = (
            issue.id,
            issue.payload,
            issue.xml_content,
            issue.xml_hash,
            issue.idempotency_key,
        )
        invoice_timestamps = invoice.read(["create_date", "write_date"])
        issue_timestamps = issue.read(["create_date", "write_date"])
        xml_text = invoice.preview_l10n_ae_pint()
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        xml = etree.fromstring(xml_text.encode())
        self.assertEqual(xml.findtext("cbc:InvoiceTypeCode", namespaces=NS), "380")
        self.assertEqual(
            xml.findtext("cbc:ProfileExecutionID", namespaces=NS), "00010000"
        )
        self.assertEqual(
            xml.findtext("cac:InvoicePeriod/cbc:StartDate", namespaces=NS),
            "2019-01-01",
        )
        self.assertEqual(
            xml.findtext("cac:InvoicePeriod/cbc:EndDate", namespaces=NS),
            "2019-01-31",
        )
        for path, expected in (
            ("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", "100.00"),
            ("cac:TaxTotal/cbc:TaxAmount", "5.00"),
            ("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "105.00"),
            ("cac:LegalMonetaryTotal/cbc:PayableAmount", "105.00"),
        ):
            self.assertEqual(xml.findtext(path, namespaces=NS), expected)
        invoice_lines = xml.findall("cac:InvoiceLine", namespaces=NS)
        self.assertEqual(
            [
                line.findtext("cbc:LineExtensionAmount", namespaces=NS)
                for line in invoice_lines
            ],
            ["40.00", "60.00"],
        )
        self.assertEqual(
            [
                line.findtext(
                    "cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount", namespaces=NS
                )
                for line in invoice_lines
            ],
            ["2.00", "3.00"],
        )
        self.assertEqual(
            (
                issue.id,
                issue.payload,
                issue.xml_content,
                issue.xml_hash,
                issue.idempotency_key,
            ),
            original,
        )
        self.assertEqual(invoice.preview_l10n_ae_pint(), original[2])
        repeated_report = invoice.validate_l10n_ae_pint()
        self.assertTrue(repeated_report["valid"], repeated_report)
        self.assertEqual(
            (
                issue.id,
                issue.payload,
                issue.xml_content,
                issue.xml_hash,
                issue.idempotency_key,
            ),
            original,
        )
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertEqual(
            invoice.read(["create_date", "write_date"]), invoice_timestamps
        )
        self.assertEqual(issue.read(["create_date", "write_date"]), issue_timestamps)
        for field_name in (
            "l10n_ae_invoicing_period_start",
            "l10n_ae_invoicing_period_end",
        ):
            with self.assertRaisesRegex(UserError, "frozen UAE"):
                invoice.write({field_name: "2019-02-01"})
        self.assertEqual(
            (
                invoice.l10n_ae_invoicing_period_start.isoformat(),
                invoice.l10n_ae_invoicing_period_end.isoformat(),
            ),
            ("2019-01-01", "2019-01-31"),
        )
        self.assertEqual(issue.submission_state, "queued")
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.event_ids)

    @requires_normative_resources("1.0.4")
    def test_summary_tax_invoice_serializes_single_reviewed_period_endpoint(self):
        company = self.env.company
        self.assertEqual((company.currency_id.name, self.tax_sale_a.amount), ("AED", 5))
        manager_moves = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[company.id])
        )
        self.assertFalse(manager_moves.env.su)
        journal = self.env["account.journal"].search(
            [("company_id", "=", company.id), ("type", "=", "sale")], limit=1
        )
        ledger_fields = [
            "account_id",
            "partner_id",
            "currency_id",
            "date",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "write_date",
        ]
        for index, (start, end) in enumerate(
            (("2019-01-01", False), (False, "2019-01-31")), start=1
        ):
            with self.subTest(start=start, end=end):
                binding = self._reviewed_binding(
                    {"name": f"Synthetic one-ended summary period {index}"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic controlled contract for test_summary_tax_invoice_s"
                    "erializes_single_reviewed_period_endpoint.",
                )
                invoice = manager_moves.create(
                    {
                        "move_type": "out_invoice",
                        "partner_id": self.partner_a.id,
                        "journal_id": journal.id,
                        "currency_id": company.currency_id.id,
                        "invoice_date": "2019-01-31",
                        "l10n_ae_summary": True,
                        "l10n_ae_invoicing_period_start": start,
                        "l10n_ae_invoicing_period_end": end,
                        "l10n_ae_binding_id": binding.id,
                        "l10n_ae_fiscal_uuid": (
                            f"cccccc{index:02d}-1111-4111-8111-111111111111"
                        ),
                        "invoice_line_ids": [
                            (
                                0,
                                0,
                                {
                                    "name": "Synthetic monthly supply forty",
                                    "product_id": self.product_a.id,
                                    "quantity": 1,
                                    "price_unit": 40,
                                    "tax_ids": [(6, 0, self.tax_sale_a.ids)],
                                },
                            ),
                            (
                                0,
                                0,
                                {
                                    "name": "Synthetic monthly supply sixty",
                                    "product_id": self.product_a.id,
                                    "quantity": 1,
                                    "price_unit": 60,
                                    "tax_ids": [(6, 0, self.tax_sale_a.ids)],
                                },
                            ),
                        ],
                    }
                )
                self.assertFalse(invoice.env.su)
                invoice.action_post()
                self.assertEqual(
                    (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
                    (100, 5, 105),
                )
                ledger = invoice.line_ids.sorted("id").read(ledger_fields)
                invoice.action_l10n_ae_prepare()
                issue = invoice.l10n_ae_transmission_id
                self.assertEqual(
                    json.loads(issue.payload)["invoicing_period"],
                    {"start_date": start or None, "end_date": end or None},
                )
                frozen_issue = (
                    issue.id,
                    issue.payload,
                    issue.payload_hash,
                    issue.xml_content,
                    issue.xml_hash,
                    issue.idempotency_key,
                )
                invoice_timestamps = invoice.read(["create_date", "write_date"])
                issue_timestamps = issue.read(["create_date", "write_date"])

                xml_text = invoice.preview_l10n_ae_pint()
                report = invoice.validate_l10n_ae_pint()
                self.assertTrue(report["valid"], report)
                self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
                xml = etree.fromstring(xml_text.encode())
                self.assertEqual(
                    xml.findtext("cbc:InvoiceTypeCode", namespaces=NS), "380"
                )
                self.assertEqual(
                    xml.findtext("cbc:ProfileExecutionID", namespaces=NS),
                    "00010000",
                )
                periods = xml.findall("cac:InvoicePeriod", namespaces=NS)
                self.assertEqual(len(periods), 1)
                self.assertEqual(
                    periods[0].findtext("cbc:StartDate", namespaces=NS), start or None
                )
                self.assertEqual(
                    periods[0].findtext("cbc:EndDate", namespaces=NS), end or None
                )
                self.assertEqual(
                    periods[0].find("cbc:StartDate", namespaces=NS) is not None,
                    bool(start),
                )
                self.assertEqual(
                    periods[0].find("cbc:EndDate", namespaces=NS) is not None,
                    bool(end),
                )
                for path, expected in (
                    ("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", "100.00"),
                    ("cac:TaxTotal/cbc:TaxAmount", "5.00"),
                    ("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "105.00"),
                    ("cac:LegalMonetaryTotal/cbc:PayableAmount", "105.00"),
                ):
                    self.assertEqual(xml.findtext(path, namespaces=NS), expected)
                self.assertEqual(issue.xml_content, xml_text)
                self.assertEqual(
                    hashlib.sha256(issue.xml_content.encode()).hexdigest(),
                    issue.xml_hash,
                )
                self.assertEqual(
                    (
                        issue.id,
                        issue.payload,
                        issue.payload_hash,
                        issue.xml_content,
                        issue.xml_hash,
                        issue.idempotency_key,
                    ),
                    frozen_issue,
                )
                self.assertEqual(invoice.preview_l10n_ae_pint(), frozen_issue[3])
                period_field = (
                    "l10n_ae_invoicing_period_start"
                    if start
                    else "l10n_ae_invoicing_period_end"
                )
                with self.assertRaisesRegex(UserError, "frozen UAE"):
                    invoice.write({period_field: "2019-02-01"})
                repeated_report = invoice.validate_l10n_ae_pint()
                self.assertTrue(repeated_report["valid"], repeated_report)
                self.assertEqual(
                    invoice.line_ids.sorted("id").read(ledger_fields), ledger
                )
                self.assertEqual(
                    invoice.read(["create_date", "write_date"]), invoice_timestamps
                )
                self.assertEqual(
                    issue.read(["create_date", "write_date"]), issue_timestamps
                )
                self.assertEqual(issue.submission_state, "queued")
                self.assertFalse(issue.attempt_ids)
                self.assertFalse(issue.event_ids)
