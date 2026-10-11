# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import Command
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

    def test_official_credit_metadata_follows_fiscal_country_not_address(self):
        ae = self.env.ref("base.ae")
        fr = self.env.ref("base.fr")
        company_a = self.env.company
        company_a.write(
            {"country_id": fr.id, "account_fiscal_country_id": ae.id, "state_id": False}
        )
        data_b = self.setup_other_company(name="Synthetic foreign fiscal reversal")
        company_b = data_b["company"]
        company_b.write({"country_id": ae.id, "account_fiscal_country_id": fr.id})
        self.simple_accountman.company_ids = [Command.set([company_a.id, company_b.id])]
        source_flags = {
            "l10n_ae_free_zone": True,
            "l10n_ae_deemed_supply": False,
            "l10n_ae_margin_scheme": False,
            "l10n_ae_summary": False,
            "l10n_ae_continuous_supply": False,
            "l10n_ae_disclosed_agent": True,
            "l10n_ae_ecommerce": True,
            "l10n_ae_exports": False,
        }
        for data, address, fiscal, applies in (
            (self.company_data, "FR", "AE", True),
            (data_b, "AE", "FR", False),
        ):
            with self.subTest(address=address, fiscal=fiscal):
                company = data["company"]
                moves = (
                    self.env["account.move"]
                    .with_user(self.simple_accountman)
                    .with_company(company)
                    .with_context(allowed_company_ids=[company.id])
                )
                original = moves.create(
                    {
                        "company_id": company.id,
                        "move_type": "out_invoice",
                        "partner_id": self.partner_a.id,
                        "invoice_date": "2019-01-01",
                        "journal_id": data["default_journal_sale"].id,
                        **source_flags,
                        "l10n_ae_beneficiary_id": "1000000009",
                        "l10n_ae_principal_id": "100000000000003",
                        "invoice_line_ids": [
                            Command.create(
                                {
                                    "name": "Synthetic untaxed reversal service",
                                    "price_unit": 100,
                                    "account_id": data["default_account_revenue"].id,
                                    "tax_ids": [Command.clear()],
                                }
                            )
                        ],
                    }
                )
                self.assertFalse(original.env.su)
                self.assertEqual(original.company_id.country_code, address)
                self.assertEqual(original.country_code, fiscal)
                original.action_post()
                wizard = (
                    original.env["account.move.reversal"]
                    .with_context(active_model="account.move", active_ids=original.ids)
                    .create(
                        {
                            "reason": "Synthetic reduction in consideration",
                            "l10n_ae_credit_reason": "DL8.61.1.C",
                            "journal_id": original.journal_id.id,
                        }
                    )
                )
                self.assertFalse(wizard.env.su)
                self.assertEqual(wizard.l10n_ae_credit_reason_applicable, applies)
                action = wizard.refund_moves()
                credit = wizard.new_move_ids
                self.assertEqual(action["res_id"], credit.id)
                self.assertEqual(
                    (credit.state, credit.move_type), ("draft", "out_refund")
                )
                self.assertEqual(credit.reversed_entry_id, original)
                self.assertEqual(credit.company_id, company)
                self.assertEqual(credit.country_code, fiscal)
                self.assertEqual(credit.amount_total, 100)
                self.assertEqual(
                    credit.l10n_ae_credit_reason, "DL8.61.1.C" if applies else False
                )
                for name, value in source_flags.items():
                    self.assertEqual(credit[name], value if applies else False, name)
                    self.assertEqual(original[name], value, name)
                self.assertEqual(
                    credit.l10n_ae_beneficiary_id, "1000000009" if applies else False
                )
                self.assertEqual(
                    credit.l10n_ae_principal_id, "100000000000003" if applies else False
                )
                self.assertEqual(
                    (original.state, original.amount_total), ("posted", 100)
                )
                self.assertFalse(original.l10n_ae_credit_reason)
