# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaeProviderBinding(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()

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
