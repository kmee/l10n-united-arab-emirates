# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Document refusals distinct from validation infrastructure failures."""

from odoo.exceptions import UserError


class UaeDocumentValidationError(UserError):
    """The preserved document needs review before transport can proceed."""

    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report
