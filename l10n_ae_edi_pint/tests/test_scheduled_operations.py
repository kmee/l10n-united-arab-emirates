# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import Command
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import mute_logger


@tagged("post_install", "-at_install")
class TestUaeControlledCronDefaults(TransactionCase):
    @mute_logger("odoo.addons.base.models.ir_cron")
    def test_native_incoming_actions_refuse_administrator_scheduler(self):
        """Run Manually must not turn a default administrator into the executor."""
        administrator = self.env.ref("base.user_admin")
        before = (
            self.env["l10n_ae.edi.inbox"]
            .search([])
            .read(["state", "xml_hash", "move_id", "processing_history_ids"])
        )
        for xmlid in (
            "l10n_ae_edi_pint.ir_cron_controlled_receive",
            "l10n_ae_edi_pint.ir_cron_incoming_review",
        ):
            cron = self.env.ref(xmlid)
            cron.user_id = administrator
            with self.enter_registry_test_mode(), self.registry.cursor() as cursor:
                result = cron.with_env(self.env(cr=cursor)).method_direct_trigger()
            self.assertIsInstance(
                result,
                dict,
                "Native incoming actions must refuse an administrator executor.",
            )
            self.assertEqual(result["tag"], "display_exception")
            self.assertIn(
                "non-administrator",
                result["params"]["data"]["debug"],
            )
        self.assertEqual(
            self.env["l10n_ae.edi.inbox"]
            .search([])
            .read(["state", "xml_hash", "move_id", "processing_history_ids"]),
            before,
        )

    def test_controlled_transport_requires_explicit_scheduler_activation(self):
        for xmlid in (
            "l10n_ae_edi.ir_cron_controlled_dispatch",
            "l10n_ae_edi_pint.ir_cron_controlled_receive",
        ):
            cron = self.env.ref(xmlid)
            self.assertFalse(
                cron.active,
                "A fresh installation must leave controlled transport inactive "
                "until an administrator reviews its scheduler user and activates it.",
            )
            self.assertEqual(cron.interval_number, 1)
            self.assertEqual(cron.interval_type, "minutes")
        self.assertTrue(self.env.ref("l10n_ae_edi.ir_cron_deadline_reminders").active)

    def test_incoming_review_waits_for_explicit_scheduler_configuration(self):
        self.assertFalse(
            self.env.ref("l10n_ae_edi_pint.ir_cron_incoming_review").active,
            "Fresh incoming review must wait for an ordinary scheduler account.",
        )

    @mute_logger("odoo.addons.base.models.ir_cron")
    def test_native_incoming_scheduler_rechecks_user_administration_rights(self):
        """An ordinary executor works again after administrative rights are removed."""
        for xmlid, role in (
            (
                "l10n_ae_edi_pint.ir_cron_controlled_receive",
                "l10n_ae_edi_pint.group_document_receiver",
            ),
            (
                "l10n_ae_edi_pint.ir_cron_incoming_review",
                "account.group_account_manager",
            ),
        ):
            actor = new_test_user(
                self.env,
                login="synthetic-incoming-scheduler-" + str(self.env.ref(role).id),
                groups=role,
                company_id=self.env.company.id,
                company_ids=self.env.company.ids,
            )
            self.assertTrue(actor.active)
            self.assertFalse(actor.share)
            self.assertFalse(actor.has_group("base.group_system"))
            self.assertFalse(actor.has_group("base.group_erp_manager"))
            if role == "l10n_ae_edi_pint.group_document_receiver":
                self.assertFalse(actor.has_group("account.group_account_manager"))
                self.assertFalse(
                    self.env["account.move"].with_user(actor).has_access("read")
                )
            cron = self.env.ref(xmlid)
            cron.write({"user_id": actor.id, "active": False})
            original_groups = actor.group_ids
            for elevated in (False, True, False):
                actor.group_ids = (
                    [Command.link(self.env.ref("base.group_erp_manager").id)]
                    if elevated
                    else [Command.set(original_groups.ids)]
                )
                self.assertEqual(actor.has_group("base.group_erp_manager"), elevated)
                self.assertFalse(actor.has_group("base.group_system"))
                with self.enter_registry_test_mode(), self.registry.cursor() as cursor:
                    result = cron.with_env(self.env(cr=cursor)).method_direct_trigger()
                if elevated:
                    self.assertIsInstance(result, dict)
                    self.assertEqual(result["tag"], "display_exception")
                    self.assertIn(
                        "non-administrator", result["params"]["data"]["debug"]
                    )
                else:
                    self.assertIs(result, True)
