# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models


class UaeSelfBillingAgreement(models.Model):
    _name = "l10n_ae.selfbilling.agreement"
    _description = "UAE Self-Billing Agreement"
    _check_company_auto = True

    name = fields.Char(string="Agreement Reference", required=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company, index=True
    )
    partner_id = fields.Many2one(
        "res.partner", string="Supplier", required=True, check_company=True
    )
    active = fields.Boolean(default=True)
    start_date = fields.Date(required=True)
    end_date = fields.Date()
    scope = fields.Text(required=True)
    onboarding_reference = fields.Char(
        string="Supplier Onboarding Evidence",
        required=True,
        help=(
            "Reference to the reviewed evidence that the supplier participates "
            "in the system."
        ),
    )

    _valid_dates = models.Constraint(
        "CHECK(end_date IS NULL OR end_date >= start_date)",
        "The agreement end date cannot precede its start date.",
    )
