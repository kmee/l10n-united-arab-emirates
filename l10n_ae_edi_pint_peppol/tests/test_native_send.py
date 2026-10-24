# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from lxml import etree

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
                "account_fiscal_country_id": self.env.ref("base.be").id,
                "account_peppol_proxy_state": "sender",
            }
        )
        group = self.env["account.tax.group"].create(
            {
                "name": "Synthetic Belgian verification control",
                "company_id": self.env.company.id,
                "country_id": self.env.ref("base.be").id,
            }
        )
        tax = self.env["account.tax"].create(
            {
                "name": "Synthetic verification control 5%",
                "amount": 5,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "company_id": self.env.company.id,
                "country_id": self.env.ref("base.be").id,
                "tax_group_id": group.id,
            }
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=tax)
        invoice.action_post()
        partner = invoice.commercial_partner_id.with_company(invoice.company_id)
        self.assertFalse(partner.peppol_verification_state)
        action = invoice.action_send_and_print()
        self.assertEqual(action["res_model"], "account.move.send.wizard")
        self.assertEqual(partner.peppol_verification_state, "not_verified")
        self.assertFalse(invoice.l10n_ae_transmission_ids)
        self.assertFalse(invoice.ubl_cii_xml_id)

    def test_uae_contact_verify_explains_unavailable_native_service(self):
        partner = self.partner_a.with_user(self.simple_accountman)
        self.assertFalse(partner.env.su)
        before = partner.read(["peppol_verification_state", "write_date"])
        self.assertFalse(partner.button_account_peppol_check_partner_endpoint())
        # Exercise the same public action selected by the native contact form.
        # The existing button returns False silently; no new method is assumed.
        form = partner.get_view(view_type="form")
        buttons = etree.fromstring(form["arch"]).xpath(
            "//button[@type='object'][@help=\"Verify partner's Peppol endpoint\"]"
        )
        self.assertEqual(len(buttons), 1)
        action = getattr(partner, buttons[0].get("name"))()
        self.assertEqual(
            partner.read(["peppol_verification_state", "write_date"]), before
        )
        self.assertIsInstance(action, dict)
        self.assertEqual(action["type"], "ir.actions.client")
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["type"], "info")
        self.assertIn("appointed service provider", action["params"]["message"])
        self.assertEqual(
            partner.peppol_verification_state,
            False,
            "An unavailable verification must not mark the endpoint verified.",
        )

    def test_contact_verify_preserves_non_uae_native_result(self):
        self.env.company.write(
            {
                "country_id": self.env.ref("base.be").id,
                "account_fiscal_country_id": self.env.ref("base.be").id,
                "account_peppol_proxy_state": "sender",
            }
        )
        partner = self.partner_a.with_company(self.env.company)
        self.assertFalse(partner.peppol_verification_state)
        form = partner.get_view(view_type="form")
        buttons = etree.fromstring(form["arch"]).xpath(
            "//button[@type='object'][@help=\"Verify partner's Peppol endpoint\"]"
        )
        self.assertEqual(len(buttons), 1)
        self.assertFalse(getattr(partner, buttons[0].get("name"))())
        self.assertEqual(partner.peppol_verification_state, "not_verified")

    def test_contact_verify_refuses_a_company_outside_actor_scope(self):
        outside = self.env["res.company"].create(
            {
                "name": "Endpoint verification scope control",
                "currency_id": self.env.company.currency_id.id,
            }
        )
        partner = self.env["res.partner"].create(
            {"name": "Endpoint verification without a routing identifier"}
        )
        actor_partner = partner.with_user(self.simple_accountman)
        self.assertFalse(actor_partner.env.su)
        self.assertNotIn(outside.id, actor_partner.env.user.company_ids.ids)
        self.assertFalse(partner.routing_identifier)
        before = partner.with_company(outside).read(
            ["peppol_verification_state", "write_date"]
        )
        form = actor_partner.get_view(view_type="form")
        buttons = etree.fromstring(form["arch"]).xpath(
            "//button[@type='object'][@help=\"Verify partner's Peppol endpoint\"]"
        )
        self.assertEqual(len(buttons), 1)
        with self.assertRaises(AccessError):
            actor_partner.button_account_peppol_check_partner_endpoint(company=outside)
        with self.assertRaises(AccessError):
            getattr(actor_partner, buttons[0].get("name"))(company=outside)
        self.assertEqual(
            partner.with_company(outside).read(
                ["peppol_verification_state", "write_date"]
            ),
            before,
        )

    def test_uae_fiscal_contact_uses_native_eligible_foreign_address(
        self,
    ):
        company = self.env.company
        company.write(
            {
                "country_id": self.env.ref("base.be").id,
                "account_fiscal_country_id": self.env.ref("base.ae").id,
                "account_peppol_proxy_state": "sender",
            }
        )
        # country_code is the native physical country; the fiscal country is
        # independently UAE. Both are public native company fields.
        self.assertEqual(company.country_id.code, "BE")
        self.assertEqual(company.country_code, "BE")
        self.assertEqual(company.account_fiscal_country_id.code, "AE")
        self.assertEqual(company.account_peppol_proxy_state, "sender")

        partner = self.partner_a.with_company(company)
        # This UAE-specific format is unsupported by native Peppol, so Odoo's
        # real public endpoint action returns not_verified without HTTP lookup.
        self.assertEqual(partner.invoice_edi_format, "kmee_ae_pint")
        self.assertFalse(partner.peppol_verification_state)
        form = partner.get_view(view_type="form")
        buttons = etree.fromstring(form["arch"]).xpath(
            "//button[@type='object'][@help=\"Verify partner's Peppol endpoint\"]"
        )
        self.assertEqual(len(buttons), 1)
        action = getattr(partner, buttons[0].get("name"))()
        self.assertFalse(action)
        self.assertEqual(partner.peppol_verification_state, "not_verified")
