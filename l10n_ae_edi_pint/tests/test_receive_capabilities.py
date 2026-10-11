# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_ae_edi.tests.common import review_controlled_binding

from .common import requires_normative_resources


@tagged("post_install", "-at_install")
class TestUaeReceiveCapabilities(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(
            cls.env,
            login="uae-receive-capability-manager",
            groups="account.group_account_manager",
            company_id=cls.env.company.id,
            company_ids=[(6, 0, cls.env.company.ids)],
        )

    def test_receive_only_review_retains_original_and_deduplicates_delivery(self):
        actor = (
            self.env["l10n_ae.edi.binding"]
            .with_user(self.manager)
            .with_context(allowed_company_ids=self.env.company.ids)
        )
        binding = actor.create({"name": "Synthetic receive-only contract"})
        review_controlled_binding(
            binding,
            self.manager,
            capabilities=("receive",),
            note="Synthetic receive-only contract; no outbound operation is declared.",
        )
        self.assertEqual(binding.capability_review_state, "ready")
        self.assertEqual(binding.capability_ids.mapped("code"), ["receive"])
        self.assertEqual(binding.capability_reviewed_by, self.manager)
        self.assertFalse(binding.controlled_dispatch_enabled)
        receiver = actor.env["l10n_ae.edi.inbox"]
        original = receiver.receive_controlled_document(
            binding.id,
            "synthetic-receive-only-original",
            b"<Invoice/>",
            event_id="synthetic-receive-only-delivery",
        )
        self.assertEqual(original.original_xml.content, b"<Invoice/>")
        self.assertEqual(
            original.xml_hash,
            "b8dbbfb2620e9346480364a8d935a0ff2038ce9bac7738f5ec58f22e639d2607",
        )
        self.assertEqual(original.company_id, self.env.company)
        self.assertEqual(original.binding_id, binding)
        self.assertEqual(original.state, "received")
        self.assertFalse(original.move_id)
        self.assertEqual(
            receiver.receive_controlled_document(
                binding.id,
                "synthetic-receive-only-original",
                b"<Invoice/>",
                event_id="synthetic-receive-only-delivery",
            ),
            original,
        )
        self.assertEqual(
            original.ingestion_event_ids.mapped("event_id"),
            ["synthetic-receive-only-delivery"],
        )
        self.assertEqual(receiver.search_count([("binding_id", "=", binding.id)]), 1)

    def test_unreviewed_receive_contract_refuses_before_original_and_event(self):
        company = self.env.company
        actor = (
            self.env["l10n_ae.edi.binding"]
            .with_user(self.manager)
            .with_context(allowed_company_ids=company.ids)
        )
        binding = actor.create({"name": "Synthetic unreviewed receive contract"})
        receiver = actor.env["l10n_ae.edi.inbox"]
        events = actor.env["l10n_ae.edi.inbox.ingestion"]
        self.assertFalse(actor.env.su)
        self.assertEqual(binding.capability_review_state, "review")
        self.assertFalse(binding.capability_reviewed_by)
        for elevated in (False, True):
            with self.subTest(sudo=elevated):
                caller = receiver.sudo() if elevated else receiver
                with (
                    self.cr.savepoint(),
                    self.assertRaisesRegex(
                        ValidationError, "Review.*contract capabilities.*receiving"
                    ),
                ):
                    caller.receive_controlled_document(
                        binding.id,
                        "synthetic-unreviewed-contract",
                        b"<Invoice/>",
                        event_id="synthetic-unreviewed-delivery",
                    )
                self.assertFalse(
                    receiver.search_count([("binding_id", "=", binding.id)])
                )
                self.assertFalse(events.search_count([("binding_id", "=", binding.id)]))

    def test_technical_receiver_uses_reviewed_contract_without_reviewing_it(self):
        company = self.env.company
        operator = new_test_user(
            self.env,
            login="uae-receive-capability-service",
            groups="l10n_ae_edi_pint.group_document_receiver",
            company_id=company.id,
            company_ids=[(6, 0, company.ids)],
        )
        binding = (
            self.env["l10n_ae.edi.binding"]
            .with_user(self.manager)
            .create({"name": "Synthetic technical receiver contract"})
        )
        caller_binding = binding.with_user(operator)
        receiver = caller_binding.env["l10n_ae.edi.inbox"]
        self.assertFalse(receiver.env.su)
        self.assertFalse(operator.has_group("account.group_account_manager"))
        with self.cr.savepoint(), self.assertRaises(AccessError):
            caller_binding.action_review_capabilities()
        with (
            self.cr.savepoint(),
            self.assertRaisesRegex(
                ValidationError, "Review.*contract capabilities.*receiving"
            ),
        ):
            receiver.receive_controlled_document(
                binding.id, "synthetic-service-original", b"<Invoice/>"
            )
        self.assertFalse(receiver.search_count([("binding_id", "=", binding.id)]))
        review_controlled_binding(
            binding,
            self.manager,
            capabilities=("receive",),
            note="Synthetic service receives after an ordinary manager's review.",
        )
        original = receiver.receive_controlled_document(
            binding.id,
            "synthetic-service-original",
            b"<Invoice/>",
            event_id="synthetic-service-delivery",
        )
        self.assertEqual(original.original_xml.content, b"<Invoice/>")
        self.assertEqual(original.state, "received")
        self.assertFalse(original.move_id)
        self.assertEqual(original.binding_id.capability_reviewed_by, self.manager)
        with self.cr.savepoint(), self.assertRaises(AccessError):
            original.action_process()
        self.assertEqual(original.state, "received")
        self.assertFalse(original.move_id)

    def test_reviewed_contract_without_receive_refuses_new_original_and_event(self):
        company = self.env.company
        actor = (
            self.env["l10n_ae.edi.binding"]
            .with_user(self.manager)
            .with_context(allowed_company_ids=company.ids)
        )
        binding = actor.create({"name": "Synthetic reviewed reporting-only contract"})
        review_controlled_binding(
            binding,
            self.manager,
            capabilities=("report",),
            note="Synthetic reporting contract; receiving is not declared.",
        )
        self.assertEqual(binding.capability_review_state, "ready")
        self.assertEqual(binding.capability_ids.mapped("code"), ["report"])
        receiver = actor.env["l10n_ae.edi.inbox"]
        events = actor.env["l10n_ae.edi.inbox.ingestion"]
        for elevated in (False, True):
            with self.subTest(sudo=elevated):
                caller = receiver.sudo() if elevated else receiver
                with (
                    self.cr.savepoint(),
                    self.assertRaisesRegex(ValidationError, "does not support receive"),
                ):
                    caller.receive_controlled_document(
                        binding.id,
                        "synthetic-undeclared-receive",
                        b"<Invoice/>",
                        event_id="synthetic-undeclared-delivery",
                    )
                self.assertFalse(
                    receiver.search_count([("binding_id", "=", binding.id)])
                )
                self.assertFalse(events.search_count([("binding_id", "=", binding.id)]))

    @requires_normative_resources("1.0.4")
    def test_invalidated_receive_contract_keeps_retained_original_review_available(
        self,
    ):
        actor = self.env["l10n_ae.edi.binding"].with_user(self.manager)
        binding = actor.create({"name": "Synthetic retained original recovery"})
        review_controlled_binding(
            binding,
            self.manager,
            capabilities=("receive",),
            note="Synthetic receive-only contract before retaining an original.",
        )
        receiver = actor.env["l10n_ae.edi.inbox"]
        original = receiver.receive_controlled_document(
            binding.id,
            "synthetic-retained-original",
            b"<Invoice/>",
            event_id="synthetic-retained-delivery",
        )
        immutable_fields = [
            "remote_id",
            "xml_hash",
            "received_at",
            "company_id",
            "binding_id",
        ]
        retained = original.read(immutable_fields)
        binding.write({"capability_review_note": "Changed synthetic receive contract."})
        self.assertEqual(binding.capability_review_state, "review")
        with (
            self.cr.savepoint(),
            self.assertRaisesRegex(
                ValidationError, "Review.*contract capabilities.*receiving"
            ),
        ):
            receiver.receive_controlled_document(
                binding.id,
                "synthetic-new-after-review-change",
                b"<Invoice/>",
                event_id="synthetic-new-delivery",
            )
        self.assertEqual(receiver.search_count([("binding_id", "=", binding.id)]), 1)
        # The intentionally non-fiscal XML is retained and diagnosed locally;
        # it must not fabricate a bill or require the new transport contract.
        self.assertFalse(original.action_process())
        self.assertEqual(original.state, "review")
        self.assertTrue(original.diagnostic)
        self.assertFalse(original.move_id)
        self.assertEqual(len(original.processing_history_ids), 1)
        self.assertEqual(original.read(immutable_fields), retained)
        self.assertEqual(original.original_xml.content, b"<Invoice/>")
        self.assertEqual(
            original.ingestion_event_ids.mapped("event_id"),
            ["synthetic-retained-delivery"],
        )
