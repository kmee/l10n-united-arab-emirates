# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo.exceptions import UserError
from odoo.tests import tagged

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
