# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import io
import json
import os
import tempfile
import zipfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.l10n_ae_edi.tests.common import (
    review_controlled_binding,
)

from ..tools.validation import OfflinePintValidator
from .common import NS, UaePintTestCommon, requires_normative_resources


@tagged("post_install", "-at_install")
class TestUaeReceivedXml(UaePintTestCommon):
    @requires_normative_resources("1.0.3")
    def test_received_commercial_credit_pins_package_while_original_bill_is_missing(
        self,
    ):
        commercial_source, purchase_tax, _source_tree = (
            self._commercial_received_fixture()
        )
        credit = self.init_invoice(
            "out_refund",
            amounts=[40],
            taxes=commercial_source.invoice_line_ids.tax_ids,
        )
        credit.write(
            {
                "reversed_entry_id": commercial_source.id,
                "l10n_ae_document_category": "commercial",
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "ccccccca-3333-4333-8333-333333333333",
            }
        )
        credit.action_post()
        credit_tree = self._received_test_xml(credit)
        # Historical IBR-191-AE uses equality: type81 requires no PaymentMeans.
        # The current exporter may include it; use independently constructed old XML.
        for payment in credit_tree.findall("cac:PaymentMeans", NS):
            credit_tree.remove(payment)
        self.assertFalse(credit_tree.findall("cac:PaymentMeans", NS))
        content = etree.tostring(credit_tree)
        self.assertEqual(
            credit_tree.findtext("cbc:CreditNoteTypeCode", namespaces=NS), "81"
        )
        self.assertEqual(
            Decimal(
                credit_tree.findtext(
                    "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", namespaces=NS
                )
            ),
            Decimal(40),
        )
        self.assertEqual(
            Decimal(
                credit_tree.findtext(
                    "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", namespaces=NS
                )
            ),
            Decimal(40),
        )
        self.assertEqual(
            Decimal(credit_tree.findtext("cac:TaxTotal/cbc:TaxAmount", namespaces=NS)),
            Decimal(0),
        )

        binding = self._reviewed_binding(
            {"name": "Incoming commercial credit package retention"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox_model = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        inbox = inbox_model.receive_controlled_document(
            binding.id, "commercial-credit-missing-original", content
        )
        original_xml_hash = inbox.xml_hash

        with patch.dict(
            os.environ,
            {
                "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.3-ubl-2.1",
                "KMEE_UAE_NORMATIVE_DIR": self.normative_directories["1.0.3"],
            },
        ):
            self.assertFalse(inbox.action_process())

        self.assertEqual(inbox.state, "review")
        self.assertIn("original supplier bill", inbox.diagnostic.lower())
        self.assertFalse(inbox.move_id)
        history = inbox.processing_history_ids.sorted("id")
        self.assertEqual(len(history), 1)
        first_report_text = history[0].validation_report
        first_report_hash = history[0].validation_report_hash
        first_report = json.loads(first_report_text)
        self.assertTrue(first_report["valid"], first_report)
        self.assertEqual(first_report["ruleset"], "kmee-pint-ae-1.0.3-ubl-2.1")
        self.assertEqual(
            first_report["resources"]["manifest_sha256"],
            "14929fa98465947811ac3a0b30fc62b2a0eba8dadb7712bd6c2bfa11f1363bd0",
        )
        self.assertEqual(
            first_report_hash,
            hashlib.sha256(first_report_text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, original_xml_hash)

        # Repair the missing reference by normal, non-sudo accounting CRUD.
        original_bill = (
            self.env["account.move"]
            .with_user(self.simple_accountman)
            .create(
                {
                    "move_type": "in_invoice",
                    "partner_id": self.partner_a.id,
                    "company_id": self.env.company.id,
                    "currency_id": self.env.company.currency_id.id,
                    "ref": commercial_source.name,
                    "invoice_date": commercial_source.invoice_date,
                    "l10n_ae_document_category": "commercial",
                    "invoice_line_ids": [
                        (
                            0,
                            0,
                            {
                                "name": "Reviewed synthetic commercial original",
                                "product_id": self.product_a.id,
                                "quantity": 1,
                                "price_unit": 100,
                                "tax_ids": [(6, 0, purchase_tax.ids)],
                            },
                        )
                    ],
                }
            )
        )
        self.assertEqual(original_bill.state, "draft")
        self.assertFalse(original_bill.l10n_ae_received_document)
        self.assertEqual(original_bill.company_id, self.env.company)
        self.assertEqual(original_bill.ref, commercial_source.name)
        self.assertEqual(original_bill.invoice_date, commercial_source.invoice_date)
        self.assertEqual(original_bill.l10n_ae_document_category, "commercial")
        self.assertEqual(original_bill.partner_id, self.partner_a)
        self.assertEqual(original_bill.currency_id, self.env.company.currency_id)
        self.assertEqual(
            (
                original_bill.amount_untaxed,
                original_bill.amount_tax,
                original_bill.amount_total,
            ),
            (100, 0, 100),
        )

        # A retry with another installed package must refuse before reconsidering the
        # repaired accounting reference. It adds a diagnostic row, not a report.
        with patch.dict(
            os.environ,
            {
                "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.4-ubl-2.1",
                "KMEE_UAE_NORMATIVE_DIR": self.normative_directories["1.0.4"],
            },
        ):
            self.assertFalse(inbox.action_process())
        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)
        history = inbox.processing_history_ids.sorted("id")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].validation_report, first_report_text)
        self.assertEqual(history[0].validation_report_hash, first_report_hash)
        self.assertFalse(history[1].validation_report)
        self.assertFalse(history[1].validation_report_hash)
        self.assertIn("pinned pint package", history[1].diagnostic.lower())
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, original_xml_hash)

        with patch.dict(
            os.environ,
            {
                "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.3-ubl-2.1",
                "KMEE_UAE_NORMATIVE_DIR": self.normative_directories["1.0.3"],
            },
        ):
            self.assertTrue(inbox.action_process(), inbox.diagnostic)

        history = inbox.processing_history_ids.sorted("id")
        self.assertEqual(history.mapped("outcome"), ["review", "review", "processed"])
        self.assertEqual(history[0].validation_report, first_report_text)
        self.assertEqual(history[0].validation_report_hash, first_report_hash)
        self.assertFalse(history[1].validation_report)
        final_report = json.loads(history[2].validation_report)
        self.assertTrue(final_report["valid"], final_report)
        self.assertEqual(final_report["ruleset"], "kmee-pint-ae-1.0.3-ubl-2.1")
        self.assertEqual(
            final_report["resources"]["manifest_sha256"],
            "14929fa98465947811ac3a0b30fc62b2a0eba8dadb7712bd6c2bfa11f1363bd0",
        )
        self.assertEqual(
            history[2].validation_report_hash,
            hashlib.sha256(history[2].validation_report.encode("utf-8")).hexdigest(),
        )
        refund = inbox.move_id
        self.assertEqual((refund.move_type, refund.state), ("in_refund", "draft"))
        self.assertEqual(refund.partner_id, self.partner_a)
        self.assertEqual(refund.l10n_ae_document_category, "commercial")
        self.assertEqual(refund.reversed_entry_id, original_bill)
        self.assertEqual(refund.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertEqual(refund.invoice_line_ids.tax_ids, purchase_tax)
        self.assertEqual(
            (refund.amount_untaxed, refund.amount_tax, refund.amount_total), (40, 0, 40)
        )
        self.assertEqual(original_bill.amount_total, 100)
        self.assertEqual(original_bill.state, "draft")
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, original_xml_hash)
        self.assertFalse(refund.l10n_ae_transmission_id)
        self.assertEqual(
            self.env["account.move"].search_count(
                [
                    ("company_id", "=", self.env.company.id),
                    ("l10n_ae_received_document", "=", True),
                    ("l10n_ae_fiscal_uuid", "=", credit.l10n_ae_fiscal_uuid),
                    ("move_type", "=", "in_refund"),
                ]
            ),
            1,
        )

        history_fields = [
            "validation_report",
            "validation_report_hash",
            "diagnostic",
            "outcome",
            "move_id",
            "xml_hash",
            "occurred_at",
            "operator_id",
        ]
        history_before_replay = history.read(history_fields)
        with (
            tempfile.TemporaryDirectory(
                prefix="uae-commercial-processed-replay-"
            ) as empty,
            patch.dict(
                os.environ,
                {
                    "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.4-ubl-2.1",
                    "KMEE_UAE_NORMATIVE_DIR": empty,
                },
            ),
        ):
            self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(inbox.move_id, refund)
        self.assertEqual(len(inbox.processing_history_ids), 3)
        self.assertEqual(
            inbox.processing_history_ids.read(history_fields), history_before_replay
        )
        self.assertEqual(
            inbox.processing_history_ids[0].validation_report, first_report_text
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, original_xml_hash)

    @requires_normative_resources("1.0.3")
    def test_received_review_pins_first_formal_normative_package_across_retry(self):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        source.l10n_ae_fiscal_uuid = "18181818-1818-4818-8818-181818181818"
        content = etree.tostring(self._received_test_xml(source))
        binding = self._reviewed_binding(
            {"name": "Pending incoming normative package review"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "pending-package-380", content)
        )
        original_xml_hash = inbox.xml_hash

        with patch.dict(
            os.environ,
            {
                "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.3-ubl-2.1",
                "KMEE_UAE_NORMATIVE_DIR": self.normative_directories["1.0.3"],
            },
        ):
            self.partner_a.active = False
            self.assertFalse(inbox.action_process())

        self.assertEqual(inbox.state, "review")
        self.assertIn("supplier", inbox.diagnostic.lower())
        self.assertFalse(inbox.move_id)
        first_history = inbox.processing_history_ids.sorted("id")
        self.assertEqual(len(first_history), 1)
        first_report_text = first_history.validation_report
        first_report_hash = first_history.validation_report_hash
        first_report = json.loads(first_report_text)
        self.assertTrue(first_report["valid"], first_report)
        self.assertEqual(first_report["ruleset"], "kmee-pint-ae-1.0.3-ubl-2.1")
        self.assertEqual(
            first_report["resources"]["manifest_sha256"],
            "14929fa98465947811ac3a0b30fc62b2a0eba8dadb7712bd6c2bfa11f1363bd0",
        )
        self.assertEqual(
            first_report_hash,
            hashlib.sha256(first_report_text.encode("utf-8")).hexdigest(),
        )
        self.partner_a.active = True

        with patch.dict(
            os.environ,
            {
                "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.4-ubl-2.1",
                "KMEE_UAE_NORMATIVE_DIR": self.normative_directories["1.0.4"],
            },
        ):
            self.assertFalse(inbox.action_process())

        self.assertEqual(inbox.state, "review")
        self.assertIn("pinned pint package", inbox.diagnostic.lower())
        self.assertFalse(inbox.move_id)
        histories = inbox.processing_history_ids.sorted("id")
        self.assertEqual(len(histories), 2)
        self.assertEqual(histories[0].validation_report, first_report_text)
        self.assertEqual(histories[0].validation_report_hash, first_report_hash)
        self.assertFalse(histories[1].validation_report)
        self.assertFalse(histories[1].validation_report_hash)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, original_xml_hash)
        self.assertEqual(
            self.env["account.move"].search_count(
                [
                    ("company_id", "=", self.env.company.id),
                    ("l10n_ae_received_document", "=", True),
                    ("l10n_ae_fiscal_uuid", "=", source.l10n_ae_fiscal_uuid),
                ]
            ),
            0,
        )

        with patch.dict(
            os.environ,
            {
                "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.3-ubl-2.1",
                "KMEE_UAE_NORMATIVE_DIR": self.normative_directories["1.0.3"],
            },
        ):
            self.assertTrue(inbox.action_process(), inbox.diagnostic)

        histories = inbox.processing_history_ids.sorted("id")
        self.assertEqual(histories.mapped("outcome"), ["review", "review", "processed"])
        self.assertEqual(histories[0].validation_report, first_report_text)
        self.assertEqual(histories[0].validation_report_hash, first_report_hash)
        self.assertFalse(histories[1].validation_report)
        self.assertIn("pinned pint package", histories[1].diagnostic.lower())
        final_report = json.loads(histories[2].validation_report)
        self.assertTrue(final_report["valid"], final_report)
        self.assertEqual(final_report["ruleset"], "kmee-pint-ae-1.0.3-ubl-2.1")
        self.assertEqual(
            final_report["resources"]["manifest_sha256"],
            "14929fa98465947811ac3a0b30fc62b2a0eba8dadb7712bd6c2bfa11f1363bd0",
        )
        self.assertEqual(inbox.move_id.move_type, "in_invoice")
        self.assertEqual(inbox.move_id.state, "draft")
        self.assertEqual(inbox.move_id.partner_id, self.partner_a)
        self.assertEqual(
            (
                inbox.move_id.amount_untaxed,
                inbox.move_id.amount_tax,
                inbox.move_id.amount_total,
            ),
            (100, 5, 105),
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, original_xml_hash)
        self.assertFalse(inbox.move_id.l10n_ae_transmission_id)
        self.assertEqual(
            self.env["account.move"].search_count(
                [
                    ("company_id", "=", self.env.company.id),
                    ("l10n_ae_received_document", "=", True),
                    ("l10n_ae_fiscal_uuid", "=", source.l10n_ae_fiscal_uuid),
                ]
            ),
            1,
        )

    @requires_normative_resources("1.0.3")
    def test_received_technical_review_pins_package_before_resources_exist(self):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        source.l10n_ae_fiscal_uuid = "19191919-1919-4919-8919-191919191919"
        content = etree.tostring(self._received_test_xml(source))
        binding = self._reviewed_binding(
            {"name": "Synthetic technical-review package pin"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id, "technical-review-package-pin", content
            )
        )
        self.assertFalse(inbox.env.su)
        self.assertEqual(inbox.env.uid, self.simple_accountman.id)
        self.assertFalse(inbox.env.user._is_admin())
        original_xml_hash = inbox.xml_hash
        history_fields = [
            "normative_ruleset",
            "normative_manifest_hash",
            "validation_report",
            "validation_report_hash",
            "diagnostic",
            "outcome",
            "move_id",
            "xml_hash",
            "occurred_at",
            "operator_id",
            "create_date",
            "write_date",
        ]
        package_manifests = {
            "kmee-pint-ae-1.0.3-ubl-2.1": "normative-1.0.3.json",
            "kmee-pint-ae-1.0.4-ubl-2.1": "normative.json",
        }
        manifest_hashes = {
            ruleset: hashlib.sha256(
                (Path(__file__).resolve().parents[1] / "tools" / filename).read_bytes()
            ).hexdigest()
            for ruleset, filename in package_manifests.items()
        }
        self.assertNotEqual(
            manifest_hashes["kmee-pint-ae-1.0.3-ubl-2.1"],
            manifest_hashes["kmee-pint-ae-1.0.4-ubl-2.1"],
        )

        with (
            tempfile.TemporaryDirectory(prefix="uae-inbound-empty-package-") as empty,
            patch.dict(
                os.environ,
                {
                    "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.3-ubl-2.1",
                    "KMEE_UAE_NORMATIVE_DIR": empty,
                },
            ),
        ):
            self.assertFalse(inbox.action_process())

        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)
        self.assertFalse(inbox.processing_history_ids[-1].validation_report)
        self.assertIn("resource is unavailable", inbox.diagnostic.lower())
        first_history = inbox.processing_history_ids.sorted("id")
        self.assertEqual(len(first_history), 1)
        self.assertEqual(first_history.normative_ruleset, "kmee-pint-ae-1.0.3-ubl-2.1")
        self.assertEqual(
            first_history.normative_manifest_hash,
            manifest_hashes["kmee-pint-ae-1.0.3-ubl-2.1"],
        )
        first_history_before = first_history.read(history_fields)

        with patch.dict(
            os.environ,
            {
                "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.4-ubl-2.1",
                "KMEE_UAE_NORMATIVE_DIR": self.normative_directories["1.0.4"],
            },
        ):
            self.assertFalse(inbox.action_process())

        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)
        self.assertIn("pinned pint package", inbox.diagnostic.lower())
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, original_xml_hash)
        self.assertEqual(first_history.read(history_fields), first_history_before)
        drift_history = inbox.processing_history_ids.sorted("id")[-1]
        self.assertEqual(drift_history.normative_ruleset, "kmee-pint-ae-1.0.4-ubl-2.1")
        self.assertEqual(
            drift_history.normative_manifest_hash,
            manifest_hashes["kmee-pint-ae-1.0.4-ubl-2.1"],
        )
        self.assertFalse(drift_history.validation_report)

        with patch.dict(
            os.environ,
            {
                "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.3-ubl-2.1",
                "KMEE_UAE_NORMATIVE_DIR": self.normative_directories["1.0.3"],
            },
        ):
            self.assertTrue(inbox.action_process(), inbox.diagnostic)

        self.assertEqual(inbox.move_id.move_type, "in_invoice")
        self.assertEqual(inbox.move_id.state, "draft")
        self.assertEqual(
            (
                inbox.move_id.amount_untaxed,
                inbox.move_id.amount_tax,
                inbox.move_id.amount_total,
            ),
            (100, 5, 105),
        )
        successful_history = inbox.processing_history_ids.sorted("id")[-1]
        self.assertEqual(
            successful_history.normative_ruleset, "kmee-pint-ae-1.0.3-ubl-2.1"
        )
        self.assertEqual(
            successful_history.normative_manifest_hash,
            manifest_hashes["kmee-pint-ae-1.0.3-ubl-2.1"],
        )
        successful_report = json.loads(successful_history.validation_report)
        self.assertTrue(successful_report["valid"], successful_report)
        self.assertEqual(successful_report["ruleset"], "kmee-pint-ae-1.0.3-ubl-2.1")
        self.assertEqual(
            successful_report["resources"]["manifest_sha256"],
            manifest_hashes["kmee-pint-ae-1.0.3-ubl-2.1"],
        )
        self.assertEqual(first_history.read(history_fields), first_history_before)
        history_after_success = inbox.processing_history_ids.read(history_fields)
        move_after_success = inbox.move_id.read(
            ["state", "amount_untaxed", "amount_tax", "amount_total", "write_date"]
        )
        ledger_after_success = inbox.move_id.line_ids.read(
            [
                "account_id",
                "partner_id",
                "currency_id",
                "debit",
                "credit",
                "balance",
                "amount_currency",
                "amount_residual",
                "amount_residual_currency",
                "reconciled",
                "create_date",
                "write_date",
            ]
        )

        with patch.dict(
            os.environ,
            {
                "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.4-ubl-2.1",
                "KMEE_UAE_NORMATIVE_DIR": self.normative_directories["1.0.4"],
            },
        ):
            self.assertTrue(inbox.action_process(), inbox.diagnostic)

        self.assertEqual(
            inbox.processing_history_ids.read(history_fields), history_after_success
        )
        self.assertEqual(
            inbox.move_id.read(
                ["state", "amount_untaxed", "amount_tax", "amount_total", "write_date"]
            ),
            move_after_success,
        )
        self.assertEqual(
            inbox.move_id.line_ids.read(
                [
                    "account_id",
                    "partner_id",
                    "currency_id",
                    "debit",
                    "credit",
                    "balance",
                    "amount_currency",
                    "amount_residual",
                    "amount_residual_currency",
                    "reconciled",
                    "create_date",
                    "write_date",
                ]
            ),
            ledger_after_success,
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, original_xml_hash)

    @requires_normative_resources("1.0.3")
    def test_received_processing_retains_the_administrative_package_report(self):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        source.l10n_ae_fiscal_uuid = "17171717-1717-4717-8717-171717171717"
        content = etree.tostring(self._received_test_xml(source))
        binding = self._reviewed_binding(
            {"name": "Historical received package review"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "historical-package-380", content)
        )
        with patch.dict(
            os.environ,
            {
                "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.3-ubl-2.1",
                "KMEE_UAE_NORMATIVE_DIR": self.normative_directories["1.0.3"],
            },
        ):
            self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(len(inbox.processing_history_ids), 1)
        history = inbox.processing_history_ids
        report = json.loads(history.validation_report)
        self.assertTrue(report["valid"])
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.3-ubl-2.1")
        self.assertEqual(
            report["resources"]["manifest_sha256"],
            "14929fa98465947811ac3a0b30fc62b2a0eba8dadb7712bd6c2bfa11f1363bd0",
        )
        with self.assertRaisesRegex(UserError, "immutable"):
            history.write({"validation_report": "{}"})
        original = (
            inbox.xml_hash,
            history.id,
            history.validation_report,
            history.validation_report_hash,
            inbox.move_id.id,
        )
        with (
            tempfile.TemporaryDirectory(prefix="uae-empty-current-normative-") as empty,
            patch.dict(
                os.environ,
                {
                    "KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.4-ubl-2.1",
                    "KMEE_UAE_NORMATIVE_DIR": empty,
                },
            ),
        ):
            self.assertTrue(inbox.action_process())
        self.assertEqual(len(inbox.processing_history_ids), 1)
        self.assertEqual(
            (
                inbox.xml_hash,
                inbox.processing_history_ids.id,
                history.validation_report,
                history.validation_report_hash,
                inbox.move_id.id,
            ),
            original,
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.move_id.state, "draft")
        self.assertEqual(inbox.move_id.move_type, "in_invoice")
        self.assertEqual(
            (
                inbox.move_id.amount_untaxed,
                inbox.move_id.amount_tax,
                inbox.move_id.amount_total,
            ),
            (100, 5, 105),
        )
        self.assertFalse(inbox.move_id.l10n_ae_transmission_id)

    @requires_normative_resources("1.0.4")
    def test_received_agent_free_zone_invoice_and_credit_preserve_principal_and_replay(
        self,
    ):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        original.write(
            {
                "l10n_ae_disclosed_agent": True,
                "l10n_ae_principal_id": "100000000300003",
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000004",
                "l10n_ae_fiscal_uuid": "aaaaaaaf-3333-4333-8333-333333333333",
            }
        )
        binding = self._reviewed_binding(
            {"name": "Received agent"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        original_content = etree.tostring(self._received_test_xml(original))
        inbox = receiver.receive_controlled_document(
            binding.id, "received-agent-invoice", original_content
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        bill = inbox.move_id
        self.assertEqual(
            (bill.move_type, bill.state, bill.amount_tax, bill.amount_total),
            ("in_invoice", "draft", 5, 105),
        )
        self.assertTrue(bill.l10n_ae_disclosed_agent)
        self.assertTrue(bill.l10n_ae_free_zone)
        self.assertEqual(bill.l10n_ae_principal_id, "100000000300003")
        self.assertEqual(bill.l10n_ae_beneficiary_id, "1000000004")
        self.assertEqual(inbox.original_xml.content, original_content)
        self.assertEqual(
            receiver.receive_controlled_document(
                binding.id, "received-agent-invoice", original_content
            ),
            inbox,
        )
        self.assertTrue(inbox.action_process())
        self.assertEqual(inbox.move_id, bill)
        for field, value in (
            ("l10n_ae_principal_id", "100000000400003"),
            ("l10n_ae_disclosed_agent", False),
        ):
            with self.assertRaisesRegex(UserError, "received"):
                bill.write({field: value})
        credit = self.init_invoice("out_refund", amounts=[40], taxes=self.tax_sale_a)
        credit.write(
            {
                "reversed_entry_id": original.id,
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_disclosed_agent": True,
                "l10n_ae_principal_id": "100000000300003",
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000004",
                "l10n_ae_fiscal_uuid": "aaaaaaaf-4444-4444-8444-444444444444",
            }
        )
        credit.action_post()
        self.assertTrue(credit.validate_l10n_ae_pint()["valid"])
        content = etree.tostring(self._received_test_xml(credit))
        refund_inbox = receiver.receive_controlled_document(
            binding.id, "received-agent-credit", content
        )
        self.assertTrue(refund_inbox.action_process(), refund_inbox.diagnostic)
        refund = refund_inbox.move_id
        self.assertEqual(
            (refund.move_type, refund.state, refund.amount_tax, refund.amount_total),
            ("in_refund", "draft", 2, 42),
        )
        self.assertEqual(refund.l10n_ae_principal_id, "100000000300003")
        self.assertTrue(refund.l10n_ae_disclosed_agent)
        self.assertTrue(refund.l10n_ae_free_zone)
        self.assertEqual(refund_inbox.original_xml.content, content)
        self.assertEqual(
            receiver.receive_controlled_document(
                binding.id, "received-agent-credit", content
            ),
            refund_inbox,
        )
        self.assertTrue(refund_inbox.action_process())
        self.assertEqual(refund_inbox.move_id, refund)

    @requires_normative_resources("1.0.4")
    def test_received_principal_defects_retain_original_and_require_review(self):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        source.write(
            {
                "l10n_ae_disclosed_agent": True,
                "l10n_ae_principal_id": "100000000300003",
                "l10n_ae_fiscal_uuid": "aaaaaaaf-5555-4555-8555-555555555555",
            }
        )
        binding = self._reviewed_binding(
            {"name": "Agent principal defects"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        for defect, diagnostic in (
            ("missing", "ibr-137-ae"),
            ("supplier", "ibr-176-ae"),
            ("duplicate", "principal structure"),
            ("format", "Principal ID"),
        ):
            with self.subTest(defect=defect):
                tree = self._received_test_xml(source)
                party = tree.find("cac:SellerSupplierParty/cac:Party", NS)
                identifier = party.find("cac:PartyIdentification", NS)
                if defect == "missing":
                    tree.remove(tree.find("cac:SellerSupplierParty", NS))
                elif defect == "supplier":
                    identifier.find("cbc:ID", NS).text = "100000000200003"
                elif defect == "duplicate":
                    party.append(etree.fromstring(etree.tostring(identifier)))
                else:
                    identifier.find("cbc:ID", NS).text = "synthetic-principal"
                content = etree.tostring(tree)
                inbox = receiver.receive_controlled_document(
                    binding.id, f"agent-defect-{defect}", content
                )
                self.assertFalse(inbox.action_process())
                self.assertIn(diagnostic, inbox.diagnostic)
                self.assertEqual(inbox.state, "review")
                self.assertFalse(inbox.move_id)
                self.assertEqual(inbox.original_xml.content, content)

    def test_received_package_preserves_every_identity_selection_and_original(self):
        binding = self._reviewed_binding(
            {"name": "Portable identity review"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        tree = self._received_test_xml()
        first_bytes = etree.tostring(tree)
        first = receiver.receive_controlled_document(
            binding.id, "portable-identity", first_bytes
        )
        tree.insert(0, etree.Comment("Synthetic portable identity variant"))
        second_bytes = etree.tostring(tree)
        selected = receiver.receive_controlled_document(
            binding.id, "portable-identity", second_bytes
        )
        first_decision = first.action_select_identity_variant(
            "Reviewed first original.", "SYNTH-PORTABLE-001"
        )
        last_decision = selected.action_select_identity_variant(
            "Reviewed second original.", "SYNTH-PORTABLE-002"
        )
        auditor = new_test_user(
            self.env,
            login="synthetic-identity-package-auditor",
            groups="l10n_ae_edi.group_fiscal_auditor,base.group_allow_export",
            company_id=self.env.company.id,
            company_ids=self.env.company.ids,
        )
        audit = first.with_user(auditor)
        with zipfile.ZipFile(io.BytesIO(audit.evidence_archive.content)) as archive:
            self.assertIn("identity-review.json", archive.namelist())
            review = json.loads(archive.read("identity-review.json"))
            self.assertEqual(review["format"], "kmee-uae-identity-review-1")
            self.assertEqual(
                review["originals"],
                [
                    {
                        "inbox_id": first.id,
                        "xml_hash": hashlib.sha256(first_bytes).hexdigest(),
                    },
                    {
                        "inbox_id": selected.id,
                        "xml_hash": hashlib.sha256(second_bytes).hexdigest(),
                    },
                ],
            )
            self.assertEqual(review["selected_inbox_id"], selected.id)
            self.assertTrue(review["review_pending"])
            self.assertEqual(
                [row["id"] for row in review["decisions"]],
                [first_decision.id, last_decision.id],
            )
            self.assertEqual(
                [row["review_reference"] for row in review["decisions"]],
                ["SYNTH-PORTABLE-001", "SYNTH-PORTABLE-002"],
            )
            for decision in review["decisions"]:
                self.assertEqual(decision["operator_id"], self.simple_accountman.id)
                self.assertTrue(decision["occurred_at"])
                self.assertEqual(decision["member_snapshot"], review["originals"])
                self.assertEqual(decision["member_ids"], [first.id, selected.id])
            self.assertEqual(archive.read("document.xml"), first_bytes)
            nested_bytes = archive.read(f"identity-originals/{selected.id}.zip")
            with zipfile.ZipFile(io.BytesIO(nested_bytes)) as nested:
                self.assertEqual(nested.read("document.xml"), second_bytes)
                self.assertNotIn("identity-review.json", nested.namelist())
                self.assertEqual(json.loads(nested.read("processing-history.json")), [])
                self.assertIn("retention.json", nested.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(
                manifest["relationships"]["identity_decisions"],
                [first_decision.id, last_decision.id],
            )
            self.assertEqual(
                manifest["relationships"]["identity_originals"], [first.id, selected.id]
            )
            for name, metadata in manifest["artifacts"].items():
                content = archive.read(name)
                self.assertEqual(
                    metadata,
                    {
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    },
                )
        self.assertFalse((first | selected).move_id)
        self.assertFalse((first | selected).processing_history_ids)

    def test_received_identity_conflict_includes_an_unreadable_original(self):
        binding = self._reviewed_binding(
            {"name": "Restricted original identity"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        first = receiver.receive_controlled_document(
            binding.id, "restricted-same-id", b"<original/>"
        )
        restriction = self.env["ir.access"].create(
            {
                "name": "Synthetic restricted receipt original",
                "model_id": self.env["ir.model"]._get("l10n_ae.edi.inbox").id,
                "operation": "r",
                "domain": f"[('id', '!=', {first.id})]",
            }
        )
        try:
            self.env.invalidate_all()
            changed = receiver.receive_controlled_document(
                binding.id, "restricted-same-id", b"<changed/>"
            )
            self.assertEqual(changed.state, "review")
            self.assertTrue(changed.identity_conflict)
            self.assertFalse(changed.action_process())
            with self.assertRaises(AccessError), self.cr.savepoint():
                receiver.receive_controlled_document(
                    binding.id, "restricted-same-id", b"<original/>"
                )
            with self.assertRaises(AccessError):
                first.read(["original_xml"])
        finally:
            restriction.unlink()
            self.env.invalidate_all()
        originals = receiver.search(
            [("binding_id", "=", binding.id), ("remote_id", "=", "restricted-same-id")]
        )
        self.assertEqual(originals, first | changed)
        self.assertEqual(originals.mapped("state"), ["review", "review"])
        self.assertTrue(all(originals.mapped("identity_conflict")))
        self.assertEqual(first.original_xml.content, b"<original/>")
        self.assertEqual(changed.original_xml.content, b"<changed/>")
        self.assertFalse(originals.move_id)

    @requires_normative_resources("1.0.4")
    def test_received_usd_partial_credit_preserves_aed_facts_and_original_reference(
        self,
    ):
        tree, _rate, source = self._foreign_received_fixture()
        binding = self._reviewed_binding(
            {"name": "Incoming USD credit"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox_model = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
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

    @requires_normative_resources("1.0.4")
    def test_received_inactive_document_currency_stays_in_review(self):
        tree, _rate, _source = self._foreign_received_fixture()
        self.env.ref("base.CHF").active = False
        tree.find("cbc:DocumentCurrencyCode", NS).text = "CHF"
        tree.find("cac:TaxExchangeRate/cbc:SourceCurrencyCode", NS).text = "CHF"
        for node in tree.xpath("//*[@currencyID='USD']"):
            node.set("currencyID", "CHF")
        binding = self._reviewed_binding(
            {"name": "Inactive CHF"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id,
                "inactive-chf",
                etree.tostring(tree),
            )
        )
        self.assertFalse(inbox.action_process())
        self.assertEqual(inbox.state, "review")
        self.assertIn("document currency", inbox.diagnostic)
        self.assertFalse(inbox.move_id)
        self.assertFalse(self.env.ref("base.CHF").active)

    @requires_normative_resources("1.0.4")
    def test_received_usd_bill_preserves_sender_aed_facts_separately_from_local_review(
        self,
    ):
        tree, rate, _source = self._foreign_received_fixture()
        content = etree.tostring(tree)
        binding = self._reviewed_binding(
            {"name": "Incoming USD evidence"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id,
                "received-usd-1",
                content,
            )
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
        self.assertEqual(
            bill.l10n_ae_received_aed_comparison["gross_declared"], "385.61"
        )
        self.assertFalse(bill.l10n_ae_received_aed_review)
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

    @requires_normative_resources("1.0.4")
    def test_received_ecommerce_preserves_declared_delivery_separately_from_partners(
        self,
    ):
        tree = self._received_test_xml()
        tree.find("cbc:ProfileExecutionID", NS).text = "00000010"
        tree.find("cbc:UUID", NS).text = "eeeeeeec-1111-4111-8111-111111111111"
        delivery = tree.find("cac:Delivery", NS)
        self.assertIsNotNone(delivery)
        location = delivery.find("cac:DeliveryLocation", NS)
        self.assertIsNotNone(location)
        date = delivery.find("cbc:ActualDeliveryDate", NS)
        if date is None:
            date = etree.Element(etree.QName(NS["cbc"], "ActualDeliveryDate"))
            delivery.insert(0, date)
        date.text = "2019-01-01"
        location_id = location.find("cbc:ID", NS)
        if location_id is None:
            location_id = etree.Element(etree.QName(NS["cbc"], "ID"))
            location.insert(0, location_id)
        location_id.text = "5790000435963"
        location_id.set("schemeID", "0088")
        party = delivery.find("cac:DeliveryParty", NS)
        if party is None:
            party = etree.SubElement(delivery, etree.QName(NS["cac"], "DeliveryParty"))
        identifier = etree.Element(etree.QName(NS["cac"], "PartyIdentification"))
        party.insert(0, identifier)
        etree.SubElement(
            identifier, etree.QName(NS["cbc"], "ID"), schemeID="0235"
        ).text = "1000000004"
        old_address = location.find("cac:Address", NS)
        self.assertIsNotNone(old_address)
        location.remove(old_address)
        address = etree.SubElement(location, etree.QName(NS["cac"], "Address"))
        for name, value in (
            ("StreetName", "Synthetic delivery-only street"),
            ("AdditionalStreetName", "Synthetic dock seven"),
            ("CityName", "Dubai"),
            ("PostalZone", "00000"),
            ("CountrySubentity", "DXB"),
        ):
            etree.SubElement(address, etree.QName(NS["cbc"], name)).text = value
        line = etree.SubElement(address, etree.QName(NS["cac"], "AddressLine"))
        etree.SubElement(
            line, etree.QName(NS["cbc"], "Line")
        ).text = "Synthetic warehouse seven"
        country = etree.SubElement(address, etree.QName(NS["cac"], "Country"))
        etree.SubElement(
            country, etree.QName(NS["cbc"], "IdentificationCode")
        ).text = "AE"
        content = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
        binding = self._reviewed_binding(
            {"name": "Received e-commerce delivery"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        inbox = receiver.receive_controlled_document(
            binding.id, "ecommerce-delivery-one", content
        )
        processed = inbox.action_process()
        report = json.loads(inbox.processing_history_ids.validation_report)
        # The gate, not a malformed fixture, must account for the first RED.
        self.assertTrue(report["xsd_valid"], report)
        self.assertTrue(report["valid"], report)
        self.assertTrue(processed, inbox.diagnostic)
        bill = inbox.move_id
        self.assertEqual((bill.move_type, bill.state), ("in_invoice", "draft"))
        self.assertEqual(bill.partner_id, self.partner_a)
        self.assertEqual(bill.company_id, self.env.company)
        self.assertTrue(bill.l10n_ae_ecommerce)
        self.assertEqual(
            (bill.amount_untaxed, bill.amount_tax, bill.amount_total), (100, 5, 105)
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(
            bill.l10n_ae_fiscal_uuid, "eeeeeeec-1111-4111-8111-111111111111"
        )
        expected_address = {
            "street_name": "Synthetic delivery-only street",
            "additional_street_name": "Synthetic dock seven",
            "address_lines": ["Synthetic warehouse seven"],
            "city_name": "Dubai",
            "postal_zone": "00000",
            "country_subentity": "DXB",
            "country_subentity_code": False,
            "country_code": "AE",
            "country_name": False,
        }
        self.assertEqual(inbox.delivery_snapshot["address"], expected_address)
        self.assertEqual(inbox.delivery_snapshot["actual_delivery_date"], "2019-01-01")
        self.assertEqual(
            inbox.delivery_snapshot["location_identifiers"],
            [{"value": "5790000435963", "attributes": {"schemeID": "0088"}}],
        )
        self.assertEqual(
            inbox.delivery_snapshot["party_identifiers"],
            [{"value": "1000000004", "attributes": {"schemeID": "0235"}}],
        )
        preserved_delivery = etree.fromstring(inbox.delivery_snapshot["delivery_xml"])
        self.assertEqual(
            etree.tostring(preserved_delivery, method="c14n"),
            etree.tostring(delivery, method="c14n"),
        )
        self.assertIn("Synthetic delivery-only street", inbox.delivery_summary)
        self.assertEqual(bill.l10n_ae_received_delivery_summary, inbox.delivery_summary)
        for party in (bill.partner_id, self.env.company.partner_id):
            self.assertNotEqual(
                inbox.delivery_snapshot["address"]["street_name"], party.street
            )
        snapshot = inbox.delivery_snapshot
        self.partner_a.street = "Later edited supplier billing street"
        self.env.company.partner_id.street = "Later edited buyer billing street"
        self.assertEqual(inbox.delivery_snapshot, snapshot)
        self.assertIn(
            "Synthetic warehouse seven", bill.l10n_ae_received_delivery_summary
        )
        with self.assertRaises(UserError):
            inbox.write({"delivery_snapshot": {"address": {"street_name": "Forged"}}})
        with self.assertRaisesRegex(UserError, "received"):
            bill.write({"l10n_ae_ecommerce": False})
        replay = receiver.receive_controlled_document(
            binding.id, "ecommerce-delivery-one", content
        )
        self.assertEqual(replay, inbox)
        self.assertTrue(replay.action_process())
        self.assertEqual(len(inbox.processing_history_ids), 1)
        other_reference = receiver.receive_controlled_document(
            binding.id, "ecommerce-delivery-two", content
        )
        self.assertTrue(other_reference.action_process(), other_reference.diagnostic)
        self.assertEqual(other_reference.move_id, bill)
        self.assertEqual(other_reference.delivery_snapshot, snapshot)
        self.assertFalse(bill.l10n_ae_transmission_id)
        self.assertEqual(
            self.env["account.move"].search_count(
                [
                    ("company_id", "=", self.env.company.id),
                    ("l10n_ae_received_document", "=", True),
                    (
                        "l10n_ae_fiscal_uuid",
                        "=",
                        "eeeeeeec-1111-4111-8111-111111111111",
                    ),
                ]
            ),
            1,
        )
        with self.assertRaises(AccessError):
            inbox.read(["evidence_archive"])
        auditor = new_test_user(
            self.env,
            login="synthetic-ecommerce-delivery-auditor",
            groups="l10n_ae_edi.group_fiscal_auditor,base.group_allow_export",
            company_id=self.env.company.id,
            company_ids=self.env.company.ids,
        )
        audit = inbox.with_user(auditor)
        with zipfile.ZipFile(io.BytesIO(audit.evidence_archive.content)) as archive:
            exported_delivery = archive.read("delivery.json")
            self.assertEqual(json.loads(exported_delivery), snapshot)
            self.assertEqual(archive.read("document.xml"), content)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(
                manifest["artifacts"]["delivery.json"]["sha256"],
                hashlib.sha256(exported_delivery).hexdigest(),
            )

    @requires_normative_resources("1.0.4")
    def test_received_free_zone_ecommerce_combines_beneficiary_delivery_and_replay(
        self,
    ):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        fiscal_uuid = "eeeeeeea-1111-4111-8111-111111111111"
        source.write(
            {
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000004",
                "l10n_ae_fiscal_uuid": fiscal_uuid,
            }
        )
        tree = self._received_test_xml(source)
        tree.find("cbc:ProfileExecutionID", NS).text = "10000010"
        address = tree.find("cac:Delivery/cac:DeliveryLocation/cac:Address", NS)
        self.assertIsNotNone(address)
        for name, value in (
            ("StreetName", "Synthetic combined delivery street"),
            ("CityName", "Dubai"),
            ("CountrySubentity", "DXB"),
        ):
            node = address.find(f"cbc:{name}", NS)
            self.assertIsNotNone(node)
            node.text = value
        content = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
        self.assertEqual(tree.findtext("cbc:InvoiceTypeCode", namespaces=NS), "380")
        self.assertEqual(
            tree.findtext("cbc:CustomizationID", namespaces=NS),
            "urn:peppol:pint:billing-1@ae-1",
        )
        self.assertEqual(
            tree.findtext(
                "cac:BuyerCustomerParty/cac:Party/cac:PartyIdentification/cbc:ID",
                namespaces=NS,
            ),
            "1000000004",
        )
        for party in ("AccountingSupplierParty", "AccountingCustomerParty"):
            for path in (
                "cac:Party/cbc:EndpointID",
                "cac:Party/cac:PartyTaxScheme/cbc:CompanyID",
            ):
                identity = tree.findtext(f"cac:{party}/{path}", namespaces=NS)
                self.assertTrue(identity)
                self.assertNotEqual(identity, "1000000004")
        self.assertEqual(
            tree.findtext(
                "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:ID",
                namespaces=NS,
            ),
            "S",
        )
        self.assertEqual(
            Decimal(
                tree.findtext(
                    "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:Percent",
                    namespaces=NS,
                )
            ),
            5,
        )
        binding = self._reviewed_binding(
            {"name": "Received combined Free Zone e-commerce"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        inbox = receiver.receive_controlled_document(
            binding.id, "combined-freezone-ecommerce", content
        )
        processed = inbox.action_process()
        report = json.loads(inbox.processing_history_ids.validation_report)
        self.assertTrue(report["xsd_valid"], report)
        self.assertTrue(report["valid"], report)
        self.assertTrue(processed, inbox.diagnostic)
        bill = inbox.move_id
        self.assertEqual((bill.move_type, bill.state), ("in_invoice", "draft"))
        self.assertEqual(bill.company_id, self.env.company)
        self.assertEqual(bill.partner_id, self.partner_a)
        self.assertTrue(bill.l10n_ae_free_zone)
        self.assertTrue(bill.l10n_ae_ecommerce)
        self.assertFalse(bill.l10n_ae_disclosed_agent)
        self.assertEqual(bill.l10n_ae_beneficiary_id, "1000000004")
        self.assertEqual(bill.l10n_ae_fiscal_uuid, fiscal_uuid)
        self.assertEqual(
            (bill.amount_untaxed, bill.amount_tax, bill.amount_total), (100, 5, 105)
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, hashlib.sha256(content).hexdigest())
        snapshot = inbox.delivery_snapshot
        self.assertEqual(
            snapshot["address"]["street_name"], "Synthetic combined delivery street"
        )
        self.assertEqual(snapshot["address"]["city_name"], "Dubai")
        self.assertEqual(snapshot["address"]["country_subentity"], "DXB")
        self.assertEqual(
            snapshot["original_xml_sha256"], hashlib.sha256(content).hexdigest()
        )
        for partner in (bill.partner_id, self.env.company.partner_id):
            self.assertNotEqual(partner.street, "Synthetic combined delivery street")
        self.assertIn(
            "Synthetic combined delivery street", bill.l10n_ae_received_delivery_summary
        )
        for values in (
            {"l10n_ae_ecommerce": False},
            {"l10n_ae_free_zone": False},
            {"l10n_ae_beneficiary_id": "1000000005"},
        ):
            with self.assertRaisesRegex(UserError, "received"):
                bill.write(values)
        replay = receiver.receive_controlled_document(
            binding.id, "combined-freezone-ecommerce", content
        )
        self.assertEqual(replay, inbox)
        self.assertTrue(replay.action_process(), replay.diagnostic)
        other = receiver.receive_controlled_document(
            binding.id, "combined-freezone-ecommerce-second-reference", content
        )
        self.assertTrue(other.action_process(), other.diagnostic)
        self.assertEqual(other.move_id, bill)
        self.assertEqual(other.delivery_snapshot, snapshot)
        self.assertEqual(len(inbox.processing_history_ids), 1)
        self.assertEqual(
            self.env["account.move"].search_count(
                [
                    ("company_id", "=", self.env.company.id),
                    ("l10n_ae_received_document", "=", True),
                    ("l10n_ae_fiscal_uuid", "=", fiscal_uuid),
                ]
            ),
            1,
        )
        self.assertFalse(bill.l10n_ae_transmission_id)

    def test_received_forms_open_when_evidence_export_refuses_integrity(self):
        binding = self._reviewed_binding(
            {"name": "Received form integrity"},
            capabilities=("receive",),
            note="Synthetic reviewed receiving contract for form/export isolation.",
        )
        content = etree.tostring(self._received_test_xml())
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "received-form-integrity", content)
        )
        actors = [
            (
                new_test_user(
                    self.env,
                    login="synthetic-export-form-manager",
                    groups="account.group_account_manager,base.group_allow_export",
                    company_id=self.env.company.id,
                    company_ids=self.env.company.ids,
                ),
                "l10n_ae_edi_pint.edi_inbox_form",
            ),
            (
                new_test_user(
                    self.env,
                    login="synthetic-export-form-auditor",
                    groups="l10n_ae_edi.group_fiscal_auditor,base.group_allow_export",
                    company_id=self.env.company.id,
                    company_ids=self.env.company.ids,
                ),
                "l10n_ae_edi_pint.edi_audit_inbox_form",
            ),
        ]
        # PostgreSQL system edge simulates damaged historical evidence. No ORM,
        # archive builder or form collaborator is replaced.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE l10n_ae_edi_inbox SET xml_hash=%s WHERE id=%s",
            ["0" * 64, inbox.id],
        )
        self.env.invalidate_all()
        for actor, view_xmlid in actors:
            with self.subTest(view=view_xmlid):
                original = inbox.with_user(actor).with_context(bin_size=True)
                self.assertFalse(original.env.su)
                view = original.get_view(
                    view_id=self.env.ref(view_xmlid).id, view_type="form"
                )
                tree = etree.fromstring(view["arch"].encode())
                # Read the root fields requested by this effective native form.
                # Nested one2many subforms have their own read specifications.
                names = tree.xpath("//field[not(ancestor::field)]/@name")
                values = original.web_read({name: {} for name in names})
                self.assertEqual(len(values), 1)
                self.assertEqual(values[0]["id"], inbox.id)
                self.assertEqual(values[0]["remote_id"], "received-form-integrity")
                self.assertEqual(values[0]["xml_hash"], "0" * 64)
                self.assertEqual(values[0]["state"], "received")
                self.assertFalse(inbox.move_id)
                self.assertEqual(inbox.original_xml.content, content)
                # URL generation is safe even when the subsequent download must
                # refuse damaged evidence; it does not compute the Binary.
                self.assertEqual(
                    original.action_download_evidence(),
                    {
                        "type": "ir.actions.act_url",
                        "url": (
                            f"/web/content/l10n_ae.edi.inbox/{inbox.id}/evidence_archive"
                            "?download=true&filename_field=evidence_archive_filename"
                        ),
                        "target": "download",
                    },
                )
                self.assertEqual(
                    len(tree.xpath("//button[@name='action_download_evidence']")), 1
                )
                self.assertNotIn("evidence_archive", names)
                with self.assertRaisesRegex(UserError, "integrity check failed"):
                    original.with_context(bin_size=False).read(["evidence_archive"])
                if view_xmlid.endswith(".edi_inbox_form"):
                    for action in ("action_process", "action_request_review"):
                        self.assertEqual(
                            len(tree.xpath(f"//button[@name='{action}']")), 1
                        )

        no_export = new_test_user(
            self.env,
            login="synthetic-no-export-form-manager",
            groups="account.group_account_manager",
            company_id=self.env.company.id,
            company_ids=self.env.company.ids,
        )
        self.assertFalse(no_export.has_group("base.group_allow_export"))
        for elevated in (False, True):
            with self.subTest(elevated=elevated), self.assertRaises(AccessError):
                inbox.with_user(no_export).sudo(elevated).action_download_evidence()
        for actor, _view_xmlid in actors:
            actor.active = False
            with self.assertRaises(AccessError):
                inbox.with_user(actor).sudo().action_download_evidence()
            actor.active = True

    @requires_normative_resources("1.0.4")
    def test_received_ecommerce_archive_refuses_a_tampered_delivery_snapshot(self):
        tree = self._received_test_xml()
        tree.find("cbc:ProfileExecutionID", NS).text = "00000010"
        tree.find("cbc:UUID", NS).text = "eeeeeeec-1111-4111-8111-111111111111"
        address = tree.find("cac:Delivery/cac:DeliveryLocation/cac:Address", NS)
        self.assertIsNotNone(address)
        for name, value in (
            ("StreetName", "Synthetic preserved integrity street"),
            ("CityName", "Dubai"),
            ("CountrySubentity", "DXB"),
        ):
            node = address.find(f"cbc:{name}", NS)
            self.assertIsNotNone(node)
            node.text = value
        content = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
        binding = self._reviewed_binding(
            {"name": "Received e-commerce delivery integrity"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        inbox = receiver.receive_controlled_document(
            binding.id, "ecommerce-delivery-integrity", content
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        report = json.loads(inbox.processing_history_ids.validation_report)
        self.assertTrue(report["xsd_valid"], report)
        self.assertTrue(report["valid"], report)
        bill = inbox.move_id
        self.assertTrue(bill)
        self.assertEqual((bill.move_type, bill.state), ("in_invoice", "draft"))
        self.assertEqual(
            (bill.amount_untaxed, bill.amount_tax, bill.amount_total), (100, 5, 105)
        )

        auditor = new_test_user(
            self.env,
            login="synthetic-ecommerce-integrity-auditor",
            groups="l10n_ae_edi.group_fiscal_auditor,base.group_allow_export",
            company_id=self.env.company.id,
            company_ids=self.env.company.ids,
        )
        audit = inbox.with_user(auditor)
        snapshot = json.loads(json.dumps(inbox.delivery_snapshot))
        original_xml = inbox.original_xml.content
        original_hash = inbox.xml_hash
        state = inbox.state
        bill_id = bill.id
        bill_facts = (
            bill.move_type,
            bill.state,
            bill.amount_untaxed,
            bill.amount_tax,
            bill.amount_total,
        )
        history_facts = [
            (row.id, row.validation_report_hash)
            for row in inbox.processing_history_ids.sorted("id")
        ]

        before = audit.evidence_archive.content
        with zipfile.ZipFile(io.BytesIO(before)) as archive:
            self.assertEqual(json.loads(archive.read("delivery.json")), snapshot)
            self.assertEqual(archive.read("document.xml"), original_xml)

        variants = []
        for key, value in (
            ("original_xml_sha256", "0" * 64),
            ("actual_delivery_date", "2031-01-01"),
            ("delivery_xml", "<forged-delivery/>"),
        ):
            altered = json.loads(json.dumps(snapshot))
            altered[key] = value
            variants.append((key, altered))
        altered = json.loads(json.dumps(snapshot))
        altered["address"]["street_name"] = "Synthetic tampered persisted delivery"
        variants.extend((("street_name", altered), ("missing_snapshot", False)))
        for name, altered in variants:
            with self.subTest(tampered=name):
                self.env.cr.execute(
                    "UPDATE l10n_ae_edi_inbox SET "
                    "delivery_snapshot=%s::jsonb WHERE id=%s",
                    [json.dumps(altered), inbox.id],
                )
                self.env.invalidate_all()
                try:
                    with self.assertRaisesRegex(
                        UserError, "Received delivery evidence does not match"
                    ):
                        audit.read(["evidence_archive"])
                    self.assertEqual(inbox.original_xml.content, original_xml)
                    self.assertEqual(inbox.xml_hash, original_hash)
                    self.assertEqual(inbox.state, state)
                    self.assertEqual(inbox.move_id.id, bill_id)
                    self.assertEqual(
                        (
                            bill.move_type,
                            bill.state,
                            bill.amount_untaxed,
                            bill.amount_tax,
                            bill.amount_total,
                        ),
                        bill_facts,
                    )
                    self.assertEqual(
                        [
                            (row.id, row.validation_report_hash)
                            for row in inbox.processing_history_ids.sorted("id")
                        ],
                        history_facts,
                    )
                finally:
                    self.env.cr.execute(
                        "UPDATE l10n_ae_edi_inbox SET "
                        "delivery_snapshot=%s::jsonb WHERE id=%s",
                        [json.dumps(snapshot), inbox.id],
                    )
                    self.env.invalidate_all()
                self.assertEqual(audit.evidence_archive.content, before)

        with zipfile.ZipFile(io.BytesIO(audit.evidence_archive.content)) as archive:
            restored_delivery = archive.read("delivery.json")
            self.assertEqual(json.loads(restored_delivery), snapshot)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(
                manifest["artifacts"]["delivery.json"]["sha256"],
                hashlib.sha256(restored_delivery).hexdigest(),
            )
        self.assertEqual(inbox.original_xml.content, original_xml)
        self.assertEqual(inbox.xml_hash, original_hash)
        self.assertEqual(inbox.state, state)
        self.assertEqual(inbox.move_id.id, bill_id)
        self.assertEqual(
            [
                (row.id, row.validation_report_hash)
                for row in inbox.processing_history_ids.sorted("id")
            ],
            history_facts,
        )

    @requires_normative_resources("1.0.4")
    def test_received_ecommerce_missing_required_delivery_stays_in_review(self):
        binding = self._reviewed_binding(
            {"name": "Incomplete received e-commerce delivery"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        for index, missing in enumerate(("StreetName", "CityName", "CountrySubentity")):
            with self.subTest(missing=missing):
                tree = self._received_test_xml()
                tree.find("cbc:ProfileExecutionID", NS).text = "00000010"
                fiscal_uuid = f"eeeeeeee-000{index}-4000-8000-111111111111"
                tree.find("cbc:UUID", NS).text = fiscal_uuid
                address = tree.find("cac:Delivery/cac:DeliveryLocation/cac:Address", NS)
                self.assertIsNotNone(address)
                for name, value in (
                    ("StreetName", "Synthetic incomplete delivery street"),
                    ("CityName", "Dubai"),
                    ("CountrySubentity", "DXB"),
                ):
                    node = address.find(f"cbc:{name}", NS)
                    self.assertIsNotNone(node)
                    node.text = value
                address.remove(address.find(f"cbc:{missing}", NS))
                content = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
                inbox = receiver.receive_controlled_document(
                    binding.id, f"ecommerce-missing-{index}", content
                )
                self.assertFalse(inbox.action_process())
                self.assertEqual(inbox.state, "review")
                self.assertFalse(inbox.move_id)
                self.assertFalse(inbox.delivery_snapshot)
                self.assertEqual(inbox.original_xml.content, content)
                self.assertEqual(inbox.xml_hash, hashlib.sha256(content).hexdigest())
                history = inbox.processing_history_ids
                self.assertEqual(len(history), 1)
                report = json.loads(history.validation_report)
                self.assertTrue(report["xsd_valid"], report)
                self.assertFalse(report["valid"], report)
                self.assertIn("ibr-142-ae", inbox.diagnostic.lower())
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
                self.assertEqual(
                    receiver.receive_controlled_document(
                        binding.id, f"ecommerce-missing-{index}", content
                    ),
                    inbox,
                )
                self.assertEqual(len(inbox.processing_history_ids), 1)

    @requires_normative_resources("1.0.4")
    def test_received_ecommerce_multiple_deliveries_require_review_without_a_draft(
        self,
    ):
        tree = self._received_test_xml()
        tree.find("cbc:ProfileExecutionID", NS).text = "00000010"
        fiscal_uuid = "eeeeeeef-1111-4111-8111-111111111111"
        tree.find("cbc:UUID", NS).text = fiscal_uuid
        delivery = tree.find("cac:Delivery", NS)
        self.assertIsNotNone(delivery)
        delivery.addnext(etree.fromstring(etree.tostring(delivery)))
        content = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
        binding = self._reviewed_binding(
            {"name": "Ambiguous received e-commerce delivery"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        inbox = receiver.receive_controlled_document(
            binding.id, "ecommerce-ambiguous-delivery", content
        )
        self.assertFalse(inbox.action_process())
        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)
        self.assertFalse(inbox.delivery_snapshot)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, hashlib.sha256(content).hexdigest())
        self.assertEqual(len(inbox.processing_history_ids), 1)
        report = json.loads(inbox.processing_history_ids.validation_report)
        self.assertTrue(report["xsd_valid"], report)
        self.assertFalse(report["valid"], report)
        self.assertIn("ibr-107", inbox.diagnostic.lower())
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
    def test_received_ecommerce_delivery_is_visible_in_native_review_forms(self):
        tree = self._received_test_xml()
        tree.find("cbc:ProfileExecutionID", NS).text = "00000010"
        tree.find("cbc:UUID", NS).text = "eeeeeeed-1111-4111-8111-111111111111"
        location = tree.find("cac:Delivery/cac:DeliveryLocation", NS)
        self.assertIsNotNone(location)
        location.remove(location.find("cac:Address", NS))
        address = etree.SubElement(location, etree.QName(NS["cac"], "Address"))
        for name, value in (
            ("StreetName", "Synthetic native delivery street"),
            ("CityName", "Dubai"),
            ("CountrySubentity", "DXB"),
        ):
            etree.SubElement(address, etree.QName(NS["cbc"], name)).text = value
        country = etree.SubElement(address, etree.QName(NS["cac"], "Country"))
        etree.SubElement(
            country, etree.QName(NS["cbc"], "IdentificationCode")
        ).text = "AE"
        content = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
        binding = self._reviewed_binding(
            {"name": "Received native e-commerce review"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "ecommerce-native-review", content)
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        report = json.loads(inbox.processing_history_ids.validation_report)
        self.assertTrue(report["valid"], report)
        review = Form(inbox)
        self.assertIn("Synthetic native delivery street", review.delivery_summary)
        bill = Form(inbox.move_id)
        self.assertEqual(
            bill.l10n_ae_received_delivery_summary, review.delivery_summary
        )
        self.assertEqual(inbox.move_id.state, "draft")
        self.assertEqual(inbox.original_xml.content, content)

    @requires_normative_resources("1.0.4")
    def test_received_delivery_emirate_requires_review_and_preserves_original(self):
        original = self._received_test_xml()
        binding = self._reviewed_binding(
            {"name": "Synthetic received delivery emirate"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        cases = (
            ("AE", "Dubai", False),
            ("AE", None, False),
            ("AE", "RAK", True),
            ("GB", "London", True),
        )
        for index, (country, subdivision, accepted) in enumerate(cases):
            with self.subTest(country=country, subdivision=subdivision):
                tree = etree.fromstring(etree.tostring(original))
                tree.find("cbc:UUID", NS).text = f"ddddddde-8888-4888-8888-{index:012d}"
                tree.find("cbc:ID", NS).text = f"SYNTH-DELIVERY-EMIRATE-{index}"
                address = tree.find("cac:Delivery/cac:DeliveryLocation/cac:Address", NS)
                self.assertIsNotNone(address)
                address.find("cac:Country/cbc:IdentificationCode", NS).text = country
                node = address.find("cbc:CountrySubentity", NS)
                if subdivision is None:
                    address.remove(node)
                else:
                    node.text = subdivision
                content = etree.tostring(tree)
                count = self.env["account.move"].search_count([])
                inbox = receiver.receive_controlled_document(
                    binding.id, f"delivery-emirate-{index}", content
                )
                self.assertEqual(inbox.action_process(), accepted, inbox.diagnostic)
                self.assertEqual(inbox.original_xml.content, content)
                self.assertEqual(inbox.xml_hash, hashlib.sha256(content).hexdigest())
                history = inbox.processing_history_ids
                self.assertEqual(len(history), 1)
                self.assertEqual(history.operator_id, self.simple_accountman)
                report = json.loads(history.validation_report)
                self.assertTrue(report["xsd_valid"])
                self.assertEqual(report["valid"], accepted)
                self.assertTrue(all(not c["failures"] for c in report["layers"]))
                self.assertEqual(
                    history.validation_report_hash,
                    hashlib.sha256(history.validation_report.encode()).hexdigest(),
                )
                check = next(
                    c
                    for c in report["supplemental_checks"]
                    if c["name"] == "kmee-ae-delivery-address-v1"
                )
                if accepted:
                    self.assertEqual(inbox.state, "processed")
                    self.assertEqual(inbox.move_id.state, "draft")
                    self.assertEqual(inbox.move_id.amount_total, 105)
                    self.assertFalse(inbox.move_id.l10n_ae_transmission_id)
                    self.assertEqual(check["failures"], [])
                else:
                    self.assertEqual(inbox.state, "review")
                    self.assertFalse(inbox.move_id)
                    self.assertEqual(self.env["account.move"].search_count([]), count)
                    self.assertIn("KMEE supplemental ibr-128-ae", inbox.diagnostic)
                    self.assertIn("delivery address", inbox.diagnostic)
                    self.assertEqual(
                        [f["code"] for f in check["failures"]], ["ibr-128-ae"]
                    )
                    self.assertIn("DeliveryLocation", check["failures"][0]["location"])
                    with self.assertRaises(UserError):
                        history.write({"validation_report": "{}"})

    def test_received_event_package_retains_every_conflicting_association(self):
        binding = self._reviewed_binding(
            {"name": "Portable events"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        content = b"<synthetic-original/>"
        original = receiver.receive_controlled_document(
            binding.id, "first-document", content, event_id="portable-event"
        )
        auditor = new_test_user(
            self.env,
            login="synthetic-event-export-auditor",
            groups="l10n_ae_edi.group_fiscal_auditor,base.group_allow_export",
            company_id=self.env.company.id,
            company_ids=self.env.company.ids,
        )
        audit = original.with_user(auditor)
        before = audit.evidence_archive.content
        second = receiver.receive_controlled_document(
            binding.id,
            "second-document",
            b"<other-original/>",
            event_id="portable-event",
        )
        after = audit.evidence_archive.content
        self.assertNotEqual(before, after)
        with zipfile.ZipFile(io.BytesIO(after)) as archive:
            events = json.loads(archive.read("ingestion-events.json"))
            self.assertEqual(
                [row["event_id"] for row in events],
                ["portable-event", "portable-event"],
            )
            self.assertEqual(
                [row["inbox_id"] for row in events], [original.id, second.id]
            )
            self.assertEqual(
                [row["disposition"] for row in events], ["retained", "collision"]
            )
            self.assertEqual(
                [row["operator_id"] for row in events],
                [self.simple_accountman.id, self.simple_accountman.id],
            )
            self.assertEqual(archive.read("document.xml"), content)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(
                manifest["relationships"]["ingestion_events"],
                [row["id"] for row in events],
            )
            nested_content = archive.read(f"ingestion-originals/{second.id}.zip")
            with zipfile.ZipFile(io.BytesIO(nested_content)) as nested:
                self.assertEqual(nested.read("document.xml"), b"<other-original/>")
                self.assertEqual(
                    json.loads(nested.read("receipt.json"))["ingestion_conflict"], True
                )
        for hidden in (original.ingestion_event_ids, second):
            rule = self.env["ir.access"].create(
                {
                    "name": "Synthetic complete event archive access",
                    "model_id": self.env["ir.model"]._get(hidden._name).id,
                    "operation": "r",
                    "domain": f"[('id', '!=', {hidden.id})]",
                }
            )
            try:
                self.env.invalidate_all()
                for source in (audit, audit.sudo()):
                    with self.assertRaises(AccessError):
                        source.read(["evidence_archive"])
            finally:
                rule.unlink()
                self.env.invalidate_all()
            self.assertEqual(audit.evidence_archive.content, after)

    @requires_normative_resources("1.0.4")
    def test_received_events_replay_without_duplicate_original_or_bill(self):
        binding = self._reviewed_binding(
            {"name": "Event identity"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        content = etree.tostring(self._received_test_xml())
        original = receiver.receive_controlled_document(
            binding.id, "invoice-event-source", content, event_id="delivery-first"
        )
        self.assertTrue(original.action_process(), original.diagnostic)
        bill = original.move_id
        self.assertEqual(
            (bill.move_type, bill.state, bill.amount_total),
            ("in_invoice", "draft", 105),
        )
        first = original.ingestion_event_ids
        self.assertEqual(len(first), 1)
        retained = first.read(["event_id", "inbox_id", "operator_id", "occurred_at"])
        for event_id in ("delivery-first", "delivery-second", "delivery-second"):
            replay = receiver.receive_controlled_document(
                binding.id, "invoice-event-source", content, event_id=event_id
            )
            self.assertEqual(replay, original)
            self.assertTrue(replay.action_process(), replay.diagnostic)
            self.assertEqual(replay.move_id, bill)
        self.assertEqual(len(original.ingestion_event_ids), 2)
        self.assertEqual(
            original.ingestion_event_ids.mapped("event_id"),
            ["delivery-first", "delivery-second"],
        )
        self.assertEqual(
            first.read(["event_id", "inbox_id", "operator_id", "occurred_at"]), retained
        )
        self.assertEqual(first.operator_id, self.simple_accountman)
        self.assertEqual(original.original_xml.content, content)
        self.assertEqual(len(original.processing_history_ids), 1)
        self.assertFalse(original.ingestion_conflict)

    @requires_normative_resources("1.0.4")
    def test_received_summary_preserves_period_original_and_one_supplier_draft(self):
        source = self.init_invoice(
            "out_invoice", amounts=[40, 60], taxes=self.tax_sale_a, post=True
        )
        source.write(
            {
                "l10n_ae_summary": True,
                "l10n_ae_invoicing_period_start": "2019-01-01",
                "l10n_ae_invoicing_period_end": "2019-01-31",
                "l10n_ae_fiscal_uuid": "cccccccf-1111-4111-8111-111111111111",
            }
        )
        content = etree.tostring(self._received_test_xml(source))
        original_hash = hashlib.sha256(content).hexdigest()
        binding = self._reviewed_binding(
            {"name": "Synthetic received Summary period"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
        )
        self.assertFalse(receiver.env.su)
        inbox = receiver.receive_controlled_document(
            binding.id, "received-summary-month", content
        )
        processed = inbox.action_process()
        report = json.loads(inbox.processing_history_ids.validation_report)
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        self.assertTrue(processed, inbox.diagnostic)
        bill = inbox.move_id
        self.assertEqual((bill.move_type, bill.state), ("in_invoice", "draft"))
        self.assertTrue(bill.l10n_ae_summary)
        self.assertEqual(
            (
                bill.l10n_ae_invoicing_period_start.isoformat(),
                bill.l10n_ae_invoicing_period_end.isoformat(),
            ),
            ("2019-01-01", "2019-01-31"),
        )
        self.assertEqual(
            (bill.amount_untaxed, bill.amount_tax, bill.amount_total), (100, 5, 105)
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, original_hash)
        self.assertFalse(bill.l10n_ae_transmission_ids)
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
        ledger = bill.line_ids.sorted("id").read(ledger_fields)
        timestamps = bill.read(["create_date", "write_date"])
        replay = receiver.receive_controlled_document(
            binding.id, "received-summary-month", content
        )
        self.assertEqual(replay, inbox)
        self.assertTrue(replay.action_process(), replay.diagnostic)
        self.assertEqual(replay.move_id, bill)
        self.assertEqual(bill.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertEqual(bill.read(["create_date", "write_date"]), timestamps)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, original_hash)
        for values in (
            {"l10n_ae_summary": False},
            {"l10n_ae_invoicing_period_start": "2019-02-01"},
            {"l10n_ae_invoicing_period_end": "2019-02-28"},
        ):
            with self.assertRaisesRegex(UserError, "received"):
                bill.write(values)

    @requires_normative_resources("1.0.4")
    def test_received_free_zone_summary_invoice_preserves_beneficiary_and_period(self):
        source = self.init_invoice(
            "out_invoice", amounts=[40, 60], taxes=self.tax_sale_a, post=True
        )
        source.write(
            {
                "l10n_ae_summary": True,
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000003",
                "l10n_ae_invoicing_period_start": "2019-01-01",
                "l10n_ae_invoicing_period_end": "2019-01-31",
                "l10n_ae_fiscal_uuid": "cccccccf-2222-4222-8222-222222222222",
            }
        )
        content = etree.tostring(self._received_test_xml(source))
        tree = etree.fromstring(content)
        self.assertEqual(tree.findtext("cbc:InvoiceTypeCode", namespaces=NS), "380")
        self.assertEqual(
            tree.findtext("cbc:ProfileExecutionID", namespaces=NS), "10010000"
        )
        beneficiary_xpath = (
            "cac:BuyerCustomerParty/cac:Party/cac:PartyIdentification/cbc:ID"
        )
        self.assertEqual(len(tree.findall(beneficiary_xpath, namespaces=NS)), 1)
        self.assertEqual(tree.findtext(beneficiary_xpath, namespaces=NS), "1000000003")
        self.assertEqual(
            tree.findtext("cbc:CustomizationID", namespaces=NS),
            "urn:peppol:pint:billing-1@ae-1",
        )
        self.assertEqual(
            tree.findtext("cbc:DocumentCurrencyCode", namespaces=NS), "AED"
        )
        self.assertEqual(
            (
                tree.findtext("cac:InvoicePeriod/cbc:StartDate", namespaces=NS),
                tree.findtext("cac:InvoicePeriod/cbc:EndDate", namespaces=NS),
            ),
            ("2019-01-01", "2019-01-31"),
        )
        self.assertEqual(
            tree.findtext("cbc:ProfileID", namespaces=NS), "urn:peppol:bis:billing"
        )
        self.assertEqual(
            (
                tree.findtext(
                    "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", namespaces=NS
                ),
                tree.findtext("cac:TaxTotal/cbc:TaxAmount", namespaces=NS),
                tree.findtext(
                    "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", namespaces=NS
                ),
            ),
            ("100.00", "5.00", "105.00"),
        )
        source_ledger = source.line_ids.sorted("id").read(
            ["account_id", "debit", "credit", "balance", "write_date"]
        )
        source_timestamps = source.read(["create_date", "write_date"])
        formal = OfflinePintValidator(
            self.normative_directories["1.0.4"],
            ruleset="kmee-pint-ae-1.0.4-ubl-2.1",
        ).validate(content)
        self.assertTrue(formal["valid"], formal)
        original_hash = hashlib.sha256(content).hexdigest()
        binding = self._reviewed_binding(
            {"name": "Synthetic received Free Zone Summary period"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[self.env.company.id])
        )
        self.assertFalse(receiver.env.su)
        account_moves = self.env["account.move"].search([]).ids
        inbox = receiver.receive_controlled_document(
            binding.id, "received-free-zone-summary-month", content
        )
        processed = inbox.action_process()
        report = json.loads(inbox.processing_history_ids.validation_report)
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
        self.assertTrue(processed, inbox.diagnostic)
        bill = inbox.move_id
        self.assertEqual((bill.move_type, bill.state), ("in_invoice", "draft"))
        self.assertTrue(bill.l10n_ae_summary)
        self.assertTrue(bill.l10n_ae_free_zone)
        self.assertEqual(bill.l10n_ae_beneficiary_id, "1000000003")
        self.assertEqual(
            (
                bill.l10n_ae_invoicing_period_start.isoformat(),
                bill.l10n_ae_invoicing_period_end.isoformat(),
            ),
            ("2019-01-01", "2019-01-31"),
        )
        self.assertEqual(
            (bill.amount_untaxed, bill.amount_tax, bill.amount_total), (100, 5, 105)
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, original_hash)
        self.assertFalse(bill.l10n_ae_transmission_ids)
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
        self.assertEqual(len(inbox.processing_history_ids), 1)
        history = inbox.processing_history_ids.read(
            ["validation_report", "validation_report_hash"]
        )
        self.assertEqual(
            sorted(self.env["account.move"].search([]).ids),
            sorted(account_moves + [bill.id]),
        )
        ledger = bill.line_ids.sorted("id").read(ledger_fields)
        timestamps = bill.read(["create_date", "write_date"])
        replay = receiver.receive_controlled_document(
            binding.id, "received-free-zone-summary-month", content
        )
        self.assertEqual(replay, inbox)
        self.assertTrue(replay.action_process(), replay.diagnostic)
        self.assertEqual(replay.move_id, bill)
        self.assertEqual(bill.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertEqual(bill.read(["create_date", "write_date"]), timestamps)
        self.assertEqual(
            inbox.processing_history_ids.read(
                ["validation_report", "validation_report_hash"]
            ),
            history,
        )
        self.assertEqual(
            sorted(self.env["account.move"].search([]).ids),
            sorted(account_moves + [bill.id]),
        )
        self.assertEqual(
            source.line_ids.sorted("id").read(
                ["account_id", "debit", "credit", "balance", "write_date"]
            ),
            source_ledger,
        )
        self.assertEqual(source.read(["create_date", "write_date"]), source_timestamps)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, original_hash)
        for values in (
            {"l10n_ae_summary": False},
            {"l10n_ae_free_zone": False},
            {"l10n_ae_beneficiary_id": "1000000004"},
            {"l10n_ae_invoicing_period_start": "2019-02-01"},
            {"l10n_ae_invoicing_period_end": "2019-02-28"},
        ):
            with self.assertRaisesRegex(UserError, "received"):
                bill.write(values)

    @requires_normative_resources("1.0.4")
    def test_received_summary_invalid_periods_are_retained_without_accounting(self):
        company = self.env.company
        source = self.init_invoice(
            "out_invoice", amounts=[40, 60], taxes=self.tax_sale_a, post=True
        )
        source.write(
            {
                "l10n_ae_summary": True,
                "l10n_ae_invoicing_period_start": "2019-01-01",
                "l10n_ae_invoicing_period_end": "2019-01-31",
                "l10n_ae_fiscal_uuid": "cccccccf-2222-4222-8222-222222222222",
            }
        )
        valid_report = source.validate_l10n_ae_pint()
        self.assertTrue(valid_report["valid"], valid_report)
        control = self._received_test_xml(source)
        control_report = OfflinePintValidator(
            self.normative_directories["1.0.4"],
            ruleset="kmee-pint-ae-1.0.4-ubl-2.1",
        ).validate(etree.tostring(control))
        self.assertTrue(control_report["valid"], control_report)
        self.assertEqual(control.findtext("cbc:InvoiceTypeCode", namespaces=NS), "380")
        self.assertEqual(
            control.findtext("cbc:ProfileExecutionID", namespaces=NS), "00010000"
        )
        for path, expected in (
            ("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", "100.00"),
            ("cac:TaxTotal/cbc:TaxAmount", "5.00"),
            ("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "105.00"),
            ("cac:LegalMonetaryTotal/cbc:PayableAmount", "105.00"),
        ):
            self.assertEqual(control.findtext(path, namespaces=NS), expected)
        self.assertEqual(
            (source.amount_untaxed, source.amount_tax, source.amount_total),
            (100, 5, 105),
        )

        receiver = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .with_context(allowed_company_ids=[company.id])
        )
        self.assertFalse(receiver.env.su)
        binding = self._reviewed_binding(
            {"name": "Synthetic invalid received Summary periods"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        move_model_names = (
            "account.move",
            "account.move.line",
            "l10n_ae.edi.transmission",
            "l10n_ae.edi.attempt",
            "l10n_ae.edi.event",
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
        source_ledger = source.line_ids.sorted("id").read(ledger_fields)
        source_timestamps = source.read(["create_date", "write_date"])
        for index, (vector, expected_rule) in enumerate(
            (("reversed", "ibr-029"), ("missing", "ibr-138-ae")), start=1
        ):
            with self.subTest(vector=vector):
                tree = etree.fromstring(etree.tostring(control))
                tree.find(
                    "cbc:UUID", NS
                ).text = f"eeeeee{index:02d}-2222-4222-8222-222222222222"
                period = tree.find("cac:InvoicePeriod", NS)
                self.assertIsNotNone(period)
                if vector == "reversed":
                    period.find("cbc:StartDate", NS).text = "2019-01-31"
                    period.find("cbc:EndDate", NS).text = "2019-01-01"
                else:
                    tree.remove(period)
                content = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
                original_hash = hashlib.sha256(content).hexdigest()
                before_ids = {
                    model: self.env[model].search([("company_id", "=", company.id)]).ids
                    for model in move_model_names
                }
                inbox = receiver.receive_controlled_document(
                    binding.id, f"invalid-summary-period-{vector}", content
                )
                self.assertFalse(inbox.action_process())
                self.assertEqual(inbox.state, "review")
                self.assertFalse(inbox.move_id)
                self.assertEqual(inbox.original_xml.content, content)
                self.assertEqual(inbox.xml_hash, original_hash)
                history = inbox.processing_history_ids.sorted("id")
                self.assertEqual(len(history), 1)
                report = json.loads(history[0].validation_report)
                self.assertEqual(
                    history[0].validation_report_hash,
                    hashlib.sha256(history[0].validation_report.encode()).hexdigest(),
                )
                self.assertTrue(report["xsd_valid"], report)
                self.assertFalse(report["valid"], report)
                self.assertEqual(report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
                failures = {
                    failure["code"]
                    for layer in report["layers"]
                    for failure in layer["failures"]
                }
                self.assertEqual(failures, {expected_rule}, report)
                self.assertIn(expected_rule, inbox.diagnostic.lower())
                self.assertFalse(inbox.action_process())
                self.assertEqual(inbox.state, "review")
                self.assertFalse(inbox.move_id)
                self.assertEqual(inbox.original_xml.content, content)
                self.assertEqual(inbox.xml_hash, original_hash)
                self.assertEqual(
                    {
                        model: self.env[model]
                        .search([("company_id", "=", company.id)])
                        .ids
                        for model in move_model_names
                    },
                    before_ids,
                )
                self.assertEqual(
                    source.line_ids.sorted("id").read(ledger_fields),
                    source_ledger,
                )
                self.assertEqual(
                    source.read(["create_date", "write_date"]), source_timestamps
                )
                reports = [
                    json.loads(row.validation_report)
                    for row in inbox.processing_history_ids.sorted("id")
                ]
                self.assertEqual(len(reports), 2)
                self.assertTrue(all(not row["valid"] for row in reports))
                self.assertEqual(
                    [
                        {
                            failure["code"]
                            for layer in row["layers"]
                            for failure in layer["failures"]
                        }
                        for row in reports
                    ],
                    [{expected_rule}, {expected_rule}],
                )

    @requires_normative_resources("1.0.4")
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
        binding = self._reviewed_binding(
            {"name": "Received Free Zone"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox_model = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
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

    @requires_normative_resources("1.0.4")
    def test_received_native_multiline_aed_declarations_create_a_reviewable_draft(self):
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
        source = self.init_invoice(
            "out_invoice",
            amounts=[1.23, 1.23, 1.23],
            taxes=self.tax_sale_a,
            currency=usd,
        ).with_user(self.simple_accountman)
        source.action_post()
        source.write(
            {
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": source.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-INCOMING-HEADER-COMPARISON",
                "l10n_ae_fiscal_uuid": "aaaaaaad-1111-4111-8111-111111111111",
            }
        )
        # Independent native example: USD3.87 *3.6725 rounds to14.21,
        # while the sender's cent-allocated AED ledger declares14.22.
        self.assertEqual((source.amount_tax, source.amount_total), (0.18, 3.87))
        self.assertEqual(
            (source.amount_tax_signed, source.amount_total_signed), (0.66, 14.22)
        )
        ledger_fields = ["debit", "credit", "amount_currency", "write_date"]
        ledger = source.line_ids.read(ledger_fields)
        content = etree.tostring(self._received_test_xml(source))
        binding = self._reviewed_binding(
            {"name": "Incoming AED comparison"},
            capabilities=("receive",),
            note="Synthetic receive-only contract for native multiline declarations.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        inbox = receiver.receive_controlled_document(
            binding.id, "multiline-aed", content
        )
        processed = inbox.action_process()
        report = json.loads(inbox.processing_history_ids.validation_report)
        self.assertTrue(report["valid"], report)
        self.assertTrue(processed, inbox.diagnostic)
        bill = inbox.move_id
        self.assertFalse(bill.env.su)
        self.assertEqual(
            (inbox.state, bill.state, bill.move_type),
            ("processed", "draft", "in_invoice"),
        )
        self.assertEqual((bill.amount_tax, bill.amount_total), (0.18, 3.87))
        self.assertEqual(
            (
                bill.l10n_ae_received_tax_amount,
                bill.l10n_ae_received_tax_inclusive_amount,
            ),
            (0.66, 14.22),
        )
        comparison = bill.l10n_ae_received_aed_comparison
        self.assertEqual(comparison["gross_declared"], "14.22")
        self.assertEqual(comparison["gross_converted"], "14.21")
        self.assertIn("before posting", bill.l10n_ae_received_aed_review)
        self.assertIn("14.22", bill.l10n_ae_received_aed_review)
        self.assertIn("14.21", bill.l10n_ae_received_aed_review)
        with self.assertRaisesRegex(UserError, "comparison is protected"):
            bill.write({"l10n_ae_received_aed_comparison": False})
        replay = receiver.receive_controlled_document(
            binding.id, "multiline-aed", content
        )
        self.assertTrue(replay.action_process(), replay.diagnostic)
        self.assertEqual(replay.move_id, bill)
        self.assertEqual(bill.l10n_ae_received_aed_comparison, comparison)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(len(inbox.processing_history_ids), 1)
        self.assertFalse(
            bill.l10n_ae_transmission_ids or source.l10n_ae_transmission_ids
        )
        self.assertEqual(source.line_ids.read(ledger_fields), ledger)

    def test_received_aed_comparison_cannot_be_forged_through_create_defaults(self):
        moves = self.env["account.move"].with_user(self.simple_accountman)
        with self.assertRaisesRegex(UserError, "comparison is protected"):
            moves.create({"l10n_ae_received_aed_comparison": {"gross_declared": "1"}})
        with self.assertRaisesRegex(UserError, "comparison is protected"):
            moves.with_context(
                default_l10n_ae_received_aed_comparison={
                    "gross_declared": "1",
                }
            ).create({})

    @requires_normative_resources("1.0.4")
    def test_received_unequal_aed_headers_preserve_declarations_for_draft_review(self):
        original, rate, _source = self._foreign_received_fixture()
        binding = self._reviewed_binding(
            {"name": "Incoming declared headers"},
            capabilities=("receive",),
            note="Synthetic receive-only contract for unequal header declarations.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        cases = (
            ("cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']", "19.00", 19, 385.61),
            (
                "cac:AdditionalDocumentReference[cbc:DocumentTypeCode='aedtotal-incl-vat']/cbc:DocumentDescription",
                "385.62",
                18.36,
                385.62,
            ),
        )
        for index, (xpath, value, vat, gross) in enumerate(cases):
            with self.subTest(index=index):
                tree = etree.fromstring(etree.tostring(original))
                tree.find("cbc:UUID", NS).text = f"aaaaaaad-2222-4222-8222-{index:012d}"
                tree.find(xpath, NS).text = value
                content = etree.tostring(tree)
                inbox = receiver.receive_controlled_document(
                    binding.id,
                    f"declared-header-{index}",
                    content,
                )
                self.assertTrue(inbox.action_process(), inbox.diagnostic)
                self.assertTrue(
                    json.loads(inbox.processing_history_ids.validation_report)["valid"]
                )
                bill = inbox.move_id
                self.assertEqual((bill.state, bill.amount_total), ("draft", 105))
                self.assertEqual(bill.l10n_ae_received_tax_amount, vat)
                self.assertEqual(bill.l10n_ae_received_tax_inclusive_amount, gross)
                comparison = bill.l10n_ae_received_aed_comparison
                self.assertEqual(comparison["vat_converted"], "18.36")
                self.assertEqual(comparison["gross_converted"], "385.61")
                unchanged_fields = [
                    "invoice_date",
                    "write_uid",
                    "write_date",
                    "l10n_ae_received_tax_amount",
                    "l10n_ae_received_tax_inclusive_amount",
                    "l10n_ae_received_aed_comparison",
                ]
                # Compare persisted values across Form's native cache refresh,
                # rather than mixing creation-time cache and database metadata.
                self.env.flush_all()
                self.env.invalidate_all()
                before_form_read = bill.read(unchanged_fields)
                # Inspect the readonly notice without the context manager's
                # implicit save of unrelated native/Enterprise form fields.
                form = Form(bill)
                self.assertIn("before posting", form.l10n_ae_received_aed_review)
                self.assertEqual(bill.read(unchanged_fields), before_form_read)
                notice = bill.l10n_ae_received_aed_review
                rate.rate = 1 / 3.70
                bill.invoice_line_ids[0].price_unit = 99
                self.assertEqual(bill.l10n_ae_received_aed_comparison, comparison)
                self.assertEqual(bill.l10n_ae_received_aed_review, notice)
                self.assertEqual(inbox.original_xml.content, content)
                self.assertFalse(bill.l10n_ae_transmission_ids)

    @requires_normative_resources("1.0.4")
    def test_received_malformed_fx_headers_stay_in_review_without_a_draft(self):
        original, _rate, _source = self._foreign_received_fixture()
        binding = self._reviewed_binding(
            {"name": "Incoming FX arithmetic"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        cases = (
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
                inbox = (
                    self.env["l10n_ae.edi.inbox"]
                    .with_user(self.simple_accountman)
                    .receive_controlled_document(
                        binding.id,
                        f"bad-fx-header-{index}",
                        content,
                    )
                )
                self.assertFalse(inbox.action_process())
                self.assertEqual(inbox.state, "review")
                self.assertFalse(inbox.move_id)
                self.assertIn("fiscal AED", inbox.diagnostic)
                self.assertEqual(inbox.original_xml.content, content)
                self.assertEqual(self.env["account.move"].search_count([]), before)

    @requires_normative_resources("1.0.4")
    def test_received_fx_line_currency_and_conversion_require_review(self):
        original, _rate, _source = self._foreign_received_fixture()
        binding = self._reviewed_binding(
            {"name": "Incoming FX line arithmetic"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        cases = (
            (
                "cac:InvoiceLine/cac:ItemPriceExtension/cbc:Amount",
                "385.62",
                None,
                "fiscal AED",
            ),
            (
                "cac:InvoiceLine/cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount",
                "18.37",
                None,
                "fiscal AED",
            ),
            (
                "cac:InvoiceLine/cac:ItemPriceExtension/cbc:Amount",
                None,
                "USD",
                "KMEE supplemental BTAE-10-currency",
            ),
            (
                "cac:InvoiceLine/cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount",
                None,
                "USD",
                "KMEE supplemental BTAE-08-currency",
            ),
        )
        before = self.env["account.move"].search_count([])
        for index, (xpath, value, currency, diagnostic) in enumerate(cases):
            with self.subTest(index=index):
                tree = etree.fromstring(etree.tostring(original))
                tree.find("cbc:UUID", NS).text = f"aaaaaaac-3333-4333-8333-{index:012d}"
                node = tree.find(xpath, NS)
                if value:
                    node.text = value
                if currency:
                    node.set("currencyID", currency)
                content = etree.tostring(tree)
                inbox = (
                    self.env["l10n_ae.edi.inbox"]
                    .with_user(self.simple_accountman)
                    .receive_controlled_document(
                        binding.id,
                        f"bad-fx-line-{index}",
                        content,
                    )
                )
                self.assertFalse(inbox.action_process())
                self.assertEqual(inbox.state, "review")
                self.assertFalse(inbox.move_id)
                self.assertIn(diagnostic, inbox.diagnostic)
                self.assertEqual(inbox.original_xml.content, content)
                self.assertEqual(self.env["account.move"].search_count([]), before)
                if currency:
                    history = inbox.processing_history_ids
                    self.assertEqual(len(history), 1)
                    report = json.loads(history.validation_report)
                    self.assertFalse(report["valid"])
                    self.assertTrue(report["xsd_valid"])
                    self.assertTrue(
                        all(not layer["failures"] for layer in report["layers"])
                    )
                    supplement = next(
                        check
                        for check in report["supplemental_checks"]
                        if check["name"] == "kmee-ae-line-aed-currency-v1"
                    )
                    self.assertEqual(
                        [failure["code"] for failure in supplement["failures"]],
                        [diagnostic.removeprefix("KMEE supplemental ")],
                    )
                    self.assertEqual(
                        history.validation_report_hash,
                        hashlib.sha256(history.validation_report.encode()).hexdigest(),
                    )

    @requires_normative_resources("1.0.4")
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
        binding = self._reviewed_binding(
            {"name": "Incoming fractional quantity"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id,
                "fractional-quantity",
                etree.tostring(tree),
            )
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

    @requires_normative_resources("1.0.4")
    def test_received_fractional_discount_keeps_exact_original_and_aed_declarations(
        self,
    ):
        tree, _rate, _source = self._foreign_received_fixture()
        # Independent sender worksheet: 400.1234 * .125 less10% gives raw
        # net45.0138825/VAT2.250694125/gross47.264576625 USD. The sender's
        # declared AED line VAT/gross are8.2656741740625/173.5791576553125.
        # Build a synthetic supplier fixture; do not alter an issued original.
        values = {
            "cbc:UUID": "dadadada-2222-4222-8222-222222222222",
            "cac:InvoiceLine/cbc:InvoicedQuantity": "0.125",
            "cac:InvoiceLine/cbc:LineExtensionAmount": "45.01",
            "cac:InvoiceLine/cac:Price/cbc:PriceAmount": "400.1234",
            "cac:InvoiceLine/cac:Price/cac:AllowanceCharge/cbc:BaseAmount": "400.1234",
            "cac:InvoiceLine/cac:ItemPriceExtension/cbc:Amount": "173.5791576553125",
            "cac:InvoiceLine/cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount": (
                "8.2656741740625"
            ),
            "cac:TaxTotal/cbc:TaxAmount[@currencyID='USD']": "2.25",
            "cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']": "8.26",
            "cac:TaxTotal/cac:TaxSubtotal/cbc:TaxableAmount": "45.01",
            "cac:TaxTotal/cac:TaxSubtotal/cbc:TaxAmount": "2.25",
            "cac:LegalMonetaryTotal/cbc:LineExtensionAmount": "45.01",
            "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount": "45.01",
            "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount": "47.26",
            "cac:LegalMonetaryTotal/cbc:PayableAmount": "47.26",
            (
                "cac:AdditionalDocumentReference"
                "[cbc:DocumentTypeCode='aedtotal-incl-vat']/cbc:DocumentDescription"
            ): "173.56",
        }
        for xpath, value in values.items():
            tree.find(xpath, NS).text = value
        line = tree.find("cac:InvoiceLine", NS)
        allowance = etree.Element("{" + NS["cac"] + "}AllowanceCharge")
        for name, text, currency in [
            ("ChargeIndicator", "false", None),
            ("AllowanceChargeReasonCode", "95", None),
            ("AllowanceChargeReason", "Discount", None),
            ("MultiplierFactorNumeric", "10", None),
            ("Amount", "5.0015425", "USD"),
            ("BaseAmount", "50.015425", "USD"),
        ]:
            node = etree.SubElement(allowance, "{" + NS["cbc"] + "}" + name)
            node.text = text
            if currency:
                node.set("currencyID", currency)
        line.find("cbc:LineExtensionAmount", NS).addnext(allowance)
        content = etree.tostring(tree)
        binding = self._reviewed_binding(
            {"name": "Synthetic received fractional discount"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        self.assertFalse(receiver.env.su)
        inbox = receiver.receive_controlled_document(
            binding.id, "fractional-discount-original", content
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        bill = inbox.move_id
        self.assertEqual((bill.move_type, bill.state), ("in_invoice", "draft"))
        self.assertEqual(bill.invoice_line_ids.quantity, 0.125)
        self.assertEqual(
            (bill.amount_untaxed, bill.amount_tax, bill.amount_total),
            (45.01, 2.25, 47.26),
        )
        self.assertEqual(
            (
                bill.l10n_ae_received_tax_amount,
                bill.l10n_ae_received_tax_inclusive_amount,
            ),
            (8.26, 173.56),
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertFalse(bill.l10n_ae_transmission_id)
        self.assertEqual(
            receiver.receive_controlled_document(
                binding.id, "fractional-discount-original", content
            ),
            inbox,
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(inbox.move_id, bill)
        self.assertEqual(inbox.original_xml.content, content)

    @requires_normative_resources("1.0.4")
    def test_received_credit_codes_normalize_while_unknown_reason_stays_in_review(self):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        source.l10n_ae_fiscal_uuid = "cccccccd-1111-4111-8111-111111111111"
        binding = self._reviewed_binding(
            {"name": "Received code review"},
            capabilities=("receive",),
            note="Synthetic reviewed receiving contract for code normalization.",
        )
        inboxes = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        original = inboxes.receive_controlled_document(
            binding.id, "code-original", etree.tostring(self._received_test_xml(source))
        )
        self.assertTrue(original.action_process(), original.diagnostic)
        credit = self.init_invoice("out_refund", amounts=[40], taxes=self.tax_sale_a)
        credit.write(
            {
                "reversed_entry_id": source.id,
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "cccccccd-2222-4222-8222-222222222222",
                "l10n_ae_billing_frequency_code": "OTH",
                "l10n_ae_billing_frequency_note": "Synthetic reviewed other cadence.",
            }
        )
        credit.action_post()
        for code, accepted in ((" \tDL8.61.1.C\n ", True), ("UNKNOWN", False)):
            with self.subTest(code=code):
                tree = self._received_test_xml(credit)
                tree.find("./{*}DiscrepancyResponse/{*}ResponseCode").text = code
                tree.find("./{*}InvoicePeriod/{*}DescriptionCode").text = " \tOTH\n "
                content = etree.tostring(tree)
                inbox = inboxes.receive_controlled_document(
                    binding.id,
                    "credit-code-" + ("padded" if accepted else "unknown"),
                    content,
                )
                self.assertEqual(inbox.action_process(), accepted, inbox.diagnostic)
                self.assertEqual(inbox.original_xml.content, content)
                self.assertEqual(inbox.xml_hash, hashlib.sha256(content).hexdigest())
                history = inbox.processing_history_ids
                self.assertEqual(len(history), 1)
                report = json.loads(history.validation_report)
                self.assertEqual(report["valid"], accepted)
                self.assertTrue(report["xsd_valid"])
                if accepted:
                    refund = inbox.move_id
                    self.assertEqual(
                        (refund.move_type, refund.state), ("in_refund", "draft")
                    )
                    self.assertEqual(refund.reversed_entry_id, original.move_id)
                    self.assertEqual(refund.l10n_ae_credit_reason, "DL8.61.1.C")
                    self.assertEqual(refund.l10n_ae_billing_frequency_code, "OTH")
                    self.assertEqual(
                        refund.l10n_ae_billing_frequency_note,
                        tree.findtext("./{*}Note"),
                    )
                    self.assertEqual(
                        (refund.amount_untaxed, refund.amount_tax, refund.amount_total),
                        (40, 2, 42),
                    )
                    self.assertTrue(inbox.action_process(), inbox.diagnostic)
                    self.assertEqual(inbox.move_id, refund)
                    self.assertEqual(inbox.processing_history_ids, history)
                else:
                    self.assertEqual(inbox.state, "review")
                    self.assertFalse(inbox.move_id)
                    self.assertIn("ibr-001-ae", inbox.diagnostic.lower())
                    self.assertFalse(history.move_id)
                    self.assertEqual(history.outcome, "review")

    @requires_normative_resources("1.0.4")
    def test_received_partial_credit_links_the_supplier_bill_and_preserves_reason(self):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        original.l10n_ae_fiscal_uuid = "aaaaaaab-1111-4111-8111-111111111111"
        binding = self._reviewed_binding(
            {"name": "Incoming supplier adjustment"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox_model = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
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

    @requires_normative_resources("1.0.4")
    def test_received_summary_credit_preserves_period_and_links_supplier_bill(self):
        self._assert_received_summary_credit(
            expected_flags="00010000", free_zone=False, beneficiary_id=False
        )

    @requires_normative_resources("1.0.4")
    def test_received_free_zone_summary_credit_preserves_original_and_period(self):
        self._assert_received_summary_credit(
            expected_flags="10010000",
            free_zone=True,
            beneficiary_id="1000000003",
        )

    @requires_normative_resources("1.0.4")
    def test_received_free_zone_summary_rejects_invalid_required_facts(self):
        credit_content, receiver, binding = self._assert_received_summary_credit(
            expected_flags="10010000",
            free_zone=True,
            beneficiary_id="1000000003",
        )
        source = self.init_invoice(
            "out_invoice", amounts=[40, 60], taxes=self.tax_sale_a, post=True
        )
        source.write(
            {
                "l10n_ae_summary": True,
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000003",
                "l10n_ae_invoicing_period_start": "2019-01-01",
                "l10n_ae_invoicing_period_end": "2019-01-31",
                "l10n_ae_fiscal_uuid": "cccccccf-3333-4333-8333-333333333333",
            }
        )
        invoice_content = etree.tostring(self._received_test_xml(source))
        content_by_type = {
            "CreditNote381": credit_content,
            "Invoice380": invoice_content,
        }
        expected_flags = {
            "missing-beneficiary": "ibr-007-ae",
            "missing-period": "ibr-138-ae",
            "reversed-period": "ibr-029",
        }
        model_names = (
            "account.move",
            "account.move.line",
            "l10n_ae.edi.transmission",
            "l10n_ae.edi.attempt",
            "l10n_ae.edi.event",
        )
        baseline_ids = {
            model: self.env[model].search([], order="id").ids for model in model_names
        }
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
        baseline_ledger = (
            self.env["account.move.line"].search([], order="id").read(ledger_fields)
        )
        baseline_move_timestamps = (
            self.env["account.move"]
            .search([], order="id")
            .read(["create_date", "write_date"])
        )
        validator = OfflinePintValidator(
            self.normative_directories["1.0.4"],
            ruleset="kmee-pint-ae-1.0.4-ubl-2.1",
        )
        for document_type, valid_content in content_by_type.items():
            control_tree = etree.fromstring(valid_content)
            self.assertEqual(
                control_tree.findtext("cbc:ProfileExecutionID", namespaces=NS),
                "10010000",
            )
            expected_type = "380" if document_type == "Invoice380" else "381"
            type_path = (
                "cbc:InvoiceTypeCode"
                if document_type == "Invoice380"
                else "cbc:CreditNoteTypeCode"
            )
            self.assertEqual(
                control_tree.findtext(type_path, namespaces=NS), expected_type
            )
            valid_control_report = validator.validate(valid_content)
            self.assertTrue(valid_control_report["valid"], valid_control_report)
            self.assertEqual(
                valid_control_report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1"
            )
            for vector_index, (vector, expected_rule) in enumerate(
                expected_flags.items(), start=1
            ):
                variant_index = (
                    0 if document_type == "CreditNote381" else 3
                ) + vector_index
                tree = etree.fromstring(valid_content)
                variant_uuid = f"eeeeee{variant_index:02d}-6666-4666-8666-666666666666"
                uuid_node = tree.find("cbc:UUID", NS)
                self.assertIsNotNone(uuid_node)
                uuid_node.text = variant_uuid
                if vector == "missing-beneficiary":
                    beneficiary = tree.find("cac:BuyerCustomerParty", NS)
                    self.assertIsNotNone(beneficiary)
                    tree.remove(beneficiary)
                elif vector == "missing-period":
                    period = tree.find("cac:InvoicePeriod", NS)
                    self.assertIsNotNone(period)
                    tree.remove(period)
                else:
                    period = tree.find("cac:InvoicePeriod", NS)
                    self.assertIsNotNone(period)
                    period.find("cbc:StartDate", NS).text = "2019-01-31"
                    period.find("cbc:EndDate", NS).text = "2019-01-01"
                content = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
                original_hash = hashlib.sha256(content).hexdigest()
                formal_report = validator.validate(content)
                self.assertTrue(formal_report["xsd_valid"], formal_report)
                self.assertFalse(formal_report["valid"], formal_report)
                self.assertEqual(formal_report["ruleset"], "kmee-pint-ae-1.0.4-ubl-2.1")
                failures = {
                    failure["code"]
                    for layer in formal_report["layers"]
                    for failure in layer["failures"]
                }
                self.assertEqual(failures, {expected_rule}, formal_report)
                inbox = receiver.receive_controlled_document(
                    binding.id,
                    f"invalid-{document_type}-{vector}",
                    content,
                )
                for _attempt in range(2):
                    self.assertFalse(inbox.action_process())
                    self.assertEqual(inbox.state, "review")
                    self.assertFalse(inbox.move_id)
                    self.assertEqual(inbox.original_xml.content, content)
                    self.assertEqual(inbox.xml_hash, original_hash)
                history = inbox.processing_history_ids.sorted("id")
                self.assertEqual(len(history), 2)
                report_texts = [row.validation_report for row in history]
                report_hashes = [row.validation_report_hash for row in history]
                self.assertTrue(all(report_texts))
                for index, report_text in enumerate(report_texts):
                    self.assertEqual(
                        report_hashes[index],
                        hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
                    )
                    retained_report = json.loads(report_text)
                    self.assertEqual(
                        retained_report["ruleset"],
                        "kmee-pint-ae-1.0.4-ubl-2.1",
                    )
                    self.assertTrue(retained_report["xsd_valid"])
                    self.assertFalse(retained_report["valid"])
                    retained_failures = {
                        failure["code"]
                        for layer in retained_report["layers"]
                        for failure in layer["failures"]
                    }
                    self.assertEqual(retained_failures, {expected_rule})
                self.assertEqual(
                    {
                        model: self.env[model].search([], order="id").ids
                        for model in model_names
                    },
                    baseline_ids,
                )
                self.assertEqual(
                    self.env["account.move.line"]
                    .search([], order="id")
                    .read(ledger_fields),
                    baseline_ledger,
                )
                self.assertEqual(
                    self.env["account.move"]
                    .search([], order="id")
                    .read(["create_date", "write_date"]),
                    baseline_move_timestamps,
                )

    @requires_normative_resources("1.0.4")
    def test_received_supplier_endpoint_must_match_the_known_vat_partner(self):
        tree = self._received_test_xml()
        tree.find(
            "cac:AccountingSupplierParty/cac:Party/cbc:EndpointID", NS
        ).text = "1000000009"
        binding = self._reviewed_binding(
            {"name": "Incoming endpoint check"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                binding.id, "supplier-mismatch", etree.tostring(tree)
            )
        )
        inbox.action_process()
        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)
        self.assertIn("supplier", inbox.diagnostic)

    @requires_normative_resources("1.0.4")
    def test_received_declared_amounts_keep_the_xml_currency_during_accounting_review(
        self,
    ):
        binding = self._reviewed_binding(
            {"name": "Received currency evidence"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        content = etree.tostring(self._received_test_xml())
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "received-currency", content)
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

    @requires_normative_resources("1.0.4")
    def test_received_commercial_invoice_matches_non_vat_supplier_registration(
        self,
    ):
        _source, purchase_tax, tree = self._commercial_received_fixture()
        self.assertIsNone(
            tree.find("cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme", NS)
        )
        content = etree.tostring(tree)
        binding = self._reviewed_binding(
            {"name": "Incoming commercial document"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "non-vat-commercial", content)
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

    @requires_normative_resources("1.0.4")
    def test_received_commercial_credit_links_the_non_vat_supplier_bill(
        self,
    ):
        source, purchase_tax, tree = self._commercial_received_fixture()
        binding = self._reviewed_binding(
            {"name": "Incoming commercial adjustment"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        model = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
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

    @requires_normative_resources("1.0.4")
    def test_received_credit_with_a_wrong_original_date_stays_in_review(self):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        original.l10n_ae_fiscal_uuid = "bbbbbbba-1111-4111-8111-111111111111"
        binding = self._reviewed_binding(
            {"name": "Incoming credit reference review"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox_model = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
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

    @requires_normative_resources("1.0.4")
    def test_received_identical_xml_with_another_remote_id_reuses_the_bill(self):
        content = etree.tostring(self._received_test_xml())
        binding = self._reviewed_binding(
            {"name": "Incoming duplicate check"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox_model = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
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
        foreign_manager = new_test_user(
            self.env,
            login="uae-foreign-document-manager",
            groups="account.group_account_manager",
            company_id=other.id,
            company_ids=[(6, 0, other.ids)],
        )
        foreign_binding = review_controlled_binding(
            foreign_binding.with_company(other),
            foreign_manager.with_company(other),
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        foreign = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(foreign_manager)
            .with_company(other)
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
        binding = self._reviewed_binding(
            {"name": "Inbound replay ASP"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox_model = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
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

    @requires_normative_resources("1.0.4")
    def test_received_branch_uses_parent_supplier_tax_and_purchase_journal(self):
        root = self.env.company
        self.partner_a.company_id = root
        content = etree.tostring(self._received_test_xml())
        branch = self.env["res.company"].create(
            {
                "name": "Synthetic received accounting branch",
                "parent_id": root.id,
                "country_id": root.country_id.id,
                "currency_id": root.currency_id.id,
                "l10n_ae_tin": root.l10n_ae_tin,
                "l10n_ae_tin_confirmed": True,
                "l10n_ae_edi_auto_prepare": False,
            }
        )
        self.env["account.chart.template"].with_company(branch).try_loading(
            "ae", branch, install_demo=False
        )
        manager = new_test_user(
            self.env,
            login="synthetic-received-branch-manager",
            groups="account.group_account_manager",
            company_id=branch.id,
            company_ids=branch.ids,
        )
        actor = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(manager)
            .with_context(allowed_company_ids=branch.ids)
            .with_company(branch)
        )
        self.assertFalse(actor.env.su)
        self.assertEqual(manager.company_ids, branch)
        self.assertEqual(self.partner_a.company_id, root)
        self.assertEqual(self.tax_purchase_a.company_id, root)
        purchase = self.company_data["default_journal_purchase"]
        self.assertEqual(purchase.company_id, root)
        self.assertEqual(
            self.env["account.journal"].search_count(
                [("company_id", "=", branch.id), ("type", "=", "purchase")]
            ),
            0,
        )
        binding = review_controlled_binding(
            actor.env["l10n_ae.edi.binding"].create(
                {"name": "Synthetic received branch contract", "company_id": branch.id}
            ),
            manager,
            capabilities=("receive",),
            note="Synthetic receive-only accounting branch master-data contract.",
        )
        inbox = actor.receive_controlled_document(
            binding.id, "received-branch-parent-masters", content
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        bill = inbox.move_id
        self.assertEqual(inbox.state, "processed")
        self.assertEqual((bill.move_type, bill.state), ("in_invoice", "draft"))
        self.assertEqual(bill.company_id, branch)
        self.assertEqual(bill.partner_id, self.partner_a)
        self.assertEqual(bill.journal_id, purchase)
        self.assertEqual(bill.invoice_line_ids.tax_ids, self.tax_purchase_a)
        self.assertEqual(
            (bill.amount_untaxed, bill.amount_tax, bill.amount_total), (100, 5, 105)
        )
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(inbox.xml_hash, hashlib.sha256(content).hexdigest())
        self.assertEqual(len(inbox.processing_history_ids), 1)
        report = json.loads(inbox.processing_history_ids.validation_report)
        self.assertTrue(report["xsd_valid"], report)
        self.assertTrue(report["valid"], report)
        replay = actor.receive_controlled_document(
            binding.id, "received-branch-parent-masters", content
        )
        self.assertEqual(replay, inbox)
        self.assertTrue(replay.action_process(), replay.diagnostic)
        self.assertEqual(replay.move_id, bill)
        self.assertEqual(len(replay.processing_history_ids), 1)
        self.assertEqual(
            actor.env["account.move"].search_count(
                [
                    ("company_id", "=", branch.id),
                    ("l10n_ae_received_document", "=", True),
                ]
            ),
            1,
        )
        self.assertFalse(bill.l10n_ae_transmission_id)

    @requires_normative_resources("1.0.4")
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
        binding = self._reviewed_binding(
            {"name": "Inbound test ASP"},
            capabilities=("receive",),
            note="Synthetic reviewed receive-only contract for this inbound fixture.",
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(binding.id, "received-invoice-1", content)
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
