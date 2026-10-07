# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Identify historical opened cases without inventing confirmation originals."""


def migrate(cr, version):
    # A stored classification is the only changed fact. In particular, do not
    # use ORM write: preserve write_date, opening actor/time, due and activities.
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
