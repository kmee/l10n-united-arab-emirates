# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaeInvoicePreparation(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        cls.partner_a.country_id = cls.env.ref("base.ae")
        if "routing_scheme" in cls.env["res.partner"]._fields:
            cls.env.company.partner_id.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000001"}
            )
            cls.partner_a.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000002"}
            )
        cls.binding = cls.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic sandbox ASP"}
        )

    def test_posting_with_automatic_queue_creates_one_frozen_intent(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.l10n_ae_binding_id = self.binding
        self.env.company.l10n_ae_edi_auto_prepare = True
        invoice.action_post()
        transmission = invoice.l10n_ae_transmission_id
        self.assertEqual(invoice.state, "posted")
        self.assertTrue(transmission)
        self.assertEqual(transmission.submission_state, "queued")
        self.assertEqual(
            json.loads(transmission.payload)["totals"]["payable"], "105.00"
        )
        self.assertFalse(transmission.attempt_ids)

    def test_posting_uses_the_company_provider_when_invoice_has_no_override(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": self.binding.id,
            }
        )
        invoice.action_post()
        self.assertEqual(invoice.l10n_ae_binding_id, self.binding)
        self.assertEqual(invoice.l10n_ae_transmission_id.binding_id, self.binding)

    def test_company_cannot_select_another_legal_entity_provider(self):
        other = self.env["res.company"].create({"name": "Another synthetic UAE entity"})
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Another entity's provider",
                "company_id": other.id,
            }
        )
        with self.assertRaises(UserError), self.cr.savepoint():
            self.env.company.l10n_ae_edi_binding_id = binding

    def test_disabled_automatic_queue_preserves_standard_posting(self):
        self.env.company.l10n_ae_edi_auto_prepare = False
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        self.assertFalse(invoice.l10n_ae_transmission_id)

    def test_ordinary_purchase_is_not_queued_by_company_policy(self):
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": self.binding.id,
            }
        )
        bill = self.init_invoice("in_invoice", amounts=[100], taxes=self.tax_purchase_a)
        self.assertFalse(bill.is_self_billing)
        bill.action_post()
        self.assertEqual(bill.state, "posted")
        self.assertFalse(bill.l10n_ae_transmission_id)

    def test_missing_provider_rolls_back_posting_and_intent(self):
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": False,
            }
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        with (
            self.assertRaisesRegex(UserError, "Select an active ASP"),
            self.cr.savepoint(),
        ):
            invoice.action_post()
        self.assertEqual(invoice.state, "draft")
        self.assertFalse(invoice.l10n_ae_transmission_id)

    def test_explicit_invoice_provider_takes_precedence_over_company_default(self):
        override = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic override"}
        )
        self.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": True,
                "l10n_ae_edi_binding_id": self.binding.id,
            }
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.l10n_ae_binding_id = override
        invoice.action_post()
        self.assertEqual(invoice.l10n_ae_transmission_id.binding_id, override)

    def test_preparation_freezes_customer_address_and_reuses_original_payload(self):
        self.partner_a.street = "Original synthetic address"
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        transmission = invoice.l10n_ae_transmission_id
        payload = transmission.payload
        self.assertEqual(
            json.loads(payload)["counterparty"]["street"], "Original synthetic address"
        )
        self.assertEqual(json.loads(payload)["totals"]["payable"], "105.00")
        self.partner_a.street = "Changed synthetic address"
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.l10n_ae_transmission_id, transmission)
        self.assertEqual(transmission.payload, payload)

    def test_batch_created_manual_invoices_receive_distinct_business_identities(self):
        template = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        )
        invoices = self.env["account.move"].create(
            [template.copy_data()[0], template.copy_data()[0]]
        )
        self.assertNotEqual(
            invoices[0].l10n_ae_origin_document_id,
            invoices[1].l10n_ae_origin_document_id,
        )

    def test_frozen_invoice_payload_cannot_be_edited_by_an_administrator(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        transmission = invoice.l10n_ae_transmission_id.sudo()
        with self.assertRaisesRegex(UserError, "immutable"):
            transmission.write({"payload": '{"totals":{"payable":"1.00"}}'})

    def test_frozen_invoice_reference_cannot_be_cleared_to_prepare_again(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        with self.assertRaisesRegex(UserError, "frozen UAE"):
            invoice.write({"l10n_ae_transmission_id": False})

    def test_frozen_evidence_cannot_be_deleted_by_an_administrator(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        with self.assertRaisesRegex(UserError, "immutable"):
            invoice.l10n_ae_transmission_id.sudo().unlink()
