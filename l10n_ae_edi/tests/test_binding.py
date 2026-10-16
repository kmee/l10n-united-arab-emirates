# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaeProviderBinding(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()

    def test_manager_reviews_contract_capabilities_before_enabling_sending(self):
        binding = (
            self.env["l10n_ae.edi.binding"]
            .create({"name": "Synthetic explicitly reviewed sending contract"})
            .with_user(self.simple_accountman)
        )
        self.assertFalse(binding.env.su)
        with (
            self.assertRaisesRegex(ValidationError, "Review.*capabilities"),
            self.cr.savepoint(),
            Form(binding) as form,
        ):
            form.controlled_dispatch_enabled = True
        self.assertFalse(binding.controlled_dispatch_enabled)
        self.assertFalse(binding.capability_reviewed_at)
        capabilities = self.env["l10n_ae.edi.capability"].search(
            [("code", "in", ["submit", "lookup", "report", "billing_invoice"])]
        )
        self.assertEqual(len(capabilities), 4)
        with Form(binding) as form:
            form.capability_profile = "kmee-controlled-1"
            form.capability_review_note = "Compared with the KMEE simulation contract."
            for capability in capabilities:
                form.capability_ids.add(capability)
        binding.action_review_capabilities()
        self.assertEqual(binding.capability_reviewed_by, self.simple_accountman)
        self.assertTrue(binding.capability_reviewed_at)
        with Form(binding) as form:
            form.controlled_dispatch_enabled = True
        self.assertTrue(binding.controlled_dispatch_enabled)
        self.assertEqual(binding.environment, "sandbox")
        self.assertEqual(binding.capability_profile, "kmee-controlled-1")

    def test_only_one_active_production_provider_per_legal_entity(self):
        binding = self.env["l10n_ae.edi.binding"]
        values = {
            "name": "Synthetic ASP",
            "company_id": self.env.company.id,
            "environment": "production",
            "provider": "controlled",
        }
        binding.create(values)
        with (
            self.assertRaisesRegex(ValidationError, "one active production ASP"),
            self.cr.savepoint(),
        ):
            binding.create(values)

    def test_contract_edit_and_enable_requires_a_separate_review(self):
        capabilities = self.env["l10n_ae.edi.capability"].search(
            [("code", "in", ["submit", "lookup", "report", "billing_invoice"])]
        )
        binding = (
            self.env["l10n_ae.edi.binding"]
            .create(
                {
                    "name": "Synthetic changed contract enable refusal",
                    "capability_profile": "kmee-controlled-1",
                    "capability_ids": [(6, 0, capabilities.ids)],
                    "capability_review_note": "Original reviewed simulation contract.",
                }
            )
            .with_user(self.simple_accountman)
        )
        binding.action_review_capabilities()
        review_fields = [
            "capability_review_note",
            "capability_reviewed_by",
            "capability_reviewed_at",
            "capability_review_hash",
            "controlled_dispatch_enabled",
        ]
        original = binding.read(review_fields)
        with (
            self.assertRaisesRegex(ValidationError, "Review.*changed.*capabilities"),
            self.cr.savepoint(),
            Form(binding) as form,
        ):
            form.capability_review_note = "Different proposed simulation contract."
            form.controlled_dispatch_enabled = True
        self.assertEqual(binding.read(review_fields), original)

    def test_accounting_user_cannot_read_another_company_binding(self):
        other = self.env["res.company"].create({"name": "Other synthetic legal entity"})
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Other company's mock", "company_id": other.id}
        )
        user = new_test_user(
            self.env,
            login="uae-isolated-accountant",
            groups="account.group_account_manager",
            company_id=self.env.company.id,
            company_ids=[(6, 0, self.env.company.ids)],
        )
        with self.assertRaises(AccessError):
            binding.with_user(user).with_context(
                allowed_company_ids=self.env.company.ids
            ).read(["name"])

    def test_manager_cannot_move_reviewed_binding_to_unauthorized_company(self):
        company = self.env.company
        other = self.env["res.company"].create(
            {"name": "Synthetic unauthorized binding destination"}
        )
        manager = new_test_user(
            self.env,
            login="uae-capability-company-manager",
            groups="account.group_account_manager",
            company_id=company.id,
            company_ids=[(6, 0, company.ids)],
        )
        capabilities = self.env["l10n_ae.edi.capability"].search(
            [("code", "in", ["submit", "lookup", "report", "billing_invoice"])]
        )
        binding = (
            self.env["l10n_ae.edi.binding"]
            .create(
                {
                    "name": "Synthetic reviewed company-isolated contract",
                    "company_id": company.id,
                    "capability_profile": "kmee-controlled-1",
                    "capability_ids": [(6, 0, capabilities.ids)],
                    "capability_review_note": "Reviewed only for this legal entity.",
                }
            )
            .with_user(manager)
            .with_context(allowed_company_ids=company.ids)
        )
        self.assertFalse(binding.env.su)
        self.assertNotIn(other, manager.company_ids)
        binding.action_review_capabilities()
        binding.controlled_dispatch_enabled = True
        evidence_fields = [
            "company_id",
            "capability_ids",
            "capability_reviewed_by",
            "capability_reviewed_at",
            "capability_review_hash",
            "capability_review_state",
            "controlled_dispatch_enabled",
        ]
        original = binding.read(evidence_fields)
        for elevated in (False, True):
            actor = binding.sudo() if elevated else binding
            with self.subTest(elevated=elevated):
                with self.cr.savepoint(), self.assertRaises(AccessError):
                    actor.write({"company_id": other.id})
                self.assertEqual(binding.read(evidence_fields), original)

    def test_company_change_requires_active_destination_and_new_contract_review(self):
        company = self.env.company
        other = self.env["res.company"].create(
            {"name": "Synthetic authorized capability destination"}
        )
        manager = new_test_user(
            self.env,
            login="uae-capability-active-company-manager",
            groups="account.group_account_manager",
            company_id=company.id,
            company_ids=[(6, 0, (company + other).ids)],
        )
        capabilities = self.env["l10n_ae.edi.capability"].search(
            [("code", "in", ["submit", "lookup", "report", "billing_invoice"])]
        )
        binding = (
            self.env["l10n_ae.edi.binding"]
            .create(
                {
                    "name": "Synthetic destination context review",
                    "company_id": company.id,
                    "capability_profile": "kmee-controlled-1",
                    "capability_ids": [(6, 0, capabilities.ids)],
                    "capability_review_note": "Original company contract review.",
                }
            )
            .with_user(manager)
            .with_context(allowed_company_ids=company.ids)
        )
        binding.action_review_capabilities()
        binding.controlled_dispatch_enabled = True
        review_fields = [
            "company_id",
            "capability_reviewed_by",
            "capability_reviewed_at",
            "capability_review_hash",
            "controlled_dispatch_enabled",
        ]
        original = binding.read(review_fields)
        for elevated in (False, True):
            actor = binding.sudo() if elevated else binding
            with self.subTest(elevated=elevated):
                with self.cr.savepoint(), self.assertRaises(AccessError):
                    actor.write({"company_id": other.id})
                self.assertEqual(binding.read(review_fields), original)
        selected = binding.with_context(allowed_company_ids=(company + other).ids)
        self.assertFalse(selected.env.su)
        selected.write({"company_id": other.id})
        self.assertEqual(selected.company_id, other)
        self.assertEqual(selected.capability_review_state, "review")
        self.assertFalse(selected.capability_reviewed_by)
        self.assertFalse(selected.capability_reviewed_at)
        self.assertFalse(selected.capability_review_hash)
        self.assertFalse(selected.controlled_dispatch_enabled)
        selected.action_review_capabilities()
        self.assertEqual(selected.capability_reviewed_by, manager)
        self.assertEqual(selected.capability_review_state, "ready")

    def test_controlled_local_uuid_assignment_cannot_be_enabled_in_production(self):
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Explicit UUID simulation",
                "fiscal_uuid_policy": "controlled_local",
            }
        )
        with (
            self.assertRaisesRegex(ValidationError, "controlled sandbox"),
            self.cr.savepoint(),
        ):
            binding.environment = "production"
        self.assertEqual(binding.environment, "sandbox")
        with (
            self.assertRaisesRegex(ValidationError, "controlled sandbox"),
            self.cr.savepoint(),
        ):
            self.env["l10n_ae.edi.binding"].create(
                {
                    "name": "Invalid local production UUID",
                    "environment": "production",
                    "fiscal_uuid_policy": "controlled_local",
                }
            )

    def test_environment_roundtrip_requires_fresh_contract_review(self):
        capabilities = self.env["l10n_ae.edi.capability"].search(
            [("code", "in", ["submit", "lookup", "report", "billing_invoice"])]
        )
        self.assertEqual(len(capabilities), 4)
        manager = self.simple_accountman
        self.assertFalse(manager.has_group("base.group_system"))
        binding = (
            self.env["l10n_ae.edi.binding"]
            .with_user(manager)
            .with_context(allowed_company_ids=self.env.company.ids)
            .create(
                {
                    "name": "Synthetic environment roundtrip contract",
                    "company_id": self.env.company.id,
                    "environment": "sandbox",
                    "fiscal_uuid_policy": "supplied",
                    "capability_profile": "kmee-controlled-1",
                    "capability_ids": [(6, 0, capabilities.ids)],
                    "capability_review_note": "Synthetic isolated test configuration.",
                }
            )
        )
        self.assertFalse(binding.env.su)
        declaration_fields = [
            "company_id",
            "provider",
            "fiscal_uuid_policy",
            "capability_profile",
            "capability_ids",
            "capability_review_note",
        ]
        declaration = binding.read(declaration_fields)
        review_fields = [
            "environment",
            "capability_review_state",
            "capability_reviewed_by",
            "capability_reviewed_at",
            "capability_review_hash",
            "controlled_dispatch_enabled",
        ]
        binding.action_review_capabilities()
        self.assertEqual(binding.capability_review_state, "ready")
        self.assertEqual(binding.capability_reviewed_by, manager)
        self.assertTrue(binding.capability_reviewed_at)
        self.assertTrue(binding.capability_review_hash)
        with Form(binding) as form:
            form.controlled_dispatch_enabled = True
        self.assertTrue(binding.controlled_dispatch_enabled)

        with Form(binding) as form:
            form.environment = "production"
        self.assertEqual(binding.environment, "production")
        self.assertEqual(binding.capability_review_state, "review")
        self.assertFalse(binding.capability_reviewed_by)
        self.assertFalse(binding.capability_reviewed_at)
        self.assertFalse(binding.capability_review_hash)
        self.assertFalse(binding.controlled_dispatch_enabled)
        production = binding.read(review_fields)
        with (
            self.assertRaisesRegex(ValidationError, "controlled sandbox"),
            self.cr.savepoint(),
        ):
            binding.action_review_capabilities()
        self.assertEqual(binding.read(review_fields), production)
        # The native production form hides this switch; public ORM must refuse it
        # too. The test never constructs a production client or sends a document.
        with (
            self.assertRaisesRegex(ValidationError, "requires the sandbox"),
            self.cr.savepoint(),
        ):
            binding.write({"controlled_dispatch_enabled": True})
        self.assertEqual(binding.read(review_fields), production)

        with Form(binding) as form:
            form.environment = "sandbox"
        self.assertEqual(binding.environment, "sandbox")
        self.assertEqual(binding.capability_review_state, "review")
        self.assertFalse(binding.capability_reviewed_by)
        self.assertFalse(binding.capability_reviewed_at)
        self.assertFalse(binding.capability_review_hash)
        self.assertFalse(binding.controlled_dispatch_enabled)
        binding.action_review_capabilities()
        self.assertEqual(binding.capability_review_state, "ready")
        self.assertEqual(binding.capability_reviewed_by, manager)
        self.assertTrue(binding.capability_reviewed_at)
        self.assertTrue(binding.capability_review_hash)
        self.assertFalse(binding.controlled_dispatch_enabled)
        with Form(binding) as form:
            form.controlled_dispatch_enabled = True
        self.assertTrue(binding.controlled_dispatch_enabled)
        self.assertEqual(binding.read(declaration_fields), declaration)

    def test_two_entity_contract_reviews_remain_independent(self):
        company_a = self.env.company
        company_b = self.env["res.company"].create(
            {
                "name": "Synthetic UAE legal entity B",
                "country_id": company_a.country_id.id,
            }
        )
        manager = new_test_user(
            self.env,
            login="uae-two-entity-contract-manager",
            groups="account.group_account_manager",
            company_id=company_a.id,
            company_ids=[(6, 0, (company_a + company_b).ids)],
        )
        self.assertFalse(manager.has_group("base.group_system"))
        company_context = {
            "allowed_company_ids": (company_a + company_b).ids,
        }
        binding_model = (
            self.env["l10n_ae.edi.binding"]
            .with_user(manager)
            .with_context(**company_context)
        )
        binding_a = binding_model.create(
            {
                "name": "Synthetic entity A credit contract",
                "company_id": company_a.id,
            }
        )
        binding_b = binding_model.create(
            {
                "name": "Synthetic entity B invoice contract",
                "company_id": company_b.id,
            }
        )
        self.assertFalse(binding_a.env.su)
        self.assertFalse(binding_b.env.su)

        capabilities = self.env["l10n_ae.edi.capability"].with_user(manager)
        credit_codes = ["submit", "lookup", "report", "billing_credit"]
        invoice_codes = ["submit", "lookup", "report", "billing_invoice"]
        credit_capabilities = capabilities.search([("code", "in", credit_codes)])
        invoice_capabilities = capabilities.search([("code", "in", invoice_codes)])
        self.assertEqual(set(credit_capabilities.mapped("code")), set(credit_codes))
        self.assertEqual(set(invoice_capabilities.mapped("code")), set(invoice_codes))

        with Form(binding_a) as form:
            form.capability_profile = "kmee-controlled-1"
            form.capability_review_note = (
                "Entity A reviewed synthetic credit-only sandbox contract."
            )
            for capability in credit_capabilities:
                form.capability_ids.add(capability)
        with Form(binding_b) as form:
            form.capability_profile = "kmee-controlled-1"
            form.capability_review_note = (
                "Entity B reviewed synthetic invoice-only sandbox contract."
            )
            for capability in invoice_capabilities:
                form.capability_ids.add(capability)

        review_fields = [
            "company_id",
            "provider",
            "environment",
            "fiscal_uuid_policy",
            "capability_profile",
            "capability_ids",
            "capability_review_note",
            "capability_review_state",
            "capability_reviewed_by",
            "capability_reviewed_at",
            "capability_review_hash",
            "controlled_dispatch_enabled",
        ]
        company_b_unreviewed = binding_b.read(review_fields)
        binding_a.action_review_capabilities()
        self.assertEqual(binding_b.read(review_fields), company_b_unreviewed)
        self.assertEqual(binding_a.capability_reviewed_by, manager)
        self.assertEqual(binding_a.capability_review_state, "ready")
        self.assertEqual(binding_b.capability_review_state, "review")
        self.assertFalse(binding_b.capability_reviewed_by)
        self.assertFalse(binding_b.capability_reviewed_at)
        self.assertFalse(binding_b.capability_review_hash)
        with (
            self.assertRaisesRegex(ValidationError, "Review.*capabilities"),
            self.cr.savepoint(),
            Form(binding_b) as form,
        ):
            form.controlled_dispatch_enabled = True

        binding_b.action_review_capabilities()
        self.assertEqual(binding_b.capability_reviewed_by, manager)
        self.assertEqual(binding_b.capability_review_state, "ready")
        with Form(binding_a) as form:
            form.controlled_dispatch_enabled = True
        with Form(binding_b) as form:
            form.controlled_dispatch_enabled = True
        self.assertTrue(binding_a.controlled_dispatch_enabled)
        self.assertTrue(binding_b.controlled_dispatch_enabled)
        self.assertEqual(
            set(binding_a.capability_ids.mapped("code")), set(credit_codes)
        )
        self.assertEqual(
            set(binding_b.capability_ids.mapped("code")), set(invoice_codes)
        )

        company_b_snapshot = binding_b.read(review_fields)
        with Form(binding_a) as form:
            form.capability_review_note = (
                "Entity A updated its synthetic credit contract evidence."
            )
        self.assertEqual(binding_a.capability_review_state, "review")
        self.assertFalse(binding_a.capability_reviewed_by)
        self.assertFalse(binding_a.capability_reviewed_at)
        self.assertFalse(binding_a.capability_review_hash)
        self.assertFalse(binding_a.controlled_dispatch_enabled)
        self.assertEqual(binding_b.read(review_fields), company_b_snapshot)

        binding_a.action_review_capabilities()
        self.assertEqual(binding_a.capability_review_state, "ready")
        self.assertFalse(binding_a.controlled_dispatch_enabled)
        self.assertEqual(binding_b.read(review_fields), company_b_snapshot)
