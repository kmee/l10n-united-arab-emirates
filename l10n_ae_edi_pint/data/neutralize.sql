-- Copyright 2026 KMEE
-- License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
-- Keep receipt cursors and originals for local audit; disable scheduled retrieval.
UPDATE l10n_ae_edi_binding
   SET controlled_receive_enabled = false;
