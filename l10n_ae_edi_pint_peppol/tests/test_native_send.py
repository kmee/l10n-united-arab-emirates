# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaePeppolSend(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.write(
            {
                "l10n_ae_edi_auto_prepare": False,
                "account_peppol_proxy_state": "not_registered",
            }
        )
        cls.partner_a.write(
            {
                "invoice_sending_method": "manual",
                "invoice_edi_format": "kmee_ae_pint",
                "routing_scheme": "0235",
                "routing_endpoint": "1000000002",
                "peppol_verification_state": False,
            }
        )

    def test_manager_opens_native_send_without_writing_unchecked_contact(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        self.assertFalse(invoice.env.user.has_group("base.group_partner_manager"))
        partner = invoice.commercial_partner_id.with_company(invoice.company_id)
        with self.assertRaises(AccessError):
            partner.check_access("write")
        self.assertFalse(partner.peppol_verification_state)
        invoice.action_post()
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        original_contact = partner.read(["peppol_verification_state", "write_date"])
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "amount_currency",
            "currency_id",
            "tax_ids",
            "create_date",
            "write_date",
        ]
        original_ledger = invoice.line_ids.read(ledger_fields)
        action = invoice.action_send_and_print()
        self.assertEqual(action["res_model"], "account.move.send.wizard")
        wizard = (
            invoice.env[action["res_model"]]
            .with_context(**action["context"])
            .create({})
        )
        self.assertEqual(wizard.move_id, invoice)
        self.assertTrue(
            any(key.startswith("l10n_ae_edi_") for key in (wizard.alerts or {}))
        )
        self.assertEqual(
            partner.read(["peppol_verification_state", "write_date"]), original_contact
        )
        self.assertEqual(invoice.line_ids.read(ledger_fields), original_ledger)
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        self.assertFalse(invoice.ubl_cii_xml_id)
        self.assertFalse(invoice.invoice_pdf_report_id)
        self.assertFalse(invoice.sending_data)

    def test_non_uae_send_retains_native_partner_verification(self):
        # Belgium/sender is a native eligible company. An unsupported format
        # gives a deterministic native not_verified result without network I/O.
        # The control is endpoint verification delegation, not Peppol delivery.
        self.env.company.write(
            {
                "country_id": self.env.ref("base.be").id,
                "account_peppol_proxy_state": "sender",
            }
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.action_post()
        partner = invoice.commercial_partner_id.with_company(invoice.company_id)
        self.assertFalse(partner.peppol_verification_state)
        with self.cr.savepoint(), self.assertRaises(AccessError):
            invoice.with_user(self.simple_accountman).action_send_and_print()
        self.assertFalse(partner.peppol_verification_state)
        action = invoice.action_send_and_print()
        self.assertEqual(action["res_model"], "account.move.send.wizard")
        self.assertEqual(partner.peppol_verification_state, "not_verified")
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.ubl_cii_xml_id)
