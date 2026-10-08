# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo.exceptions import AccessError, ValidationError

CONTROLLED_CAPABILITIES = (
    "submit",
    "lookup",
    "report",
    "receive",
    "billing_invoice",
    "billing_credit",
    "selfbilling_invoice",
    "selfbilling_credit",
)


def review_controlled_binding(binding, reviewer, *, capabilities, note):
    """Explicitly review a controlled contract as its authorized manager."""
    binding.ensure_one()
    company = binding.company_id
    if (
        not reviewer.active
        or reviewer.share
        or not reviewer.has_group("account.group_account_manager")
        or company not in reviewer.company_ids
    ):
        raise AccessError(
            binding.env._("An authorized accounting capability reviewer is required.")
        )
    codes = tuple(capabilities)
    if (
        not codes
        or len(codes) != len(set(codes))
        or not set(codes) <= set(CONTROLLED_CAPABILITIES)
    ):
        raise ValidationError(
            binding.env._("Use exact declared controlled contract capabilities.")
        )
    actor_binding = binding.with_user(reviewer).with_context(
        allowed_company_ids=[company.id]
    )
    if actor_binding.env.su:
        raise AccessError(
            binding.env._("Capability review must use an ordinary manager session.")
        )
    actor_binding.check_access("write")
    capability_records = actor_binding.env["l10n_ae.edi.capability"].search(
        [("code", "in", list(codes))]
    )
    if set(capability_records.mapped("code")) != set(codes):
        raise ValidationError(
            binding.env._("The exact controlled contract capabilities are required.")
        )
    actor_binding.write(
        {
            "capability_profile": "kmee-controlled-1",
            "capability_ids": [(6, 0, capability_records.ids)],
            "capability_review_note": note,
        }
    )
    actor_binding.action_review_capabilities()
    return binding
