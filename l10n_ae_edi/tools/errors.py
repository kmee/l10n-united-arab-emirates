# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Document refusals distinct from validation infrastructure failures."""

from odoo.exceptions import UserError


class UaeDocumentValidationError(UserError):
    """The preserved document needs review before transport can proceed."""

    def __init__(self, message, report=None, *, kind="local_review"):
        super().__init__(message)
        # The public RPC context lets the core show its standard warning dialog
        # while retaining this typed exception for controlled dispatch handling.
        self.context = {"exception_class": "odoo.exceptions.UserError"}
        self.report = report
        self.kind = kind


class UaeControlledEndpointMismatch(UserError):
    """A frozen transmission must reconcile through its original route."""
