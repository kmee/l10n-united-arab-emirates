# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaeInvoiceIdentity(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()

    def test_missing_taxpayer_identity_blocks_invoice_validation(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        with self.assertRaisesRegex(UserError, "Taxpayer Identification Number"):
            invoice.action_l10n_ae_validate()

    def test_invalid_taxpayer_identity_blocks_invoice_validation(self):
        self.env.company.l10n_ae_tin = "12345"
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        with self.assertRaisesRegex(UserError, "10 ASCII digits"):
            invoice.action_l10n_ae_validate()

    def test_unconfirmed_taxpayer_identity_blocks_invoice_validation(self):
        self.env.company.l10n_ae_tin = "1000000001"
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        with self.assertRaisesRegex(UserError, "confirmed during onboarding"):
            invoice.action_l10n_ae_validate()

    def test_changing_taxpayer_identity_requires_new_confirmation(self):
        self.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        self.env.company.l10n_ae_tin = "1000000002"
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        with self.assertRaisesRegex(UserError, "confirmed during onboarding"):
            invoice.action_l10n_ae_validate()

    def test_confirmed_identity_allows_local_validation(self):
        # The optional PINT extension checks electronic addresses at this same
        # public action. Keep a complete positive fixture without depending on it.
        for partner, endpoint in (
            (self.env.company.partner_id, "1000000001"),
            (self.partner_a, "1000000002"),
        ):
            values = {
                "country_id": self.env.ref("base.ae").id,
                "state_id": self.env.ref("base.state_ae_du").id,
            }
            if {"routing_scheme", "routing_endpoint"} <= set(partner._fields):
                values.update(routing_scheme="0235", routing_endpoint=endpoint)
            partner.write(values)
        self.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        self.assertTrue(invoice.action_l10n_ae_validate())

    def test_ordinary_purchase_cannot_be_prepared_for_outbound_emission(self):
        self.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        bill = self.init_invoice("in_invoice", amounts=[100], taxes=self.tax_purchase_a)
        with self.assertRaisesRegex(UserError, "self-billing purchase"):
            bill.action_l10n_ae_validate()

    def test_self_billing_journal_without_agreement_cannot_authorize_emission(self):
        self.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        bill = self.init_invoice("in_invoice", amounts=[100], taxes=self.tax_purchase_a)
        bill.journal_id.is_self_billing = True
        with self.assertRaisesRegex(UserError, "self-billing agreement"):
            bill.action_l10n_ae_validate()

    def test_company_uae_identity_form_follows_fiscal_country_not_address(self):
        ae = self.env.ref("base.ae")
        fr = self.env.ref("base.fr")
        company_ae_fiscal = self.env.company
        company_ae_fiscal.write(
            {
                "country_id": self.env.ref("base.us").id,
                "account_fiscal_country_id": ae.id,
                "state_id": self.env.ref("base.state_us_5").id,
                "city": "San Francisco",
                "zip": "94105",
            }
        )
        company_fr_fiscal = self.setup_other_company(
            name="Synthetic AE address with French fiscal country"
        )["company"]
        company_fr_fiscal.write(
            {
                "country_id": ae.id,
                "account_fiscal_country_id": fr.id,
                "state_id": self.env.ref("base.state_ae_du").id,
                "city": "Dubai",
            }
        )
        manager = new_test_user(
            self.env,
            login="uae-company-identity-form-manager",
            groups="base.group_erp_manager,account.group_account_manager",
            company_id=company_ae_fiscal.id,
            company_ids=[Command.set((company_ae_fiscal | company_fr_fiscal).ids)],
        )
        # Company settings are managed by an ERP/configuration manager. Odoo treats
        # this role as administrative; this is not an ordinary accountant UI proof.
        self.assertTrue(manager.has_group("base.group_erp_manager"))
        ae_company = company_ae_fiscal.with_user(manager).with_context(
            allowed_company_ids=[company_ae_fiscal.id]
        )
        self.assertFalse(ae_company.env.su)
        ae_form = Form(ae_company)
        ae_form.l10n_ae_tin = "1000000042"
        ae_form.l10n_ae_tin_confirmed = True
        if "l10n_ae_inbox_reviewer_id" in ae_company:
            ae_form.l10n_ae_inbox_reviewer_id = manager
        ae_form.save()
        self.env.flush_all()
        ae_company.invalidate_recordset()
        stored = ae_company.read(["l10n_ae_tin", "l10n_ae_tin_confirmed", "write_uid"])[
            0
        ]
        self.assertEqual(stored["l10n_ae_tin"], "1000000042")
        self.assertTrue(stored["l10n_ae_tin_confirmed"])
        self.assertEqual(stored["write_uid"][0], manager.id)
        if "l10n_ae_inbox_reviewer_id" in ae_company:
            self.assertEqual(ae_company.l10n_ae_inbox_reviewer_id, manager)
        self.assertEqual(ae_company.country_code, "US")
        self.assertEqual(ae_company.account_fiscal_country_id.code, "AE")

        fr_company = company_fr_fiscal.with_user(manager).with_context(
            allowed_company_ids=[company_fr_fiscal.id]
        )
        self.assertFalse(fr_company.env.su)
        fields_to_read = [
            "country_id",
            "account_fiscal_country_id",
            "l10n_ae_tin",
            "l10n_ae_tin_confirmed",
            "write_uid",
            "write_date",
        ]
        self.env.flush_all()
        fr_company.invalidate_recordset()
        before = fr_company.read(fields_to_read)
        fr_form = Form(fr_company)
        with self.assertRaisesRegex(
            AssertionError, "can't write on invisible field 'l10n_ae_tin'"
        ):
            fr_form.l10n_ae_tin = "1000000099"
        if "l10n_ae_inbox_reviewer_id" in fr_company:
            with self.assertRaisesRegex(
                AssertionError,
                "can't write on invisible field 'l10n_ae_inbox_reviewer_id'",
            ):
                fr_form.l10n_ae_inbox_reviewer_id = manager
        self.env.flush_all()
        fr_company.invalidate_recordset()
        self.assertEqual(fr_company.read(fields_to_read), before)
        self.assertEqual(fr_company.country_code, "AE")
        self.assertEqual(fr_company.account_fiscal_country_id.code, "FR")
