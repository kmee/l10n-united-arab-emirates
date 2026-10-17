# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
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
                result["params"]["data"]["message"],
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
        for xmlid in (
            "l10n_ae_edi.ir_cron_deadline_reminders",
            "l10n_ae_edi_pint.ir_cron_incoming_review",
        ):
            self.assertTrue(self.env.ref(xmlid).active)
