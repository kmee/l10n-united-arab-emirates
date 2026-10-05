# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Authorize the specific controlled action without changing cron operation settings."""

from odoo import SUPERUSER_ID, Command, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env.ref("l10n_ae_edi.ir_cron_controlled_dispatch").write(
        {
            "group_ids": [
                Command.link(env.ref("l10n_ae_edi.group_controlled_sender").id),
                Command.link(env.ref("account.group_account_manager").id),
            ]
        }
    )
