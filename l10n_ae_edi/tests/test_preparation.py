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
