# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import io
import json
import zipfile
from datetime import date

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_ae_edi.tests.common import (
    CONTROLLED_CAPABILITIES,
    review_controlled_binding,
)


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
            {
                "routing_scheme": "0235",
                "routing_endpoint": "1000000001",
                "state_id": cls.env.ref("base.state_ae_du").id,
            }
        )
        cls.partner_a.write(
            {
                "country_id": cls.env.ref("base.ae").id,
                "state_id": cls.env.ref("base.state_ae_du").id,
                "routing_scheme": "0235",
                "routing_endpoint": "1000000002",
            }
        )

    def test_family_package_refreshes_received_retention_and_hold_changes(self):
        receiver = self.env["l10n_ae.edi.inbox"]
        first = receiver.receive_controlled_document(
            self.binding.id, "retention-family-cache", b"<first/>"
        )
        selected = receiver.receive_controlled_document(
            self.binding.id, "retention-family-cache", b"<second/>"
        )
        selected.action_select_identity_variant(
            "Compared both synthetic originals.", "SYNTHETIC-RETENTION-FAMILY-001"
        )

        def sibling_retention():
            with (
                zipfile.ZipFile(io.BytesIO(first.evidence_archive.content)) as archive,
                zipfile.ZipFile(
                    io.BytesIO(archive.read(f"identity-originals/{selected.id}.zip"))
                ) as nested,
            ):
                return json.loads(nested.read("retention.json"))

        self.assertEqual(sibling_retention()["obligations"], [])
        policy = self.env["l10n_ae.edi.retention"].create(
            {
                "name": "Synthetic sibling retention",
                "inbox_id": selected.id,
                "record_class": "evidence",
                "tax_purpose": "vat",
                "anchor_kind": "tax_period_end",
                "period_start": "2026-01-01",
                "anchor_date": "2026-12-31",
                "retention_years": 5,
                "source_reference": "Synthetic reviewed source, not ratified",
                "policy_version": "synthetic-family-1",
                "review_note": "Illustrative interval",
            }
        )
        self.assertEqual(sibling_retention()["obligations"][0]["state"], "draft")
        policy.write({"review_note": "Updated synthetic review note"})
        self.assertEqual(
            sibling_retention()["obligations"][0]["review_note"],
            "Updated synthetic review note",
        )
        policy.action_approve()
        retained = sibling_retention()["obligations"][0]
        self.assertEqual(retained["state"], "approved")
        self.assertEqual(retained["retain_until"], "2031-12-31")
        hold = self.env["l10n_ae.edi.retention.hold"].create(
            {
                "retention_id": policy.id,
                "reason": "Synthetic family audit",
                "source_reference": "Synthetic family audit reference",
            }
        )
        self.assertEqual(sibling_retention()["holds"][0]["state"], "active")
        hold.action_release("Synthetic family resolution")
        self.assertEqual(sibling_retention()["holds"][0]["state"], "released")
        self.assertEqual(
            sibling_retention()["holds"][0]["release_reason"],
            "Synthetic family resolution",
        )
        draft = self.env["l10n_ae.edi.retention"].create(
            {
                "name": "Synthetic removable draft",
                "inbox_id": selected.id,
                "record_class": "evidence",
                "tax_purpose": "vat",
                "anchor_kind": "tax_period_end",
                "period_start": "2026-01-01",
                "anchor_date": "2026-12-31",
                "retention_years": 5,
                "source_reference": "Synthetic source",
                "policy_version": "synthetic-family-1",
                "review_note": "Illustrative draft",
            }
        )
        self.assertEqual(len(sibling_retention()["obligations"]), 2)
        draft.unlink()
        self.assertEqual(len(sibling_retention()["obligations"]), 1)
        self.assertEqual(sibling_retention()["obligations"][0]["id"], policy.id)
        self.assertFalse((first | selected).move_id)

    def test_received_package_preserves_reviewed_retention_and_hold_history(self):
        content = b'<!DOCTYPE Invoice SYSTEM "file:///etc/passwd"><Invoice/>'
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            self.binding.id, "synthetic-received-retention-package", content
        )
        self.assertFalse(inbox.action_process())
        original = (
            inbox.original_xml.content,
            inbox.xml_hash,
            inbox.state,
            inbox.move_id.id,
        )

        def appendix():
            with zipfile.ZipFile(io.BytesIO(inbox.evidence_archive.content)) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                raw = archive.read("retention.json")
                self.assertEqual(archive.read("document.xml"), content)
                self.assertEqual(
                    manifest["artifacts"]["retention.json"],
                    {
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "size": len(raw),
                    },
                )
                return json.loads(raw), manifest

        empty, _manifest = appendix()
        self.assertEqual(
            empty,
            {
                "format": "kmee-uae-retention-1",
                "source": {
                    "model": "l10n_ae.edi.inbox",
                    "id": inbox.id,
                    "company_id": self.env.company.id,
                },
                "obligations": [],
                "holds": [],
            },
        )
        policies = self.env["l10n_ae.edi.retention"]
        for purpose, years in (("vat", 5), ("corporate_tax", 7)):
            policies |= self.env["l10n_ae.edi.retention"].create(
                {
                    "name": "Synthetic portable reviewed basis",
                    "inbox_id": inbox.id,
                    "record_class": "evidence",
                    "tax_purpose": purpose,
                    "anchor_kind": "tax_period_end",
                    "period_start": "2026-01-01",
                    "anchor_date": "2026-12-31",
                    "retention_years": years,
                    "source_reference": "Synthetic reviewed source; not ratified",
                    "policy_version": "synthetic-appendix-1",
                    "review_note": "Independent fixture interval",
                }
            )
        policies.action_approve()
        hold = self.env["l10n_ae.edi.retention.hold"].create(
            {
                "retention_id": policies[0].id,
                "reason": "Synthetic unresolved audit",
                "source_reference": "Synthetic audit reference",
            }
        )
        active, manifest = appendix()
        self.assertEqual(
            manifest["relationships"]["retention_obligations"], policies.ids
        )
        self.assertEqual(manifest["relationships"]["retention_holds"], hold.ids)
        self.assertEqual(
            [row["retain_until"] for row in active["obligations"]],
            ["2031-12-31", "2033-12-31"],
        )
        self.assertEqual(
            [row["approved_by_id"] for row in active["obligations"]],
            [self.env.uid, self.env.uid],
        )
        self.assertEqual(
            [row["policy_version"] for row in active["obligations"]],
            ["synthetic-appendix-1", "synthetic-appendix-1"],
        )
        placement = active["holds"][0]
        self.assertEqual(placement["state"], "active")
        self.assertEqual(placement["placed_by_id"], self.env.uid)
        self.assertTrue(placement["placed_at"])
        self.assertFalse(placement["released_at"])
        hold.action_release("Synthetic reviewed resolution")
        released, _manifest = appendix()
        for field in ("reason", "source_reference", "placed_by_id", "placed_at"):
            self.assertEqual(released["holds"][0][field], placement[field])
        self.assertEqual(released["holds"][0]["state"], "released")
        self.assertEqual(released["holds"][0]["released_by_id"], self.env.uid)
        self.assertTrue(released["holds"][0]["released_at"])
        self.assertEqual(
            released["holds"][0]["release_reason"], "Synthetic reviewed resolution"
        )
        saved = inbox.evidence_archive.content
        auditor = new_test_user(
            self.env,
            login="synthetic-portable-retention-auditor",
            groups="base.group_user,base.group_allow_export,l10n_ae_edi.group_fiscal_auditor",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        self.assertEqual(inbox.with_user(auditor).evidence_archive.content, saved)
        self.assertEqual(
            (inbox.original_xml.content, inbox.xml_hash, inbox.state, inbox.move_id.id),
            original,
        )

    def test_issued_package_keeps_original_bytes_when_retention_is_reviewed(self):
        review_controlled_binding(
            self.binding,
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note=(
                "Synthetic controlled invoice contract for test_issued_package_keeps_"
                "original_bytes_when_retention_is_reviewed."
            ),
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        invoice.l10n_ae_binding_id = self.binding
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        with zipfile.ZipFile(io.BytesIO(issue.evidence_archive.content)) as archive:
            original_payload = archive.read("snapshot.json")
            original_xml = archive.read("document.xml")
            self.assertEqual(
                json.loads(archive.read("retention.json"))["obligations"], []
            )
        policy = self.env["l10n_ae.edi.retention"].create(
            {
                "name": "Synthetic issued portable basis",
                "transmission_id": issue.id,
                "record_class": "invoice",
                "tax_purpose": "vat",
                "anchor_kind": "tax_period_end",
                "period_start": "2026-01-01",
                "anchor_date": "2026-12-31",
                "retention_years": 5,
                "source_reference": "Synthetic reviewed source; not ratified",
                "policy_version": "synthetic-issued-appendix-1",
                "review_note": "Independent fixture interval",
            }
        )
        with zipfile.ZipFile(io.BytesIO(issue.evidence_archive.content)) as archive:
            draft = json.loads(archive.read("retention.json"))["obligations"][0]
            self.assertEqual(draft["state"], "draft")
            self.assertFalse(draft["approved_by_id"])
            self.assertFalse(draft["retain_until"])
        policy.action_approve()
        with zipfile.ZipFile(io.BytesIO(issue.evidence_archive.content)) as archive:
            raw = archive.read("retention.json")
            manifest = json.loads(archive.read("manifest.json"))
            appendix = json.loads(raw)
            self.assertEqual(
                appendix["source"],
                {
                    "model": "l10n_ae.edi.transmission",
                    "id": issue.id,
                    "company_id": self.env.company.id,
                },
            )
            self.assertEqual(
                manifest["relationships"]["retention_obligations"], policy.ids
            )
            self.assertEqual(manifest["relationships"]["retention_holds"], [])
            self.assertEqual(appendix["holds"], [])
            self.assertEqual(appendix["obligations"][0]["retain_until"], "2031-12-31")
            self.assertEqual(appendix["obligations"][0]["approved_by_id"], self.env.uid)
            self.assertEqual(
                manifest["artifacts"]["retention.json"],
                {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size": len(raw),
                },
            )
            self.assertEqual(archive.read("snapshot.json"), original_payload)
            self.assertEqual(archive.read("document.xml"), original_xml)
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.amount_total, 105)
        self.assertFalse(issue.attempt_ids)

    def test_manager_classifies_received_original_through_native_form(self):
        content = b'<!DOCTYPE Invoice SYSTEM "file:///etc/passwd"><Invoice/>'
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            self.binding.id, "synthetic-received-retention-form", content
        )
        self.assertFalse(inbox.action_process())
        before = (
            inbox.original_xml.content,
            inbox.xml_hash,
            inbox.state,
            inbox.move_id.id,
        )
        with Form(
            self.env["l10n_ae.edi.retention"],
            view="l10n_ae_edi.edi_retention_form",
        ) as form:
            form.name = "Synthetic received native form review"
            form.inbox_id = inbox
            form.record_class = "evidence"
            form.tax_purpose = "vat"
            form.anchor_kind = "tax_period_end"
            form.period_start = date(2026, 1, 1)
            form.anchor_date = date(2026, 12, 31)
            form.retention_years = 5
            form.source_reference = "Synthetic reviewed reference"
            form.policy_version = "synthetic-ui-1"
            form.review_note = "Illustrative interval; no content approval"
        policy = form.record
        self.assertEqual(policy.inbox_id, inbox)
        self.assertFalse(policy.transmission_id)
        policy.action_approve()
        self.assertEqual(policy.retain_until, date(2031, 12, 31))
        self.assertEqual(
            before,
            (inbox.original_xml.content, inbox.xml_hash, inbox.state, inbox.move_id.id),
        )

    def test_source_defaults_cannot_attach_a_second_original(self):
        inbox = self.env["l10n_ae.edi.inbox"].receive_controlled_document(
            self.binding.id, "source-defaults", b"<synthetic-retention/>"
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        )
        review_controlled_binding(
            self.binding,
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note=(
                "Synthetic controlled invoice contract for "
                "test_source_defaults_cannot_attach_a_second_original."
            ),
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
        review_controlled_binding(
            self.binding,
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note=(
                "Synthetic controlled invoice contract for "
                "test_saved_default_cannot_link_received_original_to_an_issued_policy."
            ),
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
