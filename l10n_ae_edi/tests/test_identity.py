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

    def test_document_form_limits_issuance_controls_to_supported_processes(self):
        ae = self.env.ref("base.ae")
        fr = self.env.ref("base.fr")
        company = self.env.company
        company.write(
            {
                "country_id": self.env.ref("base.us").id,
                "account_fiscal_country_id": ae.id,
                "state_id": self.env.ref("base.state_us_5").id,
            }
        )
        foreign = self.setup_other_company(
            name="Synthetic AE address and France fiscal document forms"
        )["company"]
        foreign.write({"country_id": ae.id, "account_fiscal_country_id": fr.id})
        manager = new_test_user(
            self.env,
            login="uae-document-form-accounting-manager",
            groups="account.group_account_manager",
            company_id=company.id,
            company_ids=[Command.set((company | foreign).ids)],
        )
        self.assertFalse(manager.has_group("base.group_erp_manager"))
        self.assertFalse(manager.has_group("base.group_system"))
        selfbilling_journal = self.company_data["default_journal_purchase"].copy(
            {
                "name": "Synthetic self-billing document forms",
                "code": "USBF",
                "is_self_billing": True,
            }
        )
        journal_entry = self.env["account.move"].create(
            {
                "company_id": company.id,
                "journal_id": self.company_data["default_journal_misc"].id,
                "move_type": "entry",
            }
        )
        negative_moves = [journal_entry]
        for move_type in ("in_invoice", "in_refund"):
            negative_moves.append(
                self.init_invoice(move_type, amounts=[100], taxes=self.tax_purchase_a)
            )
        negative_moves.append(
            self.init_invoice(
                "out_invoice",
                amounts=[100],
                taxes=self.env["account.tax"],
                company=foreign,
            )
        )
        attempted_fields = {
            "l10n_ae_binding_id": self.env["l10n_ae.edi.binding"].with_user(manager),
            "l10n_ae_document_category": "commercial",
            "l10n_ae_free_zone": True,
            "l10n_ae_buyer_not_implemented": True,
            "l10n_ae_tax_exchange_rate": "1",
        }
        observed_fields = [
            "move_type",
            "state",
            "company_id",
            "country_code",
            "amount_untaxed",
            "amount_tax",
            "amount_total",
            "currency_id",
            "write_uid",
            "write_date",
            "l10n_ae_received_document",
            "l10n_ae_transmission_id",
            "l10n_ae_fiscal_uuid",
            *attempted_fields,
        ]
        for move in negative_moves:
            actor_move = move.with_user(manager).with_context(
                allowed_company_ids=[move.company_id.id]
            )
            self.assertFalse(actor_move.env.su)
            self.env.flush_all()
            actor_move.invalidate_recordset()
            before = actor_move.read(observed_fields)
            native_form = Form(actor_move)
            for field_name, value in attempted_fields.items():
                with (
                    self.subTest(
                        move_type=move.move_type,
                        field=field_name,
                        fiscal_country=move.country_code,
                    ),
                    self.assertRaisesRegex(
                        AssertionError, f"can't write on invisible field '{field_name}'"
                    ),
                ):
                    setattr(native_form, field_name, value)
            self.env.flush_all()
            actor_move.invalidate_recordset()
            self.assertEqual(actor_move.read(observed_fields), before)

        for move_type in ("out_invoice", "out_refund", "in_invoice", "in_refund"):
            move = self.init_invoice(
                move_type,
                amounts=[100],
                taxes=self.tax_sale_a
                if move_type.startswith("out_")
                else self.tax_purchase_a,
                journal=selfbilling_journal if move_type.startswith("in_") else None,
            )
            actor_move = move.with_user(manager).with_context(
                allowed_company_ids=[company.id]
            )
            self.assertFalse(actor_move.env.su)
            monetary_before = actor_move.read(
                ["amount_untaxed", "amount_tax", "amount_total", "currency_id", "state"]
            )
            native_form = Form(actor_move)
            # These are valid editable draft facts, not authorization to issue.
            native_form.l10n_ae_free_zone = True
            native_form.l10n_ae_beneficiary_id = "1000000044"
            native_form.save()
            self.env.flush_all()
            actor_move.invalidate_recordset()
            self.assertTrue(actor_move.l10n_ae_free_zone)
            self.assertEqual(actor_move.l10n_ae_beneficiary_id, "1000000044")
            self.assertEqual(actor_move.write_uid, manager)
            self.assertEqual(
                actor_move.read(
                    [
                        "amount_untaxed",
                        "amount_tax",
                        "amount_total",
                        "currency_id",
                        "state",
                    ]
                ),
                monetary_before,
            )
            self.assertEqual(actor_move.country_code, "AE")
            self.assertEqual(actor_move.company_id.country_code, "US")
            self.assertFalse(actor_move.l10n_ae_transmission_id)
            self.assertFalse(actor_move.l10n_ae_fiscal_uuid)
