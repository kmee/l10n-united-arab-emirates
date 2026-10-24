# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Known document review diagnostics consumed by the native UBL protocol."""

from odoo.exceptions import UserError


class UaePintExportReview(UserError):
    """A declared invoice fact needs review before PINT generation can succeed."""
