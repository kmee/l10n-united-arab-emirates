# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
import hashlib
import io
import json
import logging
import sys
import uuid
from contextlib import closing

from lxml import etree

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user
from odoo.tools.binary import BinaryBytes
from odoo.tools.pdf import PdfReader

from odoo.addons.l10n_ae_edi.tests.common import (
    CONTROLLED_CAPABILITIES,
)

from .common import NS, UaePintTestCommon

_logger = logging.getLogger(__name__)


@tagged("post_install", "-at_install")
class TestUaeInvoiceXmlSource(UaePintTestCommon):
    def test_vat_group_member_refuses_representative_endpoint_until_repaired(self):
        member = self.env.company
        member.l10n_ae_edi_auto_prepare = False
        group_trn = "100000004200003"
        representative = self.env["res.company"].create(
            {
                "name": "Synthetic VAT-group representative",
                "country_id": self.env.ref("base.ae").id,
                "account_fiscal_country_id": self.env.ref("base.ae").id,
                "currency_id": self.env.ref("base.AED").id,
                "vat": group_trn,
                "l10n_ae_tin": "1000000042",
                "l10n_ae_tin_confirmed": True,
            }
        )
        member.partner_id.write(
            {
                "vat": group_trn,
                "routing_scheme": "0235",
                "routing_endpoint": representative.l10n_ae_tin,
                "comment": "Synthetic VAT-group onboarding review: member keeps "
                "its own TIN1000000001, distinct from representative1000000042. "
                "Shared VAT TRN is not the member's electronic address.",
            }
        )
        self.assertNotEqual(member, representative)
        self.assertEqual(member.l10n_ae_tin, "1000000001")
        self.assertTrue(member.l10n_ae_tin_confirmed)
        self.assertEqual(member.vat, representative.vat)
        binding = self._reviewed_binding(
            {
                "name": "Synthetic VAT-group member sending contract",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic member-specific onboarding and sending review.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        self.assertFalse(invoice.env.user.has_group("base.group_system"))
        invoice.l10n_ae_binding_id = binding
        invoice.action_post()
        fields = [
            "state",
            "name",
            "company_id",
            "amount_untaxed",
            "amount_tax",
            "amount_total",
            "l10n_ae_fiscal_uuid",
            "l10n_ae_transmission_ids",
            "write_date",
        ]
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "write_date",
        ]
        before = invoice.read(fields)
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        self.assertFalse(
            invoice.l10n_ae_fiscal_uuid or invoice.l10n_ae_transmission_ids
        )
        for action in (invoice.action_l10n_ae_validate, invoice.action_l10n_ae_prepare):
            with self.assertRaisesRegex(UserError, "Routing Endpoint.*confirmed TIN"):
                action()
            self.assertEqual(invoice.read(fields), before)
            self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
            self.assertEqual(member.partner_id.routing_endpoint, "1000000042")
            self.assertEqual(member.l10n_ae_tin, "1000000001")
            self.assertEqual(member.vat, group_trn)
        member.partner_id.routing_endpoint = member.l10n_ae_tin
        invoice.action_l10n_ae_validate()
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        self.assertEqual(len(invoice.l10n_ae_transmission_ids), 1)
        self.assertEqual(issue.submission_state, "queued")
        self.assertTrue(invoice.l10n_ae_fiscal_uuid)
        self.assertFalse(issue.attempt_ids or issue.event_ids or issue.remote_id)
        tree = etree.fromstring(issue.xml_content.encode())
        endpoint = tree.find("cac:AccountingSupplierParty/cac:Party/cbc:EndpointID", NS)
        self.assertEqual(
            (endpoint.get("schemeID"), endpoint.text), ("0235", "1000000001")
        )
        self.assertNotEqual(endpoint.text, representative.l10n_ae_tin)
        self.assertEqual(
            tree.findtext(
                "cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID",
                namespaces=NS,
            ),
            group_trn,
        )
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        self.assertEqual(representative.l10n_ae_tin, "1000000042")
        self.assertEqual(representative.vat, group_trn)
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_uae_issuer_wrong_scheme_blocks_check_and_prepare_until_repaired(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        binding = self._reviewed_binding(
            {
                "name": "Synthetic issuer scheme review",
                "fiscal_uuid_policy": "controlled_local",
            },
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic UAE issuer scheme validation contract.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        self.assertFalse(invoice.env.user.has_group("base.group_system"))
        invoice.l10n_ae_binding_id = binding
        invoice.action_post()
        fields = [
            "state",
            "name",
            "amount_untaxed",
            "amount_tax",
            "amount_total",
            "l10n_ae_fiscal_uuid",
            "l10n_ae_transmission_ids",
            "write_date",
        ]
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "write_date",
        ]
        before = invoice.read(fields)
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        self.assertEqual(invoice.state, "posted")
        self.assertFalse(
            invoice.l10n_ae_fiscal_uuid or invoice.l10n_ae_transmission_ids
        )
        issuer = self.env.company.partner_id
        original_identity = (
            issuer.routing_endpoint,
            issuer.vat,
            self.env.company.l10n_ae_tin,
        )
        self.assertEqual(
            original_identity, ("1000000001", "100000000100003", "1000000001")
        )
        # A generic EAS value is syntactically selectable, but is not this UAE
        # entity's onboarded scheme. The endpoint and VAT/TIN stay unchanged.
        issuer.routing_scheme = "0209"
        self.assertEqual(issuer.routing_scheme, "0209")
        for action in (invoice.action_l10n_ae_validate, invoice.action_l10n_ae_prepare):
            with self.assertRaisesRegex(UserError, "UAE.*0235"):
                action()
            self.assertEqual(invoice.read(fields), before)
            self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
            self.assertEqual(
                (issuer.routing_endpoint, issuer.vat, self.env.company.l10n_ae_tin),
                original_identity,
            )
        issuer.routing_scheme = "0235"
        invoice.action_l10n_ae_validate()
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        self.assertEqual(len(invoice.l10n_ae_transmission_ids), 1)
        self.assertEqual(issue.submission_state, "queued")
        self.assertTrue(invoice.l10n_ae_fiscal_uuid)
        self.assertFalse(issue.attempt_ids or issue.event_ids or issue.remote_id)
        tree = etree.fromstring(issue.xml_content.encode())
        endpoint = tree.find("cac:AccountingSupplierParty/cac:Party/cbc:EndpointID", NS)
        self.assertEqual(
            (endpoint.get("schemeID"), endpoint.text), ("0235", "1000000001")
        )
        self.assertEqual(
            tree.findtext(
                "cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID",
                namespaces=NS,
            ),
            "100000000100003",
        )
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertEqual(
            (
                invoice.state,
                invoice.amount_untaxed,
                invoice.amount_tax,
                invoice.amount_total,
            ),
            ("posted", 100, 5, 105),
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)

    def test_uae_participant_preserves_registered_identity_at_foreign_address(self):
        self.env.company.write(
            {
                "country_id": self.env.ref("base.us").id,
                "account_fiscal_country_id": self.env.ref("base.ae").id,
                "l10n_ae_edi_auto_prepare": False,
            }
        )
        supplier = self.env.company.partner_id
        supplier.write(
            {
                "state_id": self.env.ref("base.state_us_5").id,
                "city": "San Francisco",
                "zip": "94105",
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        self.assertFalse(invoice.env.user.has_group("base.group_system"))
        self.assertEqual(invoice.country_code, "AE")
        self.assertEqual(supplier.country_code, "US")
        self.assertEqual(supplier.routing_scheme, "0235")
        ledger_fields = ["account_id", "debit", "credit", "balance", "write_date"]
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        invoice.action_l10n_ae_validate()
        xml = etree.fromstring(invoice.preview_l10n_ae_pint().encode())
        party = xml.find("cac:AccountingSupplierParty/cac:Party", NS)
        self.assertEqual(
            party.findtext(
                "cac:PostalAddress/cac:Country/cbc:IdentificationCode", namespaces=NS
            ),
            "US",
        )
        registration = party.find("cac:PartyLegalEntity/cbc:CompanyID", NS)
        self.assertEqual(registration.text, "SYNTH-TRADE-0001")
        self.assertEqual(registration.get("schemeAgencyID"), "TL")
        self.assertEqual(
            registration.get("schemeAgencyName"), "Synthetic Free Zone Authority"
        )
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertEqual(invoice.state, "draft")
        self.assertFalse(
            invoice.l10n_ae_fiscal_uuid or invoice.l10n_ae_transmission_ids
        )

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

    def test_principal_without_agent_classification_requires_review(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.l10n_ae_principal_id = "100000000300003"
        with self.assertRaisesRegex(UserError, "disclosed-agent"):
            invoice.action_l10n_ae_validate()
        self.assertEqual(invoice.state, "draft")
        self.assertFalse(invoice.l10n_ae_transmission_id)

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

    def test_incoming_reviewer_assignment_follows_fiscal_country_not_address(self):
        ae = self.env.ref("base.ae")
        fr = self.env.ref("base.fr")
        company_a = self.env.company
        company_a.partner_id.vat = False
        company_a.write(
            {"country_id": fr.id, "account_fiscal_country_id": ae.id, "state_id": False}
        )
        company_b = self.setup_other_company(
            name="Synthetic foreign fiscal incoming review"
        )["company"]
        company_b.write({"country_id": ae.id, "account_fiscal_country_id": fr.id})
        reviewer = new_test_user(
            self.env,
            login="synthetic-fiscal-incoming-reviewer",
            groups="account.group_account_manager",
            company_id=company_a.id,
            company_ids=[(6, 0, (company_a | company_b).ids)],
        )
        configurator = new_test_user(
            self.env,
            login="synthetic-fiscal-review-configurator",
            groups="account.group_account_manager,base.group_erp_manager",
            company_id=company_a.id,
            company_ids=[(6, 0, (company_a | company_b).ids)],
        )
        for company, address, fiscal, allowed in (
            (company_a, "FR", "AE", True),
            (company_b, "AE", "FR", False),
        ):
            with self.subTest(address=address, fiscal=fiscal):
                target = company.with_user(configurator).with_context(
                    allowed_company_ids=[company.id]
                )
                self.assertFalse(target.env.su)
                self.assertFalse(configurator.has_group("base.group_system"))
                self.assertEqual(target.country_code, address)
                self.assertEqual(target.account_fiscal_country_id.code, fiscal)
                self.assertFalse(target.l10n_ae_inbox_reviewer_id)
                if allowed:
                    target.write({"l10n_ae_inbox_reviewer_id": reviewer.id})
                    self.assertEqual(target.l10n_ae_inbox_reviewer_id.id, reviewer.id)
                else:
                    with (
                        self.assertRaisesRegex(
                            ValidationError, "incoming document reviewer"
                        ),
                        self.cr.savepoint(),
                    ):
                        target.write({"l10n_ae_inbox_reviewer_id": reviewer.id})
                    self.assertFalse(target.l10n_ae_inbox_reviewer_id)
                self.assertEqual(target.country_code, address)
                self.assertEqual(target.account_fiscal_country_id.code, fiscal)

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

    def test_buyer98_pdf_preserves_private_original_in_invoice_company(self):
        company = self.env.company
        other_company = self.env["res.company"].create(
            {"name": "Synthetic current PDF company B"}
        )
        manager = self.simple_accountman
        manager.company_ids |= other_company
        reader = new_test_user(
            self.env,
            login="synthetic-pdf-original-reader",
            groups="account.group_account_readonly",
            company_ids=company.ids,
            company_id=company.id,
        )
        self.partner_a.write(
            {"name": "Synthetic Buyer98 Attachment Original", "routing_endpoint": False}
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        ).with_user(manager)
        invoice.write(
            {
                "l10n_ae_buyer_not_implemented": True,
                "l10n_ae_buyer_onboarding_review_date": "2019-01-01",
                "l10n_ae_buyer_onboarding_review_reference": "SYNTH-PDF-ORIGINAL",
                "l10n_ae_fiscal_uuid": "aaaaaaaf-7777-4777-8777-777777777777",
                "l10n_ae_binding_id": self._reviewed_binding(
                    {"name": "Synthetic private PDF original"},
                    capabilities=CONTROLLED_CAPABILITIES,
                    note="Synthetic review of the buyer98 preserved PDF contract.",
                ).id,
            }
        )
        self.assertFalse(invoice.env.su)
        self.assertFalse(manager.has_group("base.group_system"))
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        before = invoice.read(["state", "amount_untaxed", "amount_tax", "amount_total"])
        ledger_fields = ["debit", "credit", "balance", "amount_currency", "write_date"]
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        current = invoice.with_context(
            allowed_company_ids=(other_company | company).ids
        )
        self.assertEqual(current.env.company, other_company)
        self.assertEqual(issue.company_id, company)
        # Let the real renderer request its assets through the native test server.
        with self.allow_pdf_render():
            action = current.action_l10n_ae_generate_conventional_pdf()
        expected_url = (
            f"/web/content/l10n_ae.edi.transmission/{issue.id}/"
            "conventional_pdf_content?download=true&filename_field=conventional_filename"
        )
        self.assertEqual(action["url"], expected_url)
        content = issue.conventional_pdf_content.content
        self.assertTrue(content.startswith(b"%PDF-"))
        text = " ".join(
            " ".join(
                page.extract_text() for page in PdfReader(io.BytesIO(content)).pages
            ).split()
        )
        self.assertIn("Buyer98", text)
        self.assertIn(invoice.name, text)
        self.assertIn("105.00", text)
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .search(
                [
                    ("res_model", "=", issue._name),
                    ("res_id", "=", issue.id),
                    ("res_field", "=", "conventional_pdf_original_content"),
                ]
            )
        )
        self.assertEqual(
            len(attachment),
            1,
            "Generate must preserve one private PDF original attachment",
        )
        self.assertEqual(attachment.company_id, company)
        self.assertFalse(attachment.public)
        self.assertFalse(attachment.access_token)
        digest, generated_at = (
            issue.conventional_pdf_hash,
            issue.conventional_pdf_generated_at,
        )
        self.assertEqual(hashlib.sha256(content).hexdigest(), digest)
        self.assertEqual(invoice.action_l10n_ae_generate_conventional_pdf(), action)
        self.assertEqual(invoice.with_user(reader).action_print_pdf(), action)
        self.assertEqual(
            issue.with_user(reader).conventional_pdf_content.content, content
        )
        self.assertEqual(
            (issue.conventional_pdf_hash, issue.conventional_pdf_generated_at),
            (digest, generated_at),
        )
        self.assertEqual(
            invoice.read(["state", "amount_untaxed", "amount_tax", "amount_total"]),
            before,
        )
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        for actor in (manager, reader):
            for record, field in (
                (issue.with_user(actor), "conventional_pdf_original_content"),
                (attachment.with_user(actor), "raw"),
            ):
                with (
                    self.subTest(actor=actor.login, field=field),
                    self.assertRaises(AccessError),
                ):
                    record.read([field])
        # Storage faults are fixture injections; the public field observes refusal.
        for fault in ("hash", "bytes"):
            with closing(self.cr.savepoint()):
                if fault == "hash":
                    self.cr.execute(
                        "UPDATE l10n_ae_edi_transmission "
                        "SET conventional_pdf_hash = %s WHERE id = %s",
                        ["0" * 64, issue.id],
                    )
                else:
                    attachment.write({"raw": BinaryBytes(b"Corrupt PDF original\n")})
                issue.invalidate_recordset(
                    [
                        "conventional_pdf_content",
                        "conventional_pdf_original_content",
                        "conventional_pdf_hash",
                    ]
                )
                with self.assertRaisesRegex(
                    UserError, "preserved invoice PDF is corrupt"
                ) as error:
                    _ = issue.with_user(reader).conventional_pdf_content
                self.assertIs(type(error.exception), UserError)
            issue.invalidate_recordset(
                [
                    "conventional_pdf_content",
                    "conventional_pdf_original_content",
                    "conventional_pdf_hash",
                ]
            )
            self.assertEqual(
                issue.with_user(reader).conventional_pdf_content.content, content
            )
        for role in (
            "group_controlled_sender",
            "group_billing_reader",
            "group_fiscal_auditor",
            "group_event_receiver",
        ):
            actor = new_test_user(
                self.env,
                login="synthetic-pdf-" + role,
                groups="l10n_ae_edi." + role,
                company_ids=company.ids,
                company_id=company.id,
            )
            actual = issue.with_user(actor)
            self.assertFalse(actual.env["account.move"].has_access("read"))
            self.assertEqual(actual.conventional_pdf_content.content, content)
            with self.assertRaises(AccessError):
                actual.read(["conventional_pdf_original_content"])
        outsider = new_test_user(
            self.env,
            login="synthetic-pdf-no-transmission-rights",
            groups="base.group_user",
            company_ids=company.ids,
            company_id=company.id,
        )
        foreign_reader = new_test_user(
            self.env,
            login="synthetic-pdf-foreign-company",
            groups="account.group_account_readonly",
            company_ids=other_company.ids,
            company_id=other_company.id,
        )
        # Warmed content must not cross actual UID/company boundaries under sudo.
        for candidate in (
            issue.with_user(outsider),
            issue.with_user(foreign_reader).with_context(
                allowed_company_ids=other_company.ids
            ),
            issue.with_context(allowed_company_ids=other_company.ids),
        ):
            with self.assertRaises(AccessError):
                _ = candidate.sudo().conventional_pdf_content

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
            "conventional_pdf_original_content",
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

    def test_existing_auditor_recovers_original_after_legal_company_archival(self):
        """Archival preserves evidence access without reopening accounting scope."""
        original_company = self.env.company
        active_company = self.env["res.company"].create(
            {
                "name": "Synthetic active audit home after archival",
                "country_id": self.env.ref("base.ae").id,
                "currency_id": self.env.ref("base.AED").id,
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        binding = self._reviewed_binding(
            {"name": "Synthetic original before company archival"},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic local contract; no actual ASP approval.",
        )
        invoice.l10n_ae_binding_id = binding
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        auditor = new_test_user(
            self.env,
            login="synthetic-existing-archived-company-auditor",
            groups=(
                "base.group_user,base.group_allow_export,"
                "l10n_ae_edi.group_fiscal_auditor"
            ),
            company_id=active_company.id,
            company_ids=[original_company.id, active_company.id],
        ).with_context(allowed_company_ids=active_company.ids)
        memberships = sorted(auditor.with_context(active_test=False).company_ids.ids)
        roles = sorted(auditor.group_ids.ids)
        fields = ["xml_download", "evidence_archive"]
        before = (
            issue.with_user(auditor)
            .with_context(allowed_company_ids=original_company.ids)
            .read(fields)[0]
        )
        xml = base64.b64decode(before["xml_download"]["content"])
        package = base64.b64decode(before["evidence_archive"]["content"])
        self.assertEqual(xml, issue.xml_content.encode())
        self.assertEqual(hashlib.sha256(xml).hexdigest(), issue.xml_hash)
        identity = (issue.payload, issue.idempotency_key, invoice.l10n_ae_fiscal_uuid)
        ledger = invoice.line_ids.read(
            ["debit", "credit", "balance", "amount_currency", "write_date"]
        )

        # Native Odoo forbids archiving any active user's default company.
        # Relocate those defaults legitimately before archival, without changing
        # this auditor's pre-existing memberships, roles or active home company.
        defaults = self.env["res.users"].search(
            [("active", "=", True), ("company_id", "=", original_company.id)]
        )
        defaults.write(
            {
                "company_ids": [Command.link(active_company.id)],
                "company_id": active_company.id,
            }
        )
        original_company.with_context(
            allowed_company_ids=active_company.ids
        ).action_archive()
        archived_company = original_company.with_context(
            allowed_company_ids=active_company.ids
        )
        self.assertFalse(archived_company.active)
        self.assertEqual(
            sorted(auditor.with_context(active_test=False).company_ids.ids),
            memberships,
        )
        self.assertEqual(sorted(auditor.group_ids.ids), roles)
        self.assertEqual(auditor.company_id, active_company)
        self.assertFalse(auditor.has_group("base.group_system"))
        self.assertFalse(auditor.has_group("account.group_account_invoice"))
        self.env.invalidate_all()
        archived = issue.with_user(auditor).with_context(
            allowed_company_ids=active_company.ids
        )
        try:
            after = archived.read(fields)[0]
        except AccessError as error:
            self.fail(
                "Previously authorized fiscal auditor cannot recover the "
                f"archived company original: {error}"
            )
        self.assertEqual(base64.b64decode(after["xml_download"]["content"]), xml)
        self.assertEqual(
            base64.b64decode(after["evidence_archive"]["content"]), package
        )
        # The accounting preservation oracle is separate from the auditor's
        # evidence-only reads; no new accounting permission is granted to them.
        current = issue.sudo().with_context(allowed_company_ids=active_company.ids)
        self.assertEqual(
            (
                current.payload,
                current.idempotency_key,
                current.move_id.l10n_ae_fiscal_uuid,
            ),
            identity,
        )
        self.assertEqual(
            current.move_id.line_ids.read(
                ["debit", "credit", "balance", "amount_currency", "write_date"]
            ),
            ledger,
        )
        _logger.info(
            "UAE_ARCHIVED_COMPANY_AUDIT=%s",
            json.dumps(
                {
                    "company_id": original_company.id,
                    "active_home_company_id": active_company.id,
                    "auditor_id": auditor.id,
                    "issue_id": issue.id,
                    "xml_sha256": hashlib.sha256(xml).hexdigest(),
                    "package_sha256": hashlib.sha256(package).hexdigest(),
                    "memberships_and_roles_preserved": True,
                    "archived_company_stays_inactive": not archived_company.active,
                    "accounting_and_original_preserved": True,
                },
                sort_keys=True,
            ),
        )

    def _archived_audit_fixture(self, suffix):
        company_a = self.env.company
        company_b = self.env["res.company"].create(
            {
                "name": "Synthetic active audit home after archival " + suffix,
                "country_id": self.env.ref("base.ae").id,
                "currency_id": self.env.ref("base.AED").id,
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        binding = self._reviewed_binding(
            {"name": "Synthetic original before company archival " + suffix},
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic local contract; no actual ASP approval.",
        )
        invoice.l10n_ae_binding_id = binding
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        auditor = new_test_user(
            self.env,
            login="synthetic-archive-negative-" + suffix,
            groups="base.group_user,base.group_allow_export,l10n_ae_edi.group_fiscal_auditor",
            company_id=company_b.id,
            company_ids=[company_a.id, company_b.id],
        )
        source = issue.with_user(auditor).with_context(
            allowed_company_ids=company_a.ids
        )
        expected = source.read(["xml_download", "evidence_archive"])[0]
        xml = base64.b64decode(expected["xml_download"]["content"])
        package = base64.b64decode(expected["evidence_archive"]["content"])
        self.assertEqual(xml, issue.xml_content.encode())
        self.assertEqual(hashlib.sha256(xml).hexdigest(), issue.xml_hash)
        original = issue.read(
            [
                "payload",
                "payload_hash",
                "xml_content",
                "xml_hash",
                "idempotency_key",
                "revision",
            ]
        )[0]
        ledger = invoice.line_ids.read(
            ["debit", "credit", "balance", "amount_currency", "write_date"]
        )
        defaults = self.env["res.users"].search(
            [("active", "=", True), ("company_id", "=", company_a.id)]
        )
        defaults.write(
            {"company_ids": [Command.link(company_b.id)], "company_id": company_b.id}
        )
        # Admin configuration mutations below use the legitimate active home B.
        admin = self.env(
            context={**self.env.context, "allowed_company_ids": company_b.ids}
        )
        company_a.with_env(admin).action_archive()
        self.assertFalse(company_a.with_env(admin).active)
        return {
            "company_a": company_a,
            "company_b": company_b,
            "invoice": invoice,
            "issue": issue,
            "binding": binding,
            "auditor": auditor,
            "admin": admin,
            "xml": xml,
            "package": package,
            "original": original,
            "ledger": ledger,
            "ledger_ids": [row["id"] for row in ledger],
        }

    def _archived_audit_as_actor(self, fixture, user=None):
        actor = user or fixture["auditor"]
        return (
            fixture["issue"]
            .with_user(actor)
            .sudo(False)
            .with_context(allowed_company_ids=fixture["company_b"].ids)
        )

    def _assert_archived_audit_downloads(self, fixture, user=None):
        issue = self._archived_audit_as_actor(fixture, user)
        self.assertFalse(issue.env.su)
        issue.check_access("read")
        result = issue.read(["xml_download", "evidence_archive"])[0]
        self.assertEqual(
            base64.b64decode(result["xml_download"]["content"]), fixture["xml"]
        )
        self.assertEqual(
            base64.b64decode(result["evidence_archive"]["content"]), fixture["package"]
        )
        self.assertEqual(
            hashlib.sha256(fixture["xml"]).hexdigest(), fixture["original"]["xml_hash"]
        )

    def _assert_archived_audit_denied(self, fixture, user=None):
        issue = self._archived_audit_as_actor(fixture, user)
        self.assertFalse(issue.env.su)
        with self.assertRaises(AccessError):
            issue.check_access("read")
        with self.assertRaises(AccessError):
            issue.read(["xml_download"])
        with self.assertRaises(AccessError):
            issue.read(["evidence_archive"])

    def _assert_archived_audit_preserved(self, fixture):
        # Separate privileged fixture oracle, never the public auditor read seam.
        # Matches the original positive test's independent accounting oracle.
        issue = fixture["issue"].with_env(fixture["admin"]).sudo()
        self.assertEqual(issue.read(list(fixture["original"]))[0], fixture["original"])
        self.assertEqual(
            issue.move_id.line_ids.read(
                ["debit", "credit", "balance", "amount_currency", "write_date"]
            ),
            fixture["ledger"],
        )
        self.assertFalse(fixture["company_a"].with_env(fixture["admin"]).active)

    def test_archived_auditor_warm_reads_recheck_role_membership_active_and_export(
        self,
    ):
        """Each revocation is a separate denial after an actually warmed success."""
        fixture = self._archived_audit_fixture("warm-revocations")
        auditor = fixture["auditor"].with_env(fixture["admin"])
        role = self.env.ref("l10n_ae_edi.group_fiscal_auditor")
        export = self.env.ref("base.group_allow_export")
        self._assert_archived_audit_downloads(fixture)
        auditor.write({"group_ids": [Command.unlink(role.id)]})
        self._assert_archived_audit_denied(fixture)
        auditor.write({"group_ids": [Command.link(role.id)]})
        self._assert_archived_audit_downloads(fixture)

        auditor.write({"company_ids": [Command.unlink(fixture["company_a"].id)]})
        self._assert_archived_audit_denied(fixture)
        auditor.write({"company_ids": [Command.link(fixture["company_a"].id)]})
        self._assert_archived_audit_downloads(fixture)

        auditor.write({"active": False})
        self._assert_archived_audit_denied(fixture)
        auditor.write({"active": True})
        self._assert_archived_audit_downloads(fixture)

        auditor.write({"group_ids": [Command.unlink(export.id)]})
        actual = self._archived_audit_as_actor(fixture)
        actual.check_access("read")
        self.assertEqual(
            base64.b64decode(
                actual.read(["xml_download"])[0]["xml_download"]["content"]
            ),
            fixture["xml"],
        )
        with self.assertRaises(AccessError):
            actual.read(["evidence_archive"])
        auditor.write({"group_ids": [Command.link(export.id)]})
        self._assert_archived_audit_downloads(fixture)
        # No explicit invalidate/flush/cache clear or new login between changes.
        self._assert_archived_audit_preserved(fixture)
        _logger.info(
            "UAE_ARCHIVED_AUDIT_SECURITY=%s",
            json.dumps(
                {
                    "case": "warm_revocations",
                    "actual_uid_reads": True,
                    "denied": ["role", "membership", "inactive_actor", "no_export"],
                    "xml_without_export_preserved": True,
                    "original_and_ledger_preserved": True,
                }
            ),
        )

    def test_archived_audit_exception_does_not_extend_to_active_companies_or_accounting(
        self,
    ):
        fixture = self._archived_audit_fixture("scope-boundaries")
        self._assert_archived_audit_downloads(fixture)
        target = fixture["company_a"].with_env(fixture["admin"])
        target.action_unarchive()
        # A is allowed by membership but active and deselected: archive-only
        # access must disappear even after a warmed successful package read.
        self._assert_archived_audit_denied(fixture)
        target.action_archive()
        self._assert_archived_audit_downloads(fixture)
        home = fixture["company_b"]
        groups = {
            "manager-only": (
                "base.group_user,account.group_account_manager,base.group_allow_export"
            ),
            "system-only": "base.group_user,base.group_system,base.group_allow_export",
            "foreign-auditor": (
                "base.group_user,l10n_ae_edi.group_fiscal_auditor,"
                "base.group_allow_export"
            ),
            "manager-auditor": (
                "base.group_user,account.group_account_manager,"
                "l10n_ae_edi.group_fiscal_auditor,base.group_allow_export"
            ),
        }
        users = {}
        for name, roles in groups.items():
            memberships = (
                home.ids if name == "foreign-auditor" else [target.id, home.id]
            )
            users[name] = new_test_user(
                fixture["admin"],
                login="synthetic-archive-boundary-" + name,
                groups=roles,
                company_id=home.id,
                company_ids=memberships,
            )
        for name in ("manager-only", "system-only", "foreign-auditor"):
            self._assert_archived_audit_denied(fixture, users[name])
        manager_auditor = users["manager-auditor"]
        self._assert_archived_audit_downloads(fixture, manager_auditor)
        issue = self._archived_audit_as_actor(fixture, manager_auditor)
        for operation in ("create", "write", "unlink"):
            with self.assertRaises(AccessError):
                issue.check_access(operation)
        line_records = fixture["admin"]["account.move.line"].browse(
            fixture["ledger_ids"]
        )
        for record in (fixture["invoice"], fixture["binding"], *line_records):
            scoped = (
                record.with_user(manager_auditor)
                .sudo(False)
                .with_context(allowed_company_ids=home.ids)
            )
            self.assertFalse(scoped.env.su)
            with self.assertRaises(AccessError):
                scoped.check_access("read")
        # Native environments still reject selecting an inactive company,
        # even for an assigned auditor eligible for historical evidence reads.
        forged = self._archived_audit_as_actor(fixture).with_context(
            allowed_company_ids=target.ids, active_test=False
        )
        with self.assertRaises(AccessError):
            forged.read(["xml_download", "evidence_archive"])
        self._assert_archived_audit_preserved(fixture)
        _logger.info(
            "UAE_ARCHIVED_AUDIT_SECURITY=%s",
            json.dumps(
                {
                    "case": "scope_boundaries",
                    "actual_uid_reads": True,
                    "denied": [
                        "active_deselected",
                        "manager_only",
                        "system_without_auditor",
                        "nonmember",
                        "cud",
                        "accounting",
                        "binding",
                        "inactive_company_context",
                    ],
                    "original_and_ledger_preserved": True,
                }
            ),
        )
