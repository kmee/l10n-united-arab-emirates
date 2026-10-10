# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Native public action and Form coverage for correction before sending."""

from odoo.exceptions import AccessError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_ae_edi.tests.common import (
    CONTROLLED_CAPABILITIES,
    review_controlled_binding,
)

# Deliberately excludes local_correction_id: beginning a correction is the one
# trusted history annotation expected to change on the frozen source.
FROZEN_SOURCE_FIELDS = [
    "company_id",
    "move_id",
    "binding_id",
    "provider",
    "environment",
    "process",
    "exchange_mode",
    "origin_system",
    "origin_document_id",
    "revision",
    "payload",
    "payload_hash",
    "xml_content",
    "xml_hash",
    "idempotency_key",
    "submission_state",
    "remote_id",
    "validation_receipt",
    "attempt_ids",
    "event_ids",
]


@tagged("post_install", "-at_install")
class TestUaeLocalCorrectionWizard(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        company.write({"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True})
        company.partner_id.write(
            {
                "vat": "100000000100003",
                "street": "Synthetic supplier street",
                "city": "Dubai",
                "state_id": cls.env.ref("base.state_ae_du").id,
            }
        )
        cls.partner_a.write(
            {
                "country_id": cls.env.ref("base.ae").id,
                "vat": "100000000200003",
                "street": "Synthetic buyer street",
                "city": "Dubai",
                "state_id": cls.env.ref("base.state_ae_du").id,
            }
        )
        if "account.edi.ubl.kmee_ae" in cls.env:
            # Explicit synthetic PINT identifiers, never customer registrations.
            cls.product_a.l10n_ae_classification_code = "999999"
            for partner, endpoint, license_number in (
                (company.partner_id, "1000000001", "SYNTH-CORRECTION-0001"),
                (cls.partner_a, "1000000002", "SYNTH-CORRECTION-0002"),
            ):
                partner.write(
                    {
                        "routing_scheme": "0235",
                        "routing_endpoint": endpoint,
                        "l10n_ae_registration_type": "TL",
                        "l10n_ae_registration_authority": "Synthetic UAE Authority",
                        "additional_identifiers": {"AE_TL": license_number},
                    }
                )
        cls.binding = cls.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic local-correction sandbox",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        review_controlled_binding(
            cls.binding,
            cls.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Reviewed synthetic contract for local-correction tests.",
        )
        company.l10n_ae_edi_binding_id = cls.binding
        cls.editor = new_test_user(
            cls.env,
            login="uae-local-correction-invoice-editor",
            groups="account.group_account_invoice",
            company_id=company.id,
            company_ids=[(6, 0, company.ids)],
        )

    def _queue_issued_invoice(self):
        company = self.env.company
        company.l10n_ae_edi_auto_prepare = True
        move = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        move.l10n_ae_binding_id = self.binding
        actor = move.with_user(self.editor).with_context(
            allowed_company_ids=company.ids
        )
        self.assertFalse(actor.env.su)
        self.assertFalse(self.editor.has_group("account.group_account_manager"))
        self.assertFalse(self.editor.has_group("base.group_system"))
        actor.action_post()
        source = actor.l10n_ae_transmission_id
        self.assertEqual((actor.state, source.submission_state), ("posted", "queued"))
        self.assertFalse(source.attempt_ids)
        self.assertFalse(source.event_ids)
        self.assertEqual(
            (actor.amount_untaxed, actor.amount_tax, actor.amount_total), (100, 5, 105)
        )
        return actor, source

    def _wizard_from_public_action(self, actor):
        action = actor.action_l10n_ae_start_local_correction()
        self.assertEqual(action["type"], "ir.actions.act_window")
        self.assertEqual(action["res_model"], "l10n_ae.edi.correction.wizard")
        self.assertEqual(action["view_mode"], "form")
        self.assertEqual(action["target"], "new")
        self.assertEqual(action["context"]["default_move_id"], actor.id)
        self.assertEqual(
            action["context"]["default_source_transmission_id"],
            actor.l10n_ae_transmission_id.id,
        )
        wizard_model = (
            self.env["l10n_ae.edi.correction.wizard"]
            .with_user(actor.env.user)
            .with_context(allowed_company_ids=actor.company_id.ids, **action["context"])
        )
        with Form(wizard_model) as form:
            self.assertEqual(form.move_id, actor)
            self.assertEqual(form.source_transmission_id, actor.l10n_ae_transmission_id)
            form.reason = "Corrected the synthetic unit price before sending."
        wizard = form.record
        self.assertFalse(wizard.env.su)
        return wizard

    def _source_facts(self, source):
        # Compare stored relation IDs: native reset-to-draft changes the move's
        # display name, which is not part of the frozen transmission evidence.
        return source.read(FROZEN_SOURCE_FIELDS, load=None)

    def test_invoice_user_corrects_through_native_action_and_form_wizard(self):
        actor, source = self._queue_issued_invoice()
        original_source = self._source_facts(source)
        if "account.edi.ubl.kmee_ae" in self.env:
            self.assertTrue(source.xml_content)
        wizard = self._wizard_from_public_action(actor)
        result = wizard.action_confirm()
        self.assertEqual(result["type"], "ir.actions.act_window")
        self.assertEqual(result["res_model"], "account.move")
        self.assertEqual(result["res_id"], actor.id)
        self.assertEqual(actor.state, "draft")
        self.assertFalse(actor.l10n_ae_transmission_id)
        correction = actor.l10n_ae_correction_id
        self.assertTrue(correction)
        self.assertEqual(correction.state, "editing")
        self.assertEqual(correction.move_id, actor)
        self.assertEqual(correction.company_id, actor.company_id)
        self.assertEqual(correction.source_transmission_id, source)
        self.assertEqual(
            correction.reason, "Corrected the synthetic unit price before sending."
        )
        self.assertEqual(correction.started_by, self.editor)
        self.assertEqual(wizard.create_uid, self.editor)
        self.assertTrue(correction.started_at)
        self.assertEqual(source.local_correction_id, correction)
        self.assertEqual(self._source_facts(source), original_source)

        # Exercise an ordinary invoice edit through the native move Form while the
        # correction is in draft, then use native posting/auto-prepare for revision 2.
        with (
            Form(actor) as move_form,
            move_form.invoice_line_ids.edit(0) as line_form,
        ):
            line_form.price_unit = 110
        self.assertEqual(
            (actor.amount_untaxed, actor.amount_tax, actor.amount_total),
            (110, 5.5, 115.5),
        )
        actor.action_post()

        replacement = actor.l10n_ae_transmission_id
        self.assertTrue(replacement)
        self.assertEqual(
            (actor.state, replacement.submission_state), ("posted", "queued")
        )
        self.assertEqual((source.revision, replacement.revision), (1, 2))
        self.assertEqual(replacement.company_id, source.company_id)
        self.assertEqual(replacement.process, source.process)
        self.assertEqual(replacement.origin_system, source.origin_system)
        self.assertEqual(replacement.origin_document_id, source.origin_document_id)
        self.assertEqual(replacement.binding_id, source.binding_id)
        self.assertNotEqual(replacement.idempotency_key, source.idempotency_key)
        self.assertNotEqual(replacement.payload_hash, source.payload_hash)
        self.assertEqual(bool(replacement.xml_content), bool(source.xml_content))
        if source.xml_content:
            self.assertNotEqual(replacement.xml_content, source.xml_content)
            self.assertNotEqual(replacement.xml_hash, source.xml_hash)
        self.assertFalse(replacement.attempt_ids)
        self.assertFalse(replacement.event_ids)
        self.assertEqual(
            actor.l10n_ae_transmission_ids.sorted("revision"), source | replacement
        )
        self.assertEqual(correction.replacement_transmission_id, replacement)
        self.assertEqual(correction.state, "completed")
        self.assertTrue(correction.completed_at)
        self.assertEqual(self._source_facts(source), original_source)
        self.assertEqual(source.local_correction_id, correction)

    def test_readonly_and_foreign_company_cannot_start_or_confirm_correction(self):
        from_company = self.env.company
        other_company = self.env["res.company"].create(
            {"name": "Synthetic foreign correction company"}
        )
        readonly = new_test_user(
            self.env,
            login="uae-local-correction-readonly",
            groups="account.group_account_readonly",
            company_id=from_company.id,
            company_ids=from_company.ids,
        )
        foreign_editor = new_test_user(
            self.env,
            login="uae-local-correction-foreign-editor",
            groups="account.group_account_invoice",
            company_id=other_company.id,
            company_ids=other_company.ids,
        )

        move, source = self._queue_issued_invoice()
        ledger_fields = [
            "date",
            "account_id",
            "partner_id",
            "name",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "currency_id",
            "tax_ids",
            "tax_tag_ids",
            "display_type",
            "quantity",
            "price_unit",
            "product_id",
            "analytic_distribution",
            "write_uid",
            "write_date",
        ]

        def snapshot():
            # Observe completed native accounting writes before comparing audit
            # fields across a refused operation; keep every ledger assertion.
            self.env.flush_all()
            return {
                "move": move.read(
                    [
                        "company_id",
                        "state",
                        "write_date",
                        "amount_untaxed",
                        "amount_tax",
                        "amount_total",
                        "l10n_ae_transmission_id",
                        "l10n_ae_correction_id",
                    ]
                ),
                "ledger": move.line_ids.sorted("id").read(ledger_fields, load=None),
                "source": self._source_facts(source),
                "source_correction": source.local_correction_id.id,
                "history_count": self.env["l10n_ae.edi.correction"].search_count(
                    [("move_id", "=", move.id)]
                ),
            }

        before = snapshot()
        self.assertEqual(
            (move.amount_untaxed, move.amount_tax, move.amount_total), (100, 5, 105)
        )
        self.assertFalse(source.local_correction_id)
        self.assertEqual(before["history_count"], 0)

        readonly_move = move.with_user(readonly).with_context(
            allowed_company_ids=from_company.ids
        )
        self.assertFalse(readonly_move.env.su)
        with self.assertRaises(AccessError):
            readonly_move.action_l10n_ae_start_local_correction()
        self.assertEqual(snapshot(), before)

        foreign_move = move.with_user(foreign_editor).with_context(
            allowed_company_ids=other_company.ids
        )
        self.assertFalse(foreign_move.env.su)
        with self.assertRaises(AccessError):
            foreign_move.action_l10n_ae_start_local_correction()
        self.assertEqual(snapshot(), before)

        # Create through the public transient ORM as the foreign actor, with forged
        # foreign-actor defaults and explicit IDs. This makes the transient belong to
        # that actor, so confirm cannot fail merely because someone else owns it.
        foreign_wizard_model = (
            self.env["l10n_ae.edi.correction.wizard"]
            .with_user(foreign_editor)
            .with_context(
                allowed_company_ids=other_company.ids,
                default_move_id=move.id,
                default_source_transmission_id=source.id,
            )
        )
        foreign_wizard = foreign_wizard_model.create(
            {
                "move_id": move.id,
                "source_transmission_id": source.id,
                "reason": "Synthetic forged cross-company correction attempt.",
            }
        )
        self.assertFalse(foreign_wizard.env.su)
        self.assertEqual(foreign_wizard.create_uid, foreign_editor)
        self.assertEqual(foreign_wizard.move_id.id, move.id)
        self.assertEqual(foreign_wizard.source_transmission_id.id, source.id)
        with self.assertRaises(AccessError):
            foreign_wizard.action_confirm()
        self.assertEqual(snapshot(), before)
