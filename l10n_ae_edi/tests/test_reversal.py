# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaeCreditNoteWizard(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()

    def test_native_credit_wizard_preserves_the_official_uae_reason(self):
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        wizard = (
            self.env["account.move.reversal"]
            .with_context(active_model="account.move", active_ids=original.ids)
            .create(
                {
                    "reason": "Synthetic reduction in consideration",
                    "l10n_ae_credit_reason": "DL8.61.1.C",
                    "journal_id": original.journal_id.id,
                }
            )
        )
        action = wizard.refund_moves()
        credit = wizard.new_move_ids
        self.assertEqual(action["res_id"], credit.id)
        self.assertEqual((credit.state, credit.move_type), ("draft", "out_refund"))
        self.assertEqual(credit.l10n_ae_credit_reason, "DL8.61.1.C")
        self.assertEqual(credit.reversed_entry_id, original)
        self.assertEqual((credit.amount_tax, credit.amount_total), (5, 105))
        self.assertEqual((original.state, original.amount_total), ("posted", 105))
        self.assertFalse(original.l10n_ae_credit_reason)

    def test_credit_draft_without_an_official_reason_cannot_pass_emission_checks(self):
        self.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        wizard = (
            self.env["account.move.reversal"]
            .with_context(active_model="account.move", active_ids=original.ids)
            .create(
                {
                    "reason": "Free text is not the official fiscal reason",
                    "journal_id": original.journal_id.id,
                }
            )
        )
        wizard.refund_moves()
        credit = wizard.new_move_ids
        self.assertEqual(credit.state, "draft")
        self.assertFalse(credit.l10n_ae_credit_reason)
        with self.assertRaisesRegex(UserError, "official UAE credit note reason"):
            credit.action_l10n_ae_validate()
        self.env.company.l10n_ae_edi_auto_prepare = True
        credit.action_post()
        self.assertEqual(credit.state, "posted")
        self.assertEqual(credit.l10n_ae_preparation_state, "review")
        self.assertIn(
            "official UAE credit note reason", credit.l10n_ae_preparation_error
        )
        with self.assertRaisesRegex(UserError, "official UAE credit note reason"):
            credit.action_l10n_ae_prepare()
        self.assertEqual((original.state, original.amount_total), ("posted", 105))
        self.assertFalse(credit.l10n_ae_transmission_id)
