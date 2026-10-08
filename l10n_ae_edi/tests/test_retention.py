# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from datetime import date

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_ae_edi.tests.common import (
    CONTROLLED_CAPABILITIES,
    review_controlled_binding,
)


@tagged("post_install", "-at_install")
class TestUaeRetention(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        cls.partner_a.write(
            {
                "country_id": cls.env.ref("base.ae").id,
                "state_id": cls.env.ref("base.state_ae_du").id,
            }
        )
        cls.env.company.partner_id.state_id = cls.env.ref("base.state_ae_du")
        if "routing_scheme" in cls.partner_a._fields:
            cls.env.company.partner_id.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000001"}
            )
            cls.partner_a.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000002"}
            )
        cls.binding = review_controlled_binding(
            cls.env["l10n_ae.edi.binding"].create({"name": "Synthetic retention only"}),
            cls.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic reviewed controlled contract fixture.",
        )
        cls.auditor = new_test_user(
            cls.env,
            login="synthetic-retention-auditor",
            groups="base.group_user,l10n_ae_edi.group_fiscal_auditor",
            company_ids=cls.env.company.ids,
            company_id=cls.env.company.id,
        )

    def issue(self):
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        return invoice.l10n_ae_transmission_id

    def obligation(self, issue, years=5, purpose="vat", context=None, **extra):
        model = self.env["l10n_ae.edi.retention"]
        if context:
            model = model.with_context(**context)
        return model.create(
            {
                "name": "Synthetic reviewed obligation",
                "transmission_id": issue.id,
                "record_class": "invoice",
                "tax_purpose": purpose,
                "anchor_kind": "tax_period_end",
                "period_start": "2026-01-01",
                "anchor_date": "2026-12-31",
                "retention_years": years,
                "source_reference": "Synthetic normative reference; not ratified",
                "policy_version": "synthetic-2026-1",
                "review_note": "Independent fixture interval; no production policy.",
                **extra,
            }
        )

    def test_unclassified_original_requires_review(self):
        issue = self.issue()
        status = issue.get_retention_status("2034-01-01")
        self.assertEqual(status["state"], "unclassified")
        self.assertEqual(status["obligation_count"], 0)
        self.assertFalse(status["retain_until"])
        self.assertFalse(status["disposition_reviewable"])

    def test_manager_reviews_issued_obligation_through_native_form(self):
        issue = self.issue()
        with Form(
            self.env["l10n_ae.edi.retention"],
            view="l10n_ae_edi.edi_retention_form",
        ) as form:
            form.name = "Synthetic native form review"
            form.transmission_id = issue
            form.record_class = "invoice"
            form.tax_purpose = "vat"
            form.anchor_kind = "tax_period_end"
            form.period_start = date(2026, 1, 1)
            form.anchor_date = date(2026, 12, 31)
            form.retention_years = 5
            form.source_reference = "Synthetic reviewed source, not ratified"
            form.policy_version = "synthetic-ui-1"
            form.review_note = "Explicit engineering example, not a legal policy"
        policy = form.record
        self.assertEqual(policy.company_id, issue.company_id)
        self.assertEqual(policy.state, "draft")
        policy.action_approve()
        self.assertEqual(policy.retain_until, date(2031, 12, 31))
        self.assertEqual(policy.approved_by_id, self.env.user)
        self.assertTrue(policy.approved_at)
        self.assertEqual(issue.get_retention_status("2030-01-01")["state"], "retained")

    def test_auditor_assesses_all_original_obligations_without_mutation(self):
        issue = self.issue()
        vat = self.obligation(issue)
        corporate = self.obligation(issue, 7, "corporate_tax")
        (vat | corporate).action_approve()
        before = (issue.payload, issue.idempotency_key, issue.move_id.amount_total)
        action = vat.with_user(self.auditor).action_assess_original("2032-01-01")
        self.assertEqual(action["tag"], "display_notification")
        self.assertIn("Retained", action["params"]["message"])
        self.assertIn("2033-12-31", action["params"]["message"])
        self.assertIn("Obligations: 2", action["params"]["message"])
        self.assertIn("does not authorize deletion", action["params"]["message"])
        self.assertEqual(
            before, (issue.payload, issue.idempotency_key, issue.move_id.amount_total)
        )

    def test_auditor_native_retention_form_is_readonly(self):
        policy = self.obligation(self.issue())
        policy.action_approve()
        with Form(
            policy.with_user(self.auditor),
            view="l10n_ae_edi.edi_audit_retention_form",
        ) as form:
            self.assertEqual(form.retain_until, "2031-12-31")
            with self.assertRaises(AssertionError):
                form.name = "Unauthorized rewritten review"
        self.assertEqual(policy.name, "Synthetic reviewed obligation")
        draft = self.obligation(policy.transmission_id)
        with (
            Form(
                draft.with_user(self.auditor),
                view="l10n_ae_edi.edi_audit_retention_form",
            ) as form,
            self.assertRaises(AssertionError),
        ):
            form.name = "Unauthorized draft review"

    def test_manager_places_hold_through_native_dialog(self):
        issue = self.issue()
        policy = self.obligation(issue)
        policy.action_approve()
        action = policy.action_open_hold()
        model = self.env[action["res_model"]].with_context(**action["context"])
        with Form(model, view=action["view_id"]) as dialog:
            dialog.reason = "Synthetic open audit, not a real audit"
            dialog.source_reference = "Synthetic support review reference"
        dialog.record.action_confirm()
        hold = policy.hold_ids
        self.assertEqual(len(hold), 1)
        self.assertEqual(hold.reason, "Synthetic open audit, not a real audit")
        self.assertEqual(hold.placed_by_id, self.env.user)
        self.assertTrue(hold.placed_at)
        self.assertEqual(issue.get_retention_status("2034-01-01")["state"], "held")

    def test_manager_releases_hold_through_native_dialog_without_rewriting_it(self):
        issue = self.issue()
        policy = self.obligation(issue)
        policy.action_approve()
        hold = self.env["l10n_ae.edi.retention.hold"].create(
            {
                "retention_id": policy.id,
                "reason": "Synthetic placement review",
                "source_reference": "Synthetic source reference",
            }
        )
        placement = (
            hold.reason,
            hold.source_reference,
            hold.placed_at,
            hold.placed_by_id,
        )
        action = hold.action_open_release()
        model = self.env[action["res_model"]].with_context(**action["context"])
        with Form(model, view=action["view_id"]) as dialog:
            dialog.reason = "Synthetic closed audit, not a real audit"
        dialog.record.action_confirm()
        self.assertEqual(hold.state, "released")
        self.assertEqual(
            hold.release_reason, "Synthetic closed audit, not a real audit"
        )
        self.assertEqual(hold.released_by_id, self.env.user)
        self.assertTrue(hold.released_at)
        self.assertEqual(
            placement,
            (hold.reason, hold.source_reference, hold.placed_at, hold.placed_by_id),
        )
        self.assertTrue(
            issue.get_retention_status("2034-01-01")["disposition_reviewable"]
        )

    def test_transient_dialog_confirmation_rechecks_current_active_reviewer(self):
        policy = self.obligation(self.issue())
        policy.action_approve()
        hold = self.env["l10n_ae.edi.retention.hold"].create(
            {
                "retention_id": policy.id,
                "reason": "Synthetic preserved audit",
                "source_reference": "Synthetic placement source",
            }
        )
        manager = new_test_user(
            self.env,
            login="synthetic-retention-dialog-manager",
            groups="account.group_account_manager",
            company_id=self.env.company.id,
            company_ids=self.env.company.ids,
        )
        placement = (
            self.env["l10n_ae.edi.retention.hold.wizard"]
            .with_user(manager)
            .create(
                {
                    "retention_id": policy.id,
                    "reason": "Synthetic new placement",
                    "source_reference": "Synthetic source",
                }
            )
        )
        release = (
            self.env["l10n_ae.edi.retention.release.wizard"]
            .with_user(manager)
            .create({"hold_id": hold.id, "reason": "Synthetic reviewed release"})
        )
        manager.active = False
        for wizard in (placement, release):
            with self.cr.savepoint(), self.assertRaises(AccessError):
                wizard.sudo().action_confirm()
        self.assertEqual(policy.hold_ids, hold)
        self.assertEqual(hold.state, "active")
        self.assertFalse(hold.released_at)
        self.assertFalse(hold.release_reason)

    def test_longer_tax_purpose_protects_original_after_shorter_expiry(self):
        issue = self.issue()
        original = (issue.payload, issue.idempotency_key, issue.move_id.amount_total)
        vat = self.obligation(issue)
        corporate = self.obligation(issue, 7, "corporate_tax")
        vat.action_approve()
        corporate.action_approve()
        self.assertEqual(vat.retain_until, date(2031, 12, 31))
        self.assertEqual(corporate.retain_until, date(2033, 12, 31))
        self.assertEqual(issue.get_retention_status("2032-01-01")["state"], "retained")
        self.assertFalse(
            issue.get_retention_status("2033-12-31")["disposition_reviewable"]
        )
        self.assertTrue(
            issue.get_retention_status("2034-01-01")["disposition_reviewable"]
        )
        self.assertEqual(
            original, (issue.payload, issue.idempotency_key, issue.move_id.amount_total)
        )

    def test_draft_obligation_blocks_disposition_review(self):
        issue = self.issue()
        approved = self.obligation(issue)
        approved.action_approve()
        self.obligation(issue, 7, "corporate_tax")
        status = issue.get_retention_status("2034-01-01")
        self.assertEqual(status["state"], "review_required")
        self.assertEqual(status["obligation_count"], 2)
        self.assertFalse(status["disposition_reviewable"])

    def test_approved_basis_and_actor_cannot_be_forged_or_rewritten(self):
        issue = self.issue()
        policy = self.obligation(issue)
        for vals in (
            {"state": "approved"},
            {"approved_by_id": self.env.user.id},
            {"approved_at": "2026-01-01 00:00:00"},
        ):
            with self.cr.savepoint(), self.assertRaises(UserError):
                policy.write(vals)
        policy.action_approve()
        actor, when = policy.approved_by_id, policy.approved_at
        self.assertEqual(actor, self.env.user)
        self.assertTrue(when)
        for vals in ({"retention_years": 1}, {"source_reference": "Replacement"}):
            with self.cr.savepoint(), self.assertRaises(UserError):
                policy.write(vals)
        with self.cr.savepoint(), self.assertRaises(UserError):
            policy.unlink()
        self.assertEqual((policy.approved_by_id, policy.approved_at), (actor, when))

    def test_create_context_defaults_cannot_forge_approval(self):
        issue = self.issue()
        policy = self.obligation(
            issue,
            context={
                "default_state": "approved",
                "default_approved_by_id": self.auditor.id,
                "default_approved_at": "2026-01-01 00:00:00",
                "default_retain_until": "2099-12-31",
            },
        )

        self.assertEqual(policy.state, "draft")
        self.assertFalse(policy.approved_by_id)
        self.assertFalse(policy.approved_at)
        self.assertFalse(policy.retain_until)

        policy.action_approve()
        self.assertEqual(policy.state, "approved")
        self.assertEqual(policy.approved_by_id, self.env.user)
        self.assertTrue(policy.approved_at)
        self.assertEqual(policy.retain_until, date(2031, 12, 31))

    def test_calendar_and_period_anchors_require_reviewed_valid_inputs(self):
        issue = self.issue()
        for extra in (
            {"anchor_kind": "calendar_year_end", "anchor_date": "2026-12-30"},
            {"period_start": "2027-01-01"},
            {"retention_years": 0},
            {"source_reference": "   "},
            {"policy_version": ""},
            {"extended_until": "2027-12-31"},
        ):
            with self.subTest(extra=extra), self.cr.savepoint():
                policy = self.obligation(issue, **extra)
                with self.assertRaises(ValidationError):
                    policy.action_approve()
                self.assertEqual(policy.state, "draft")
        leap = self.obligation(
            issue,
            7,
            anchor_kind="source_event",
            anchor_date="2024-02-29",
            period_start=False,
        )
        leap.action_approve()
        self.assertEqual(leap.retain_until, date(2031, 2, 28))

    def test_approval_batch_with_unrepresentable_end_date_is_atomic(self):
        issue = self.issue()
        valid = self.obligation(issue)
        overflow = self.obligation(
            issue,
            anchor_kind="source_event",
            anchor_date="9999-12-31",
            period_start=False,
        )

        with self.assertRaises(ValidationError):
            (valid | overflow).action_approve()

        for policy in (valid, overflow):
            self.assertEqual(policy.state, "draft")
            self.assertFalse(policy.approved_by_id)
            self.assertFalse(policy.approved_at)
            self.assertFalse(policy.retain_until)

    def test_readonly_auditor_cannot_approve_even_under_elevation(self):
        issue = self.issue()
        policy = self.obligation(issue)
        for target in (
            policy.with_user(self.auditor),
            policy.with_user(self.auditor).sudo(),
        ):
            with self.cr.savepoint(), self.assertRaises(AccessError):
                target.action_approve()
        self.assertEqual(policy.state, "draft")
        policy.action_approve()
        status = issue.with_user(self.auditor).get_retention_status("2032-01-01")
        self.assertEqual(status["state"], "reviewable")
        self.assertEqual(policy.with_user(self.auditor).approved_by_id.id, self.env.uid)

    def test_assessment_rejects_obligations_and_holds_hidden_by_global_access(self):
        issue = self.issue()
        policy = self.obligation(issue)
        policy.action_approve()
        hold = self.env["l10n_ae.edi.retention.hold"].create(
            {
                "retention_id": policy.id,
                "reason": "Synthetic unresolved audit",
                "source_reference": "Synthetic audit request",
            }
        )
        model = self.env["ir.model"]

        for access_model, hidden_record in (
            ("l10n_ae.edi.retention", policy),
            ("l10n_ae.edi.retention.hold", hold),
        ):
            restriction = self.env["ir.access"].create(
                {
                    "name": "Synthetic hidden retention assessment row",
                    "model_id": model._get(access_model).id,
                    "operation": "r",
                    "domain": f"[('id', '!=', {hidden_record.id})]",
                }
            )
            for elevated in (False, True):
                target = issue.with_user(self.auditor)
                if elevated:
                    target = target.sudo()
                with self.cr.savepoint(), self.assertRaises(AccessError):
                    target.get_retention_status("2034-01-01")
            restriction.unlink()

    def test_hold_survives_expiry_and_release_retains_original_history(self):
        issue = self.issue()
        policy = self.obligation(issue)
        policy.action_approve()
        hold = self.env["l10n_ae.edi.retention.hold"].create(
            {
                "retention_id": policy.id,
                "reason": "Synthetic unresolved audit",
                "source_reference": "Synthetic audit request",
            }
        )
        self.assertEqual(issue.get_retention_status("2040-01-01")["state"], "held")
        original = (
            hold.reason,
            hold.source_reference,
            hold.placed_by_id,
            hold.placed_at,
        )
        with self.cr.savepoint(), self.assertRaises(AccessError):
            hold.with_user(self.auditor).sudo().action_release("Not authorized")
        hold.action_release("Synthetic audit resolution reviewed")
        self.assertEqual(hold.state, "released")
        self.assertEqual(hold.released_by_id, self.env.user)
        self.assertTrue(hold.released_at)
        self.assertEqual(
            (hold.reason, hold.source_reference, hold.placed_by_id, hold.placed_at),
            original,
        )
        with self.cr.savepoint(), self.assertRaises(UserError):
            hold.write({"release_reason": "Rewritten"})
        with self.cr.savepoint(), self.assertRaises(UserError):
            hold.unlink()
        self.assertTrue(
            issue.get_retention_status("2040-01-01")["disposition_reviewable"]
        )

    def test_hold_create_context_defaults_cannot_forge_release(self):
        issue = self.issue()
        policy = self.obligation(issue)
        policy.action_approve()
        old_time = "2000-01-01 00:00:00"
        hold = (
            self.env["l10n_ae.edi.retention.hold"]
            .with_context(
                default_state="released",
                default_released_by_id=self.auditor.id,
                default_released_at=old_time,
                default_release_reason="Forged release",
            )
            .create(
                {
                    "retention_id": policy.id,
                    "reason": "Synthetic unresolved audit",
                    "source_reference": "Synthetic audit request",
                }
            )
        )

        self.assertEqual(hold.state, "active")
        self.assertFalse(hold.released_by_id)
        self.assertFalse(hold.released_at)
        self.assertFalse(hold.release_reason)
        self.assertEqual(hold.placed_by_id, self.env.user)
        self.assertTrue(hold.placed_at)
        self.assertNotEqual(str(hold.placed_at), old_time)
        self.assertEqual(issue.get_retention_status("2034-01-01")["state"], "held")

        hold.action_release("Synthetic audit resolution reviewed")
        self.assertEqual(hold.state, "released")
        self.assertEqual(hold.released_by_id, self.env.user)
        self.assertTrue(hold.released_at)
        self.assertNotEqual(str(hold.released_at), old_time)
        self.assertEqual(hold.release_reason, "Synthetic audit resolution reviewed")

    def test_company_manager_cannot_sudo_delete_foreign_draft_obligation(self):
        company_a = self.env.company
        company_b_data = self.setup_other_company(
            name="Synthetic foreign retention company"
        )
        company_b = company_b_data["company"]
        company_b.write({"l10n_ae_tin": "1000000011", "l10n_ae_tin_confirmed": True})
        company_b.partner_id.state_id = self.env.ref("base.state_ae_du")
        if "routing_scheme" in company_b.partner_id._fields:
            company_b.partner_id.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000011"}
            )
        binding_b = (
            self.env["l10n_ae.edi.binding"]
            .with_company(company_b)
            .create({"name": "Synthetic foreign retention ASP"})
        )
        contract_reviewer_b = new_test_user(
            self.env,
            login="synthetic-retention-company-b-contract-reviewer",
            groups="account.group_account_manager",
            company_id=company_b.id,
            company_ids=company_b.ids,
        )
        review_controlled_binding(
            binding_b,
            contract_reviewer_b,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic foreign issuer contract; company A remains isolated.",
        )
        invoice_b = self.init_invoice(
            "out_invoice",
            amounts=[100],
            taxes=company_b.account_sale_tax_id,
            company=company_b,
            journal=company_b_data["default_journal_sale"],
            post=True,
        )
        invoice_b.l10n_ae_binding_id = binding_b
        invoice_b.action_l10n_ae_prepare()
        foreign_draft = self.obligation(invoice_b.l10n_ae_transmission_id)
        manager_a = new_test_user(
            self.env,
            login="synthetic-retention-company-a-manager",
            groups="account.group_account_manager",
            company_id=company_a.id,
            company_ids=company_a.ids,
        )

        for elevated in (False, True):
            target = foreign_draft.with_user(manager_a).with_context(
                allowed_company_ids=company_a.ids
            )
            if elevated:
                target = target.sudo()
            with self.cr.savepoint(), self.assertRaises(AccessError):
                target.unlink()
            self.assertTrue(foreign_draft.exists())

        own_draft = self.obligation(self.issue())
        own_draft.with_user(manager_a).with_context(
            allowed_company_ids=company_a.ids
        ).unlink()
        self.assertFalse(own_draft.exists())

        foreign_draft.action_approve()
        foreign_hold = self.env["l10n_ae.edi.retention.hold"].create(
            {
                "retention_id": foreign_draft.id,
                "reason": "Synthetic foreign audit",
                "source_reference": "Synthetic foreign source",
            }
        )
        # Crafted readonly dialog references must still use the final native
        # source/company authority, even when the caller elevates the wizard.
        model = self.env["l10n_ae.edi.retention.hold.wizard"].with_user(manager_a)
        placement = model.create(
            {
                "retention_id": foreign_draft.id,
                "reason": "Unauthorized foreign placement",
                "source_reference": "Synthetic crafted reference",
            }
        )
        release = (
            self.env["l10n_ae.edi.retention.release.wizard"]
            .with_user(manager_a)
            .create(
                {"hold_id": foreign_hold.id, "reason": "Unauthorized foreign release"}
            )
        )
        for wizard in (placement, release):
            with self.cr.savepoint(), self.assertRaises(AccessError):
                wizard.sudo().action_confirm()
        self.assertEqual(foreign_draft.hold_ids, foreign_hold)
        self.assertEqual(foreign_hold.state, "active")
        self.assertFalse(foreign_hold.released_at)
