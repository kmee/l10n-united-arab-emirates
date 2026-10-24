# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import base64
import hashlib
import io
import json
import zipfile

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_ae_edi.tests.common import review_controlled_binding


@tagged("post_install", "-at_install")
class TestUaeReceivedRetentionExportAccess(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.binding = cls.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic retention export access"}
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

    def test_received_export_refuses_hidden_retention_rows_even_under_sudo(self):
        manager = self.simple_accountman
        review_controlled_binding(
            self.binding,
            manager,
            capabilities=("receive",),
            note="Synthetic receive-only contract for retention export access.",
        )
        content = b"<SyntheticReceivedRetention>preserved</SyntheticReceivedRetention>"
        inbox = (
            self.env["l10n_ae.edi.inbox"]
            .with_user(manager)
            .receive_controlled_document(
                self.binding.id, "synthetic-retention-export-access", content
            )
        )
        original = (
            inbox.original_xml.content,
            inbox.xml_hash,
            inbox.state,
            inbox.move_id.id,
        )
        obligation = (
            self.env["l10n_ae.edi.retention"]
            .with_user(manager)
            .create(
                {
                    "name": "Synthetic export access obligation",
                    "inbox_id": inbox.id,
                    "record_class": "evidence",
                    "tax_purpose": "vat",
                    "anchor_kind": "tax_period_end",
                    "period_start": "2026-01-01",
                    "anchor_date": "2026-12-31",
                    "retention_years": 5,
                    "source_reference": "Synthetic reviewed source; not ratified",
                    "policy_version": "synthetic-export-access-1",
                    "review_note": "Access-control fixture only",
                }
            )
        )
        obligation.action_approve()
        hold = (
            self.env["l10n_ae.edi.retention.hold"]
            .with_user(manager)
            .create(
                {
                    "retention_id": obligation.id,
                    "reason": "Synthetic unresolved review",
                    "source_reference": "Synthetic export access test",
                }
            )
        )
        auditor = new_test_user(
            self.env,
            login="synthetic-retention-export-auditor",
            groups=(
                "base.group_user,base.group_allow_export,"
                "l10n_ae_edi.group_fiscal_auditor"
            ),
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )

        def public_package():
            # Each call models a separate native read request with a fresh cache.
            self.env.invalidate_all()
            exported = inbox.with_user(auditor).read(["evidence_archive"])[0][
                "evidence_archive"
            ]
            return base64.b64decode(exported["content"])

        def inspect_package(package):
            with zipfile.ZipFile(io.BytesIO(package)) as archive:
                appendix_bytes = archive.read("retention.json")
                appendix = json.loads(appendix_bytes)
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(archive.read("document.xml"), content)
                self.assertEqual(
                    manifest["artifacts"]["retention.json"],
                    {
                        "sha256": hashlib.sha256(appendix_bytes).hexdigest(),
                        "size": len(appendix_bytes),
                    },
                )
                self.assertEqual(
                    appendix["source"],
                    {
                        "model": "l10n_ae.edi.inbox",
                        "id": inbox.id,
                        "company_id": self.env.company.id,
                    },
                )
                self.assertEqual(
                    manifest["relationships"]["retention_obligations"],
                    [obligation.id],
                )
                self.assertEqual(
                    manifest["relationships"]["retention_holds"], [hold.id]
                )
                self.assertEqual(
                    [row["id"] for row in appendix["obligations"]],
                    [obligation.id],
                )
                self.assertEqual([row["id"] for row in appendix["holds"]], [hold.id])
                self.assertEqual(appendix["holds"][0]["state"], "active")

        package = public_package()
        inspect_package(package)

        ir_access = self.env["ir.access"]
        for model_name, hidden_record in (
            ("l10n_ae.edi.retention", obligation),
            ("l10n_ae.edi.retention.hold", hold),
        ):
            restriction = ir_access.create(
                {
                    "name": f"Synthetic hide retention row {hidden_record.id}",
                    "model_id": self.env["ir.model"]._get(model_name).id,
                    "operation": "r",
                    "domain": f"[('id', '!=', {hidden_record.id})]",
                }
            )
            try:
                # Invalidate cached Binary values after changing native access rules.
                self.env.invalidate_all()
                with self.assertRaises(AccessError):
                    inbox.with_user(auditor).read(["evidence_archive"])
                self.env.invalidate_all()
                with self.assertRaises(AccessError):
                    inbox.with_user(auditor).sudo().read(["evidence_archive"])
            finally:
                restriction.unlink()
                self.env.invalidate_all()

        self.assertEqual(public_package(), package)
        inspect_package(public_package())
        self.assertEqual(
            (
                inbox.original_xml.content,
                inbox.xml_hash,
                inbox.state,
                inbox.move_id.id,
            ),
            original,
        )
