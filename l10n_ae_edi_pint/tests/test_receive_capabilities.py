# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_ae_edi.tests.common import review_controlled_binding


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
