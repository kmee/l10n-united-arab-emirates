# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Also classify cases upgraded through the published v32 intake prototype."""


def migrate(cr, version):
    # The earliest published v32 commit added the original gate before its
    # classification migration existed. Same-version updates skip that hook.
    # Repeat the classification in v33 without changing historic audit fields.
    cr.execute(
        """
        UPDATE l10n_ae_edi_incident AS incident
           SET confirmation_intake_mode = 'legacy_opened'
         WHERE incident.incident_kind = 'registration_change'
           AND incident.state = 'open'
           AND NOT EXISTS (
               SELECT 1
                 FROM l10n_ae_edi_incident_confirmation AS confirmation
                WHERE confirmation.incident_id = incident.id
           )
        """
    )
