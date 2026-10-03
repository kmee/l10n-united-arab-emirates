# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaeInvoiceScenarios(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )

    def test_free_zone_without_beneficiary_blocks_validation(self):
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.l10n_ae_free_zone = True
        with self.assertRaisesRegex(UserError, "IBR-007-AE"):
            invoice.action_l10n_ae_validate()
