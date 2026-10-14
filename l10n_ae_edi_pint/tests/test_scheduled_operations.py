# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestUaeControlledCronDefaults(TransactionCase):
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
