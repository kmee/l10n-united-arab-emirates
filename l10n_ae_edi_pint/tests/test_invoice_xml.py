# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import io
import json
import os
import tempfile
import uuid
import zipfile
from decimal import Decimal
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}


@tagged("post_install", "-at_install")
class TestUaeInvoiceXml(AccountTestInvoicingCommon):
    def test_received_review_pins_first_formal_normative_package_across_retry(self):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        source.l10n_ae_fiscal_uuid = "18181818-1818-4818-8818-181818181818"
        content = etree.tostring(self._received_test_xml(source))
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Pending incoming normative package review"}
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
                "KMEE_UAE_NORMATIVE_DIR": "/opt/uae-addons/.runtime/normative-1.0.3",
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
                "KMEE_UAE_NORMATIVE_DIR": "/opt/uae-addons/.runtime/normative",
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
                "KMEE_UAE_NORMATIVE_DIR": "/opt/uae-addons/.runtime/normative-1.0.3",
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

    def test_received_processing_retains_the_administrative_package_report(self):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        source.l10n_ae_fiscal_uuid = "17171717-1717-4717-8717-171717171717"
        content = etree.tostring(self._received_test_xml(source))
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Historical received package review"}
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
                "KMEE_UAE_NORMATIVE_DIR": "/opt/uae-addons/.runtime/normative-1.0.3",
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

    def test_pinned_identity_cache_respects_selected_company_access(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_fiscal_uuid = "16161616-1616-4616-8616-161616161616"
        invoice.l10n_ae_binding_id = self.env["l10n_ae.edi.binding"].create(
            {"name": "Company-sensitive pinned package review"}
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

    def test_native_form_preserves_readonly_prepared_package_identity(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        unprepared = Form(invoice)
        self.assertFalse(unprepared.l10n_ae_normative_ruleset)
        self.assertFalse(unprepared.l10n_ae_normative_manifest_sha256)
        invoice.l10n_ae_fiscal_uuid = "15151515-1515-4515-8515-151515151515"
        invoice.l10n_ae_binding_id = self.env["l10n_ae.edi.binding"].create(
            {"name": "Native pinned package review"}
        )
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        frozen = (issue.payload, issue.xml_content, issue.idempotency_key)
        with patch.dict(
            os.environ,
            {"KMEE_UAE_NORMATIVE_RULESET": "kmee-pint-ae-1.0.3-ubl-2.1"},
        ):
            prepared = Form(invoice)
            self.assertEqual(
                prepared.l10n_ae_normative_ruleset, "kmee-pint-ae-1.0.4-ubl-2.1"
            )
            self.assertEqual(
                prepared.l10n_ae_normative_manifest_sha256,
                "4f225c8095a2f487c5813b4d0e6515c9e7f986ba1a36e34dca7ae305b7899d76",
            )
            with self.assertRaisesRegex(AssertionError, "readonly field"):
                prepared.l10n_ae_normative_ruleset = "kmee-pint-ae-1.0.3-ubl-2.1"
            with self.assertRaisesRegex(AssertionError, "readonly field"):
                prepared.l10n_ae_normative_manifest_sha256 = "0" * 64
        self.assertEqual(
            (issue.payload, issue.xml_content, issue.idempotency_key), frozen
        )
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.validation_receipt)
        self.assertEqual(invoice.amount_total, 105)

    def test_prepared_packages_survive_configuration_swap_and_rollback(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_fiscal_uuid = "13131313-1313-4313-8313-131313131313"
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic normative rollback binding"}
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
                "KMEE_UAE_NORMATIVE_DIR": "/opt/uae-addons/.runtime/normative-1.0.3",
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
        invoice.l10n_ae_binding_id = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic pinned normative package"}
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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Integrated foreign export buyer"})
                .id,
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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Explicit foreign VAT review"})
                .id,
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

    def test_native_form_can_record_the_reviewed_non_peppol_export_buyer(self):
        self.partner_a.routing_endpoint = False
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=False
        )
        with Form(invoice) as form:
            form.l10n_ae_exports = True
            form.l10n_ae_export_buyer_no_peppol = True
            form.l10n_ae_buyer_onboarding_review_date = "2019-01-01"
            form.l10n_ae_buyer_onboarding_review_reference = (
                "SYNTH-NATIVE-EXPORT-REVIEW"
            )
        invoice.action_l10n_ae_validate()
        self.assertTrue(invoice.l10n_ae_exports)
        self.assertTrue(invoice.l10n_ae_export_buyer_no_peppol)
        self.assertEqual(
            invoice.l10n_ae_buyer_onboarding_review_reference,
            "SYNTH-NATIVE-EXPORT-REVIEW",
        )
        self.assertEqual((invoice.state, invoice.amount_total), ("draft", 105))
        self.assertFalse(invoice.l10n_ae_transmission_id)

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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Reviewed foreign export destination"})
                .id,
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

    def test_export99_requires_explicit_dated_recipient_review_before_posting(self):
        self.env.company.l10n_ae_edi_auto_prepare = True
        self.partner_a.routing_endpoint = False
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Reviewed export recipient"}
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
                with self.assertRaisesRegex(UserError, diagnostic):
                    invoice.action_post()
                self.assertEqual(invoice.state, "draft")
                self.assertFalse(invoice.l10n_ae_transmission_id)

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

    def test_disclosed_agent_invalid_principal_blocks_posting_with_specific_diagnostic(
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
                with self.assertRaisesRegex(UserError, diagnostic):
                    invoice.action_post()
                self.assertEqual(invoice.state, "draft")
                self.assertFalse(invoice.l10n_ae_transmission_id)

    def test_prepared_disclosed_agent_issue_freezes_principal_and_scenario(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_disclosed_agent": True,
                "l10n_ae_principal_id": "100000000300003",
                "l10n_ae_fiscal_uuid": "aaaaaaaf-2222-4222-8222-222222222222",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Disclosed agent preserved issue"})
                .id,
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
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Received agent"})
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

    def test_native_invoice_form_can_review_agent_and_free_zone_identifiers(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        with Form(invoice) as form:
            form.l10n_ae_disclosed_agent = True
            form.l10n_ae_principal_id = "100000000300003"
            form.l10n_ae_free_zone = True
            form.l10n_ae_beneficiary_id = "1000000004"
        invoice.action_l10n_ae_validate()
        self.assertEqual(invoice.l10n_ae_principal_id, "100000000300003")
        xml = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        self.assertEqual(
            xml.findtext("cbc:ProfileExecutionID", namespaces=NS), "10000100"
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
                "cac:BuyerCustomerParty/cac:Party/cac:PartyIdentification/cbc:ID",
                namespaces=NS,
            ),
            "1000000004",
        )

    def test_principal_without_agent_classification_requires_review(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.l10n_ae_principal_id = "100000000300003"
        with self.assertRaisesRegex(UserError, "disclosed-agent"):
            invoice.action_l10n_ae_validate()
        self.assertEqual(invoice.state, "draft")
        self.assertFalse(invoice.l10n_ae_transmission_id)

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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Agent principal defects"}
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

    def test_native_agent_credit_retains_original_identifiers_as_reviewable_defaults(
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
                "l10n_ae_fiscal_uuid": "aaaaaaaf-7777-4777-8777-777777777777",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Prepared agent source for native reversal"})
                .id,
            }
        )
        original.action_l10n_ae_prepare()
        original_issue = original.l10n_ae_transmission_id
        original_xml = original_issue.xml_content
        self.assertTrue(original_issue)
        self.assertTrue(original.l10n_ae_fiscal_uuid)
        duplicate = original.copy()
        self.assertFalse(duplicate.l10n_ae_disclosed_agent)
        self.assertFalse(duplicate.l10n_ae_principal_id)
        self.assertFalse(duplicate.l10n_ae_free_zone)
        self.assertFalse(duplicate.l10n_ae_beneficiary_id)
        wizard = (
            self.env["account.move.reversal"]
            .with_context(active_model="account.move", active_ids=original.ids)
            .create(
                {
                    "reason": "Synthetic agent consideration reduction",
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                    "journal_id": original.journal_id.id,
                    "date": original.invoice_date,
                }
            )
        )
        wizard.refund_moves()
        credit = wizard.new_move_ids
        self.assertEqual((credit.state, credit.move_type), ("draft", "out_refund"))
        self.assertTrue(credit.l10n_ae_disclosed_agent)
        self.assertTrue(credit.l10n_ae_free_zone)
        self.assertEqual(credit.l10n_ae_principal_id, "100000000300003")
        self.assertEqual(credit.l10n_ae_beneficiary_id, "1000000004")
        self.assertFalse(credit.l10n_ae_fiscal_uuid)
        self.assertFalse(credit.l10n_ae_transmission_id)
        credit.l10n_ae_principal_id = "100000000500003"
        credit.invoice_line_ids.price_unit = 40
        credit.l10n_ae_fiscal_uuid = "aaaaaaaf-6666-4666-8666-666666666666"
        credit.action_post()
        xml = etree.fromstring(credit.preview_l10n_ae_pint().encode())
        self.assertEqual(
            xml.findtext("cbc:ProfileExecutionID", namespaces=NS), "10000100"
        )
        self.assertEqual(
            xml.findtext(
                "cac:SellerSupplierParty/cac:Party/cac:PartyIdentification/cbc:ID",
                namespaces=NS,
            ),
            "100000000500003",
        )
        self.assertTrue(credit.validate_l10n_ae_pint()["valid"])
        self.assertEqual((credit.amount_tax, credit.amount_total), (2, 42))
        self.assertEqual(original.l10n_ae_principal_id, "100000000300003")
        self.assertEqual(original.l10n_ae_transmission_id, original_issue)
        self.assertEqual(original.preview_l10n_ae_pint(), original_xml)

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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Complete identity evidence access"}
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Identity archive integrity"}
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Identity review actor cache"}
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

    def test_identity_selection_requires_reading_every_linked_accounting_document(self):
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Hidden linked accounting"}
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

    def test_cached_identity_package_refreshes_after_another_original_is_processed(
        self,
    ):
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Identity package freshness"}
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

    def test_received_package_preserves_every_identity_selection_and_original(self):
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Portable identity review"}
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

    def test_retained_review_form_cannot_omit_or_expose_a_hidden_original(self):
        receiver = self.env["l10n_ae.edi.inbox"].with_user(self.simple_accountman)
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Review form authority"}
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Open review changes"}
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

    def test_manager_selects_original_through_native_review_form(self):
        tree = self._received_test_xml()
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Native selection form"}
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Actual processing authority"}
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
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Selection access"})
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
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Selection revision"})
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

    def test_selection_keeps_the_linked_original_and_restores_internal_resolution(self):
        tree = self._received_test_xml()
        content = etree.tostring(tree)
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Pinned identity review"}
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

    def test_reviewed_identity_variant_creates_only_its_one_draft(self):
        tree = self._received_test_xml()
        first_content = etree.tostring(tree)
        tree.insert(0, etree.Comment("Synthetic variant for explicit human review"))
        selected_content = etree.tostring(tree)
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Human identity review"}
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

    def test_received_identity_conflict_includes_an_unreadable_original(self):
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Restricted original identity"}
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

    def test_internal_review_preserves_authority_and_hidden_original_access(self):
        reader = new_test_user(
            self.env,
            login="synthetic-internal-review-reader",
            groups="account.group_account_readonly",
            company_id=self.env.company.id,
            company_ids=[(6, 0, self.env.company.ids)],
        )
        content = etree.tostring(self._received_test_xml())
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Review source access"}
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Revoked review owner"}
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

    def test_processed_original_without_a_request_cannot_open_a_new_review(self):
        content = etree.tostring(self._received_test_xml())
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Already processed original"}
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

    def test_supplier_recovery_resolves_internal_review_without_erasing_history(self):
        content = etree.tostring(self._received_test_xml())
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic incoming review resolution"}
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

    def test_archived_supplier_review_requests_one_internal_activity(self):
        content = etree.tostring(self._received_test_xml())
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic incoming review request"}
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

    def _foreign_received_fixture(self):
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
        source = self.init_invoice(
            "out_invoice",
            amounts=[100],
            taxes=self.tax_sale_a,
            currency=usd,
            post=True,
        )
        source.write(
            {
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": source.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-RECEIVED-FISCAL-QUOTE",
                "l10n_ae_fiscal_uuid": "aaaaaaac-1111-4111-8111-111111111111",
            }
        )
        return self._received_test_xml(source), rate, source

    def test_received_usd_partial_credit_preserves_aed_facts_and_original_reference(
        self,
    ):
        tree, _rate, source = self._foreign_received_fixture()
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming USD credit"}
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

    def test_received_inactive_document_currency_stays_in_review(self):
        tree, _rate, _source = self._foreign_received_fixture()
        self.env.ref("base.CHF").active = False
        tree.find("cbc:DocumentCurrencyCode", NS).text = "CHF"
        tree.find("cac:TaxExchangeRate/cbc:SourceCurrencyCode", NS).text = "CHF"
        for node in tree.xpath("//*[@currencyID='USD']"):
            node.set("currencyID", "CHF")
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Inactive CHF"})
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

    def test_received_usd_bill_preserves_sender_aed_facts_separately_from_local_review(
        self,
    ):
        tree, rate, _source = self._foreign_received_fixture()
        content = etree.tostring(tree)
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming USD evidence"}
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Received e-commerce delivery"}
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Received combined Free Zone e-commerce"}
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Combined Free Zone e-commerce missing declarations"}
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Received e-commerce delivery integrity"}
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

    def test_received_ecommerce_missing_required_delivery_stays_in_review(self):
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incomplete received e-commerce delivery"}
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Ambiguous received e-commerce delivery"}
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Received native e-commerce review"}
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

    def _received_test_xml(self, source=None):
        if source is None:
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

    def test_repeated_credit_delivery_keeps_one_refund_and_original_reference(self):
        source = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        source.l10n_ae_fiscal_uuid = "bbbbbbbc-1111-4111-8111-111111111111"
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Credit delivery events"}
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Protected event evidence"}
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Event versus document identity"}
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

    def test_received_event_package_retains_every_conflicting_association(self):
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Portable events"})
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

    def test_reused_delivery_event_preserves_associations_and_blocks_processing(self):
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Conflicting event"})
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

    def test_received_events_replay_without_duplicate_original_or_bill(self):
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Event identity"})
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
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Received Free Zone"})
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Ambiguous received beneficiary"}
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

    def test_received_fx_header_discrepancies_stay_in_review_without_a_draft(self):
        original, _rate, _source = self._foreign_received_fixture()
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming FX arithmetic"}
        )
        cases = (
            ("cac:TaxTotal/cbc:TaxAmount[@currencyID='AED']", "19.00"),
            (
                "cac:AdditionalDocumentReference[cbc:DocumentTypeCode='aedtotal-incl-vat']/cbc:DocumentDescription",
                "385.62",
            ),
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

    def test_received_fx_line_currency_and_conversion_require_review(self):
        original, _rate, _source = self._foreign_received_fixture()
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming FX line arithmetic"}
        )
        cases = (
            ("cac:InvoiceLine/cac:ItemPriceExtension/cbc:Amount", "385.62", None),
            (
                "cac:InvoiceLine/cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount",
                "18.37",
                None,
            ),
            ("cac:InvoiceLine/cac:ItemPriceExtension/cbc:Amount", None, "USD"),
            (
                "cac:InvoiceLine/cac:ItemPriceExtension/cac:TaxTotal/cbc:TaxAmount",
                None,
                "USD",
            ),
        )
        for index, (xpath, value, currency) in enumerate(cases):
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
                self.assertIn("fiscal AED", inbox.diagnostic)
                self.assertEqual(inbox.original_xml.content, content)

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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming fractional quantity"}
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

    def test_received_partial_credit_links_the_supplier_bill_and_preserves_reason(self):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        original.l10n_ae_fiscal_uuid = "aaaaaaab-1111-4111-8111-111111111111"
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming supplier adjustment"}
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

    def test_received_supplier_endpoint_must_match_the_known_vat_partner(self):
        tree = self._received_test_xml()
        tree.find(
            "cac:AccountingSupplierParty/cac:Party/cbc:EndpointID", NS
        ).text = "1000000009"
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming endpoint check"}
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

    def test_received_declared_amounts_keep_the_xml_currency_during_accounting_review(
        self,
    ):
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Received currency evidence"}
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

    def _commercial_received_fixture(self):
        self.partner_a.vat = False
        outside_scope = self.tax_sale_a.copy(
            {
                "name": "Commercial outside scope",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        purchase_tax = self.env.company.account_purchase_tax_id.copy(
            {
                "name": "Reviewed outside-scope purchase",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        source = self.init_invoice("out_invoice", amounts=[100], taxes=outside_scope)
        source.write(
            {
                "l10n_ae_document_category": "commercial",
                "l10n_ae_fiscal_uuid": "ccccccca-1111-4111-8111-111111111111",
            }
        )
        source.action_post()
        tree = self._received_test_xml(source)
        return source, purchase_tax, tree

    def test_received_commercial_invoice_matches_non_vat_supplier_registration(
        self,
    ):
        _source, purchase_tax, tree = self._commercial_received_fixture()
        self.assertIsNone(
            tree.find("cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme", NS)
        )
        content = etree.tostring(tree)
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming commercial document"}
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

    def test_non_vat_supplier_with_a_different_license_stays_in_review(self):
        source, _purchase_tax, tree = self._commercial_received_fixture()
        tree.find(
            "cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:CompanyID",
            NS,
        ).text = "UNMAPPED-LICENSE-0042"
        content = etree.tostring(tree)
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming unknown license"}
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

    def test_received_commercial_credit_links_the_non_vat_supplier_bill(
        self,
    ):
        source, purchase_tax, tree = self._commercial_received_fixture()
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming commercial adjustment"}
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

    def test_received_credit_with_a_wrong_original_date_stays_in_review(self):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        original.l10n_ae_fiscal_uuid = "bbbbbbba-1111-4111-8111-111111111111"
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming credit reference review"}
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

    def test_received_identical_xml_with_another_remote_id_reuses_the_bill(self):
        content = etree.tostring(self._received_test_xml())
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming duplicate check"}
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

    def test_unsafe_received_xml_is_preserved_without_creating_a_bill(self):
        content = b'<!DOCTYPE Invoice [<!ENTITY ex SYSTEM "file:///etc/passwd">]><Invoice>&ex;</Invoice>'
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Incoming XML check"})
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Incoming access check"}
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
        binding = self.env["l10n_ae.edi.binding"].create({"name": "Inbound replay ASP"})
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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "FX quote"})
                .id,
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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "FX mismatch"})
                .id,
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

    def test_fractional_usd_vat_divergence_requires_review_without_a_rounding_patch(
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
            post=True,
        )
        invoice.write(
            {
                "l10n_ae_tax_exchange_rate": "3.672500",
                "l10n_ae_tax_exchange_date": invoice.invoice_date,
                "l10n_ae_tax_exchange_source": "SYNTH-FRACTIONAL-ROUNDING-REVIEW",
                "l10n_ae_fiscal_uuid": "ddddddda-6666-4666-8666-666666666666",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Fractional VAT review"})
                .id,
            }
        )
        original_totals = (invoice.amount_total, invoice.amount_tax_signed)
        with self.assertRaisesRegex(UserError, "AED.*accounting"):
            invoice.action_l10n_ae_prepare()
        self.assertFalse(invoice.l10n_ae_transmission_id)
        self.assertEqual(
            (invoice.amount_total, invoice.amount_tax_signed), original_totals
        )
        self.assertEqual(invoice.amount_tax, 0.18)

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

    def test_controlled_preparation_assigns_one_uuid_without_preseeded_identity(self):
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Explicit UUID simulation",
                "fiscal_uuid_policy": "controlled_local",
            }
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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create(
                    {
                        "name": "UUID failure simulation",
                        "fiscal_uuid_policy": "controlled_local",
                    }
                )
                .id,
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Allocated credit test"}
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

    def test_commercial_standard_vat_is_rejected_before_posting_or_freezing(self):
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Invalid commercial simulation",
                "fiscal_uuid_policy": "controlled_local",
            }
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
                with self.assertRaisesRegex(UserError, "IBR-122-AE"):
                    move.action_post()
                self.assertEqual(move.state, "draft")
                self.assertEqual((move.amount_tax, move.amount_total), (5, 105))
                self.assertFalse(move.l10n_ae_fiscal_uuid)
                self.assertFalse(move.l10n_ae_transmission_id)

    def test_native_partial_commercial_credit_preserves_480_original_and_type_81(self):
        self.env.company.partner_id.vat = False
        self._assert_native_partial_commercial_credit()

    def test_registered_supplier_commercial_credit_preserves_outside_scope_category(
        self,
    ):
        self.assertTrue(self.env.company.partner_id.vat)
        self._assert_native_partial_commercial_credit()

    def _assert_native_partial_commercial_credit(self):
        outside_scope = self.tax_sale_a.copy(
            {
                "name": "Reviewed synthetic outside-scope sale",
                "amount": 0,
                "ubl_cii_tax_category_code": "O",
            }
        )
        original = self.init_invoice("out_invoice", amounts=[100], taxes=outside_scope)
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Commercial credit local simulation",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        original.write(
            {
                "l10n_ae_document_category": "commercial",
                "l10n_ae_binding_id": binding.id,
            }
        )
        original.action_post()
        original.action_l10n_ae_prepare()
        original_evidence = (
            original.l10n_ae_transmission_id.payload,
            original.l10n_ae_transmission_id.xml_content,
        )
        report = original.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        wizard = (
            self.env["account.move.reversal"]
            .with_context(active_model="account.move", active_ids=original.ids)
            .create(
                {
                    "reason": "Reviewed synthetic consideration reduction",
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                    "journal_id": original.journal_id.id,
                    "date": original.invoice_date,
                }
            )
        )
        wizard.refund_moves()
        credit = wizard.new_move_ids
        self.assertEqual(
            (credit.state, credit.l10n_ae_document_category), ("draft", "commercial")
        )
        credit.invoice_line_ids.price_unit = 40
        credit.l10n_ae_binding_id = binding
        credit.action_post()
        credit.action_l10n_ae_prepare()
        report = credit.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        tree = etree.fromstring(credit.l10n_ae_transmission_id.xml_content.encode())
        self.assertEqual(tree.findtext("cbc:CreditNoteTypeCode", namespaces=NS), "81")
        self.assertEqual(
            tree.findtext(
                "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID",
                namespaces=NS,
            ),
            original.name,
        )
        self.assertEqual(
            tree.findtext(
                "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", namespaces=NS
            ),
            "40.00",
        )
        self.assertEqual(
            tree.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=NS),
            "0.00",
        )
        self.assertEqual(
            (
                credit.amount_untaxed,
                credit.amount_tax,
                credit.amount_total,
                credit.amount_residual,
            ),
            (40, 0, 40, 0),
        )
        self.assertEqual(
            (original.state, original.amount_total, original.amount_residual),
            ("posted", 100, 60),
        )
        self.assertNotEqual(credit.l10n_ae_fiscal_uuid, original.l10n_ae_fiscal_uuid)
        self.assertEqual(
            (
                original.l10n_ae_transmission_id.payload,
                original.l10n_ae_transmission_id.xml_content,
            ),
            original_evidence,
        )

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

    def _selfbilling_bill_with_agreement(self):
        bill = self.init_invoice("in_invoice", amounts=[100], taxes=self.tax_purchase_a)
        bill.journal_id.is_self_billing = True
        agreement = self.env["l10n_ae.selfbilling.agreement"].create(
            {
                "name": "SYNTH-AGREEMENT-0002",
                "partner_id": self.partner_a.id,
                "start_date": bill.invoice_date,
                "scope": "Synthetic services and adjustments",
                "onboarding_reference": "SYNTH-ONBOARD-0002",
            }
        )
        bill.write(
            {
                "l10n_ae_selfbilling_agreement_id": agreement.id,
                "l10n_ae_fiscal_uuid": "ddddddda-3333-4333-8333-333333333333",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Synthetic self-billing sandbox"})
                .id,
            }
        )
        return bill, agreement

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

    def test_native_self_billed_partial_credit_requires_reviewed_agreement_and_261(
        self,
    ):
        bill, agreement = self._selfbilling_bill_with_agreement()
        binding = bill.l10n_ae_binding_id
        binding.fiscal_uuid_policy = "controlled_local"
        bill.l10n_ae_fiscal_uuid = False
        self.env.company.write(
            {"l10n_ae_edi_auto_prepare": True, "l10n_ae_edi_binding_id": binding.id}
        )
        bill.action_post()
        original_evidence = (
            bill.l10n_ae_transmission_id.payload,
            bill.l10n_ae_transmission_id.xml_content,
        )
        wizard = (
            self.env["account.move.reversal"]
            .with_context(active_model="account.move", active_ids=bill.ids)
            .create(
                {
                    "reason": "Reviewed self-billed consideration reduction",
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                    "journal_id": bill.journal_id.id,
                    "date": bill.invoice_date,
                }
            )
        )
        self.assertTrue(wizard.l10n_ae_credit_reason_applicable)
        wizard.refund_moves()
        credit = wizard.new_move_ids
        self.assertEqual(
            (credit.state, credit.move_type, credit.is_self_billing),
            ("draft", "in_refund", True),
        )
        self.assertEqual(credit.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertFalse(credit.l10n_ae_selfbilling_agreement_id)
        with self.assertRaisesRegex(UserError, "self-billing agreement"):
            credit.action_post()
        self.assertEqual(credit.state, "draft")
        self.assertFalse(credit.l10n_ae_fiscal_uuid)
        self.assertFalse(credit.l10n_ae_transmission_id)
        credit.l10n_ae_selfbilling_agreement_id = agreement
        credit.invoice_line_ids.price_unit = 40
        credit.action_post()
        report = credit.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["profile"], "selfbilling")
        frozen = credit.l10n_ae_transmission_id
        snapshot = json.loads(frozen.payload)
        self.assertEqual(snapshot["issuer_role"], "buyer")
        self.assertEqual(snapshot["supplier"]["vat"], "100000000200003")
        self.assertEqual(snapshot["buyer"]["vat"], "100000000100003")
        self.assertEqual(snapshot["selfbilling_agreement"]["reference"], agreement.name)
        tree = etree.fromstring(frozen.xml_content.encode())
        expected = {
            "cbc:CreditNoteTypeCode": "261",
            "cbc:CustomizationID": "urn:peppol:pint:selfbilling-1@ae-1",
            "cbc:ProfileID": "urn:peppol:bis:selfbilling",
            "cac:DiscrepancyResponse/cbc:ResponseCode": "DL8.61.1.C",
            "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID": bill.name,
            "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount": "40.00",
            "cac:TaxTotal/cbc:TaxAmount": "2.00",
            "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount": "42.00",
            "cac:LegalMonetaryTotal/cbc:PrepaidAmount": "42.00",
            "cac:LegalMonetaryTotal/cbc:PayableAmount": "0.00",
        }
        for path, value in expected.items():
            self.assertEqual(tree.findtext(path, namespaces=NS), value, path)
        self.assertEqual(
            (
                bill.amount_total,
                bill.amount_residual,
                credit.amount_total,
                credit.amount_residual,
            ),
            (105, 63, 42, 0),
        )
        self.assertNotEqual(credit.l10n_ae_fiscal_uuid, bill.l10n_ae_fiscal_uuid)
        self.assertEqual(
            (
                bill.l10n_ae_transmission_id.payload,
                bill.l10n_ae_transmission_id.xml_content,
            ),
            original_evidence,
        )

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

    def test_self_billing_posting_is_atomic_when_agreement_is_outside_its_term(self):
        bill, agreement = self._selfbilling_bill_with_agreement()
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": bill.l10n_ae_binding_id.id,
            }
        )
        agreement.write({"start_date": "2000-01-01", "end_date": "2000-12-31"})
        with self.assertRaisesRegex(UserError, "self-billing agreement"):
            bill.action_post()
        self.assertEqual(bill.state, "draft")
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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Deemed supply destination"})
                .id,
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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Buyer98 reviewed destination"})
                .id,
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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Buyer98 conventional presentation"})
                .id,
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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Preserved PDF invoice association"})
                .id,
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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Frozen XML invoice association"})
                .id,
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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Protected buyer98 review"})
                .id,
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
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Deemed reporting-only obligations"})
                .id,
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

    def test_prepared_free_zone_document_freezes_scenario_and_beneficiary_facts(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.write(
            {
                "l10n_ae_free_zone": True,
                "l10n_ae_beneficiary_id": "1000000003",
                "l10n_ae_fiscal_uuid": "aaaaaaad-1111-4111-8111-111111111111",
                "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"]
                .create({"name": "Free Zone snapshot"})
                .id,
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
