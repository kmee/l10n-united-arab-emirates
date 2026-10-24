-- Copyright 2026 KMEE
-- License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
-- Native Odoo neutralization must preserve fiscal facts and disable transport opt-ins.
UPDATE l10n_ae_edi_binding
   SET controlled_dispatch_enabled = false;
