# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from lxml import etree

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaeSelfBillingInbound(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.buyer_data = cls.setup_other_company(name="Synthetic self-billing buyer")
        cls.buyer = cls.buyer_data["company"]
        for company, tin, license_number in (
            (cls.env.company, "1000000001", "SYNTH-SUPPLIER-TL-1"),
            (cls.buyer, "1000000006", "SYNTH-BUYER-TL-6"),
        ):
            company.write({"l10n_ae_tin": tin, "l10n_ae_tin_confirmed": True})
            company.partner_id.write(
                {
                    "vat": tin + "00003",
                    "street": "Synthetic UAE street",
                    "city": "Dubai",
                    "state_id": cls.env.ref("base.state_ae_du").id,
                    "routing_scheme": "0235",
                    "routing_endpoint": tin,
                    "l10n_ae_registration_type": "TL",
                    "l10n_ae_registration_authority": "Synthetic Authority",
                    "additional_identifiers": {"AE_TL": license_number},
                }
            )

    def _selfbilling_fixture(self):
        supplier = self.env.company
        journal = self.buyer_data["default_journal_purchase"]
        journal.is_self_billing = True
        source_agreement = self.env["l10n_ae.selfbilling.agreement"].create(
            {
                "name": "SYNTH-AGREEMENT-6-1",
                "company_id": self.buyer.id,
                "partner_id": supplier.partner_id.id,
                "start_date": "2026-01-01",
                "scope": "Synthetic services and adjustments",
                "onboarding_reference": "SYNTH-SUPPLIER-ONBOARD-1",
            }
        )
        recipient_agreement = self.env["l10n_ae.selfbilling.agreement"].create(
            {
                "name": "SYNTH-AGREEMENT-6-1",
                "company_id": supplier.id,
                "partner_id": self.buyer.partner_id.id,
                "local_role": "supplier",
                "start_date": "2026-01-01",
                "scope": "Synthetic services and adjustments",
                "onboarding_reference": "SYNTH-BUYER-ONBOARD-6",
            }
        )
        source = (
            self.env["account.move"]
            .with_company(self.buyer)
            .create(
                {
                    "move_type": "in_invoice",
                    "journal_id": journal.id,
                    "partner_id": supplier.partner_id.id,
                    "invoice_date": "2026-10-02",
                    "l10n_ae_selfbilling_agreement_id": source_agreement.id,
                    "l10n_ae_fiscal_uuid": "eeeeeeea-1111-4111-8111-111111111111",
                    "invoice_line_ids": [
                        (
                            0,
                            0,
                            {
                                "name": "Synthetic services",
                                "price_unit": 100,
                                "account_id": self.buyer_data[
                                    "default_account_expense"
                                ].id,
                                "tax_ids": [
                                    (6, 0, self.buyer_data["default_tax_purchase"].ids)
                                ],
                            },
                        )
                    ],
                }
            )
        )
        source.action_post()
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Supplier receiving binding",
                "company_id": supplier.id,
            }
        )
        return source, recipient_agreement, binding

    def test_buyer_issued_invoice_is_received_as_a_supplier_sale_without_emission(self):
        supplier = self.env.company
        source, recipient_agreement, binding = self._selfbilling_fixture()
        content = source.preview_l10n_ae_pint().encode()
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id,
            "buyer-issued-invoice",
            content,
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        sale = inbox.move_id
        self.assertEqual(sale.company_id, supplier)
        self.assertEqual(sale.partner_id, self.buyer.partner_id)
        self.assertEqual(sale.move_type, "out_invoice")
        self.assertEqual(sale.state, "draft")
        self.assertEqual(
            (sale.amount_untaxed, sale.amount_tax, sale.amount_total), (100, 5, 105)
        )
        self.assertEqual(sale.l10n_ae_selfbilling_agreement_id, recipient_agreement)
        self.assertTrue(sale.l10n_ae_received_document)
        self.assertEqual(sale.l10n_ae_received_process, "selfbilling")
        self.assertEqual(inbox.original_xml.content, content)
        supplier.write(
            {"l10n_ae_edi_auto_prepare": True, "l10n_ae_edi_binding_id": binding.id}
        )
        sale.action_post()
        self.assertFalse(sale.l10n_ae_transmission_id)

    def test_received_self_billed_credit_links_the_original_supplier_sale(self):
        source, agreement, binding = self._selfbilling_fixture()
        inbox_model = self.env["l10n_ae.edi.inbox"]
        original = inbox_model.receive_controlled_document(
            binding.id,
            "buyer-original",
            source.preview_l10n_ae_pint().encode(),
        )
        self.assertTrue(original.action_process(), original.diagnostic)
        buyer_agreement = source.l10n_ae_selfbilling_agreement_id
        credit = source.copy(
            {
                "move_type": "in_refund",
                "invoice_date": source.invoice_date,
                "reversed_entry_id": source.id,
                "l10n_ae_selfbilling_agreement_id": buyer_agreement.id,
                "l10n_ae_credit_reason": "DL8.61.1.C",
                "l10n_ae_fiscal_uuid": "eeeeeeea-2222-4222-8222-222222222222",
            }
        )
        credit.invoice_line_ids.write({"price_unit": 40})
        credit.action_post()
        content = credit.preview_l10n_ae_pint().encode()
        inbox = inbox_model.receive_controlled_document(
            binding.id, "buyer-credit", content
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        refund = inbox.move_id
        self.assertEqual(refund.company_id, self.env.company)
        self.assertEqual(refund.partner_id, self.buyer.partner_id)
        self.assertEqual(refund.move_type, "out_refund")
        self.assertEqual(refund.state, "draft")
        self.assertEqual(
            (refund.amount_untaxed, refund.amount_tax, refund.amount_total), (40, 2, 42)
        )
        self.assertEqual(refund.reversed_entry_id, original.move_id)
        self.assertEqual(refund.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertEqual(refund.l10n_ae_received_process, "selfbilling")
        self.assertEqual(refund.l10n_ae_selfbilling_agreement_id, agreement)
        self.assertEqual(inbox.original_xml.content, content)
        self.assertEqual(original.move_id.amount_total, 105)
        self.assertEqual(original.move_id.state, "draft")
        self.env.company.write(
            {"l10n_ae_edi_auto_prepare": True, "l10n_ae_edi_binding_id": binding.id}
        )
        refund.action_post()
        self.assertFalse(refund.l10n_ae_transmission_id)
        self.assertEqual(original.move_id.state, "draft")

    def test_received_self_billing_links_the_existing_billing_sale_without_rewriting_it(
        self,
    ):
        source, agreement, binding = self._selfbilling_fixture()
        existing = self.init_invoice(
            "out_invoice",
            partner=self.buyer.partner_id,
            invoice_date=source.invoice_date,
            amounts=[100],
            taxes=self.tax_sale_a,
        )
        existing.write(
            {
                "ref": source.name,
                "l10n_ae_origin_system": "internal-billing",
                "l10n_ae_origin_document_id": "SYNTH-AFFILIATE-001",
            }
        )
        existing.invoice_line_ids.name = "Internal affiliate billing memo"
        content = source.preview_l10n_ae_pint().encode()
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id,
            "existing-billing-sale",
            content,
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(inbox.move_id, existing)
        self.assertEqual(
            existing.invoice_line_ids.name, "Internal affiliate billing memo"
        )
        self.assertEqual(existing.l10n_ae_origin_system, "internal-billing")
        self.assertEqual(existing.l10n_ae_origin_document_id, "SYNTH-AFFILIATE-001")
        self.assertEqual(existing.l10n_ae_selfbilling_agreement_id, agreement)
        self.assertTrue(existing.l10n_ae_received_document)
        self.assertEqual(existing.l10n_ae_fiscal_uuid, source.l10n_ae_fiscal_uuid)
        self.assertEqual(existing.amount_total, 105)
        self.assertEqual(inbox.original_xml.content, content)

    def test_xml_comments_do_not_prevent_linking_the_existing_billing_sale(self):
        source, _agreement, binding = self._selfbilling_fixture()
        existing = self.init_invoice(
            "out_invoice",
            partner=self.buyer.partner_id,
            invoice_date=source.invoice_date,
            amounts=[100],
            taxes=self.tax_sale_a,
        )
        existing.ref = source.name
        tree = etree.fromstring(source.preview_l10n_ae_pint().encode())
        tree.find("./{*}InvoiceLine/{*}Price").append(
            etree.Comment("Synthetic annotation")
        )
        content = etree.tostring(tree)
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id,
            "annotated-billing-sale",
            content,
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(inbox.move_id, existing)
        self.assertEqual(inbox.original_xml.content, content)

    def test_received_agreement_evidence_survives_later_contract_changes(self):
        source, agreement, binding = self._selfbilling_fixture()
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id,
            "agreement-evidence",
            source.preview_l10n_ae_pint().encode(),
        )
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        sale = inbox.move_id
        agreement.write(
            {"name": "REVISED-REFERENCE", "scope": "Revised scope", "active": False}
        )
        self.assertEqual(
            sale.l10n_ae_received_agreement_snapshot,
            {
                "reference": "SYNTH-AGREEMENT-6-1",
                "local_role": "supplier",
                "start_date": "2026-01-01",
                "end_date": False,
                "scope": "Synthetic services and adjustments",
                "onboarding_reference": "SYNTH-BUYER-ONBOARD-6",
            },
        )
        self.assertEqual(
            sale.l10n_ae_received_agreement_reference, "SYNTH-AGREEMENT-6-1"
        )
        with self.assertRaises(UserError):
            sale.write(
                {"l10n_ae_received_agreement_snapshot": {"reference": "REWRITTEN"}}
            )

    def test_equal_totals_do_not_allow_replacing_different_billing_quantities(self):
        source, _agreement, binding = self._selfbilling_fixture()
        existing = self.init_invoice(
            "out_invoice",
            partner=self.buyer.partner_id,
            invoice_date=source.invoice_date,
            amounts=[50],
            taxes=self.tax_sale_a,
        )
        existing.invoice_line_ids.quantity = 2
        existing.ref = source.name
        self.assertEqual(existing.amount_total, 105)
        content = source.preview_l10n_ae_pint().encode()
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id,
            "different-billing-quantities",
            content,
        )
        before = self.env["account.move"].search_count(
            [("move_type", "=", "out_invoice")]
        )
        self.assertFalse(inbox.action_process())
        self.assertEqual(inbox.state, "review")
        self.assertFalse(inbox.move_id)
        self.assertIn("fiscal lines", inbox.diagnostic)
        self.assertEqual(
            self.env["account.move"].search_count([("move_type", "=", "out_invoice")]),
            before,
        )
        self.assertEqual(
            (existing.invoice_line_ids.quantity, existing.invoice_line_ids.price_unit),
            (2, 50),
        )
        self.assertFalse(existing.l10n_ae_received_document)
        self.assertEqual(inbox.original_xml.content, content)

    def test_ineligible_or_ambiguous_receiving_agreement_stays_in_review(self):
        source, agreement, binding = self._selfbilling_fixture()
        content = source.preview_l10n_ae_pint().encode()
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            binding.id,
            "review-agreement",
            content,
        )
        before = self.env["account.move"].search_count(
            [("move_type", "=", "out_invoice")]
        )
        for values in (
            {"active": False},
            {"active": True, "end_date": "2026-09-30"},
            {"end_date": False, "local_role": "buyer"},
        ):
            agreement.write(values)
            self.assertFalse(inbox.action_process())
            self.assertEqual(inbox.state, "review")
            self.assertFalse(inbox.move_id)
            self.assertIn("agreement", inbox.diagnostic)
            self.assertEqual(
                self.env["account.move"].search_count(
                    [("move_type", "=", "out_invoice")]
                ),
                before,
            )
        agreement.local_role = "supplier"
        duplicate = agreement.copy({"name": "DUPLICATE-REQUIRES-REVIEW"})
        self.assertFalse(inbox.action_process())
        self.assertFalse(inbox.move_id)
        duplicate.active = False
        self.assertTrue(inbox.action_process(), inbox.diagnostic)
        self.assertEqual(inbox.move_id.l10n_ae_selfbilling_agreement_id, agreement)
        self.assertEqual(inbox.original_xml.content, content)
