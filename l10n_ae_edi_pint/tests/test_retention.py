# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
from datetime import date

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestUaeReceivedRetention(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.binding = cls.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic received retention"}
        )
        cls.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        cls.env.company.partner_id.write(
            {"routing_scheme": "0235", "routing_endpoint": "1000000001"}
        )
        cls.partner_a.write(
            {
                "country_id": cls.env.ref("base.ae").id,
                "routing_scheme": "0235",
                "routing_endpoint": "1000000002",
            }
        )

    def test_source_defaults_cannot_attach_a_second_original(self):
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            self.binding.id, "source-defaults", b"<synthetic-retention/>"
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        model = self.env["l10n_ae.edi.retention"]
        received = model.with_context(default_transmission_id=issue.id).create(
            {"name": "Synthetic explicit received source", "inbox_id": inbox.id}
        )
        self.assertEqual(received.inbox_id, inbox)
        self.assertFalse(received.transmission_id)
        issued = model.with_context(default_inbox_id=inbox.id).create(
            {"name": "Synthetic explicit issued source", "transmission_id": issue.id}
        )
        self.assertEqual(issued.transmission_id, issue)
        self.assertFalse(issued.inbox_id)
        for values in ({}, {"transmission_id": issue.id, "inbox_id": inbox.id}):
            with self.cr.savepoint(), self.assertRaises(ValidationError):
                model.create({"name": "Synthetic invalid source choice", **values})

    def test_reviewed_obligation_preserves_received_original_without_accounting(self):
        content = (
            b'<!DOCTYPE Invoice [<!ENTITY ex SYSTEM "file:///etc/passwd">]>'
            b"<Invoice>&ex;</Invoice>"
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(self.binding.id, "unsafe-retention", content)
        )
        self.assertFalse(inbox.action_process())
        before = (
            inbox.original_xml.content,
            inbox.xml_hash,
            inbox.state,
            inbox.move_id.id,
        )
        self.assertEqual(
            before, (content, hashlib.sha256(content).hexdigest(), "review", False)
        )
        self.assertEqual(
            inbox.get_retention_status("2030-01-01")["state"], "unclassified"
        )
        obligation = self.env["l10n_ae.edi.retention"].create(
            {
                "name": "Synthetic received original obligation",
                "inbox_id": inbox.id,
                "record_class": "evidence",
                "tax_purpose": "vat",
                "anchor_kind": "tax_period_end",
                "period_start": "2026-01-01",
                "anchor_date": "2026-12-31",
                "retention_years": 5,
                "source_reference": "Synthetic reference; not ratified",
                "policy_version": "synthetic-2026-1",
                "review_note": "Independent fixture interval; no production policy.",
            }
        )
        obligation.action_approve()
        self.assertEqual(obligation.approved_by_id, self.env.user)
        self.assertEqual(obligation.retain_until, date(2031, 12, 31))
        self.assertEqual(inbox.get_retention_status("2030-01-01")["state"], "retained")
        self.assertEqual(
            (
                inbox.original_xml.content,
                inbox.xml_hash,
                inbox.state,
                inbox.move_id.id,
            ),
            before,
        )

    def test_saved_default_cannot_link_received_original_to_an_issued_policy(self):
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            self.binding.id, "saved-source-default", b"<synthetic-retention/>"
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        self.env["ir.default"].set(
            "l10n_ae.edi.retention", "inbox_id", inbox.id, user_id=True
        )
        policy = self.env["l10n_ae.edi.retention"].create(
            {"name": "Synthetic issued explicit source", "transmission_id": issue.id}
        )
        self.assertEqual(policy.transmission_id, issue)
        self.assertFalse(policy.inbox_id)

    def test_foreign_received_original_requires_current_company_even_when_elevated(
        self,
    ):
        company_a = self.env.company
        company_b = self.setup_other_company(name="Synthetic foreign archive")[
            "company"
        ]
        binding_b = (
            self.env["l10n_ae.edi.binding"]
            .with_company(company_b)
            .create({"name": "Synthetic foreign retention source"})
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_company(company_b)
            .receive_controlled_document(
                binding_b.id, "foreign-retention-source", b"<synthetic-retention/>"
            )
        )
        manager = new_test_user(
            self.env,
            login="synthetic-received-retention-company-a",
            groups="account.group_account_manager",
            company_id=company_a.id,
            company_ids=company_a.ids,
        )
        values = {
            "name": "Synthetic foreign received obligation",
            "inbox_id": inbox.id,
            "record_class": "evidence",
            "tax_purpose": "vat",
            "anchor_kind": "source_event",
            "anchor_date": "2026-12-31",
            "retention_years": 5,
            "source_reference": "Synthetic source; not ratified",
            "policy_version": "synthetic-2026-1",
            "review_note": "Synthetic engineering interval only.",
        }
        target_model = (
            self.env["l10n_ae.edi.retention"]
            .with_user(manager)
            .with_context(allowed_company_ids=company_a.ids)
            .sudo()
        )
        with self.cr.savepoint(), self.assertRaises(AccessError):
            target_model.create(values)
        policy = self.env["l10n_ae.edi.retention"].create(values)
        target = (
            policy.with_user(manager)
            .with_context(allowed_company_ids=company_a.ids)
            .sudo()
        )
        with self.cr.savepoint(), self.assertRaises(AccessError):
            target.action_approve()
        self.assertEqual(policy.state, "draft")
        self.assertFalse(policy.approved_by_id)
        policy.action_approve()
        hold = self.env["l10n_ae.edi.retention.hold"].create(
            {
                "retention_id": policy.id,
                "reason": "Synthetic audit",
                "source_reference": "Synthetic hold source",
            }
        )
        with self.cr.savepoint(), self.assertRaises(AccessError):
            hold.with_user(manager).with_context(
                allowed_company_ids=company_a.ids
            ).sudo().action_release("Unauthorized review")
        self.assertEqual(hold.state, "active")
        self.assertFalse(hold.released_by_id)
        with self.cr.savepoint(), self.assertRaises(AccessError):
            inbox.with_user(manager).with_context(
                allowed_company_ids=company_a.ids
            ).sudo().get_retention_status("2034-01-01")
        self.assertFalse(inbox.move_id)

    def test_hold_survives_received_original_expiry_and_release_preserves_history(
        self,
    ):
        content = (
            b'<!DOCTYPE Invoice [<!ENTITY ex SYSTEM "file:///etc/passwd">]>'
            b"<Invoice>&ex;</Invoice>"
        )
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(self.simple_accountman)
            .receive_controlled_document(
                self.binding.id, "held-unsafe-retention", content
            )
        )
        self.assertFalse(inbox.action_process())
        original = (
            inbox.original_xml.content,
            inbox.xml_hash,
            inbox.state,
            inbox.move_id.id,
        )
        self.assertEqual(
            original,
            (content, hashlib.sha256(content).hexdigest(), "review", False),
        )

        obligation = self.env["l10n_ae.edi.retention"].create(
            {
                "name": "Synthetic received original hold obligation",
                "inbox_id": inbox.id,
                "record_class": "evidence",
                "tax_purpose": "vat",
                "anchor_kind": "tax_period_end",
                "period_start": "2026-01-01",
                "anchor_date": "2026-12-31",
                "retention_years": 5,
                "source_reference": "Synthetic reference; not ratified",
                "policy_version": "synthetic-2026-1",
                "review_note": "Independent fixture interval; no production policy.",
            }
        )
        obligation.action_approve()
        self.assertEqual(obligation.retain_until, date(2031, 12, 31))

        hold = self.env["l10n_ae.edi.retention.hold"].create(
            {
                "retention_id": obligation.id,
                "reason": "Synthetic unresolved audit",
                "source_reference": "Synthetic audit request",
            }
        )
        placement = (
            hold.reason,
            hold.source_reference,
            hold.placed_by_id,
            hold.placed_at,
        )
        self.assertEqual(inbox.get_retention_status("2032-01-01")["state"], "held")

        hold.action_release("Synthetic audit resolution reviewed")
        self.assertEqual(hold.state, "released")
        self.assertEqual(hold.released_by_id, self.env.user)
        self.assertTrue(hold.released_at)
        self.assertEqual(hold.release_reason, "Synthetic audit resolution reviewed")
        self.assertEqual(
            (hold.reason, hold.source_reference, hold.placed_by_id, hold.placed_at),
            placement,
        )
        status = inbox.get_retention_status("2032-01-01")
        self.assertEqual(status["state"], "reviewable")
        self.assertTrue(status["disposition_reviewable"])
        self.assertEqual(
            (
                inbox.original_xml.content,
                inbox.xml_hash,
                inbox.state,
                inbox.move_id.id,
            ),
            original,
        )
