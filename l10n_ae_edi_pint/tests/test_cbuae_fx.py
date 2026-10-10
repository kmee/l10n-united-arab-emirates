# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json
from functools import partial
from pathlib import Path
from unittest.mock import patch

from requests import Response

from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.l10n_ae_edi.tests.common import (
    CONTROLLED_CAPABILITIES,
    review_controlled_binding,
)

from .common import UaePintTestCommon


@tagged("post_install", "-at_install")
class TestUaeCbuaeFx(UaePintTestCommon):
    def test_renamed_imported_currency_requires_review_and_keeps_frozen_invoice(self):
        usd, _anchor, review = self._reviewed_cbuae_usd()
        company = self.env.company
        company.l10n_ae_edi_auto_prepare = False
        binding = review_controlled_binding(
            self.env["l10n_ae.edi.binding"].create(
                {
                    "name": "Synthetic imported currency identity",
                    "fiscal_uuid_policy": "controlled_local",
                }
            ),
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic reviewed controlled contract fixture.",
        )
        original = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd
        ).with_user(self.simple_accountman)
        with Form(original) as form:
            form.invoice_date = "2026-08-27"
            form.l10n_ae_binding_id = binding
        original.action_post()
        original.action_l10n_ae_prepare()
        issue = original.l10n_ae_transmission_id
        immutable = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
            "write_uid",
            "write_date",
        ]
        frozen = issue.read(immutable)
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "write_uid",
            "write_date",
        ]
        original.line_ids.flush_recordset()
        ledger = original.line_ids.sorted("id").read(ledger_fields)
        retained = review.read(
            ["quote_literal", "import_facts", "reviewed_at", "write_uid", "write_date"]
        )
        # Administrative master change, in this disposable fixture only. ZAR is a
        # valid invoice currency code: schema validity must not hide the fact that
        # the imported source was reviewed for USD on this native currency ID.
        zar = self.env.ref("base.ZAR")
        zar.write({"name": "ZZX"})
        # Release the unique code before reusing it: deferred writes to two
        # records do not promise the administrative edit order at flush_all.
        zar.flush_recordset(["name"])
        usd.write({"name": "ZAR"})
        # Complete the native master edit before measuring any UAE replay.
        self.env.flush_all()
        after_rename = original.line_ids.sorted("id").read(ledger_fields)
        self.assertEqual(after_rename, ledger)
        self.assertEqual(issue.read(immutable), frozen)
        company.l10n_ae_edi_auto_prepare = True
        following = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd
        ).with_user(self.simple_accountman)
        self.assertFalse(following.env.su)
        with Form(following) as form:
            form.invoice_date = "2026-08-27"
            form.l10n_ae_binding_id = binding
        # The pinned native Form changes the first invoice's line write_uid
        # while saving the second invoice, before either UAE refusal or replay.
        # Preserve that observed boundary instead of attributing it to replay.
        after_form = original.line_ids.sorted("id").read(ledger_fields)
        for before, after in zip(ledger, after_form, strict=True):
            self.assertEqual(
                {key: value for key, value in before.items() if key != "write_uid"},
                {key: value for key, value in after.items() if key != "write_uid"},
            )
        self.assertEqual(following.currency_id.name, "ZAR")
        self.assertEqual(following.l10n_ae_effective_tax_exchange_origin, "review")
        self.assertIn(
            "currency code",
            following.l10n_ae_effective_tax_exchange_diagnostic,
        )
        self.assertFalse(following.l10n_ae_effective_cbuae_review_id)
        following.action_post()
        self.assertEqual(following.state, "posted")
        self.assertEqual(following.amount_tax_signed, 18.36)
        self.assertEqual(following.amount_total_signed, 385.61)
        self.assertEqual(following.l10n_ae_preparation_state, "review")
        self.assertFalse(following.l10n_ae_transmission_id)
        self.assertFalse(following.l10n_ae_fiscal_uuid)
        following.line_ids.flush_recordset()
        following_ledger = following.line_ids.sorted("id").read(ledger_fields)
        for action in (
            following.action_l10n_ae_validate,
            following.action_l10n_ae_prepare,
        ):
            with self.assertRaisesRegex(UserError, "currency code"):
                action()
        self.assertEqual(
            following.line_ids.sorted("id").read(ledger_fields), following_ledger
        )
        self.assertEqual(
            review.read(
                [
                    "quote_literal",
                    "import_facts",
                    "reviewed_at",
                    "write_uid",
                    "write_date",
                ]
            ),
            retained,
        )
        original.action_l10n_ae_prepare()
        self.assertTrue(original.validate_l10n_ae_pint()["valid"])
        self.assertEqual(original.preview_l10n_ae_pint(), frozen[0]["xml_content"])
        self.assertEqual(issue.read(immutable), frozen)
        self.assertEqual(original.line_ids.sorted("id").read(ledger_fields), after_form)
        self.assertEqual(
            json.loads(issue.payload)["tax_exchange"]["source_currency"], "USD"
        )
        self.assertEqual(review.import_facts["currency_code"], "USD")

    def test_branch_invoice_reader_retains_imported_root_source_without_mutation(self):
        usd, _anchor, review = self._reviewed_cbuae_usd()
        root = self.env.company
        branch = self.setup_other_company(
            name="Synthetic CBUAE accounting branch", parent_id=root.id
        )["company"]
        branch.write(
            {
                "l10n_ae_tin": root.l10n_ae_tin,
                "l10n_ae_tin_confirmed": True,
                "l10n_ae_edi_auto_prepare": False,
            }
        )
        branch.partner_id.write(
            {
                name: root.partner_id[name].id
                if root.partner_id._fields[name].type == "many2one"
                else root.partner_id[name]
                for name in [
                    "vat",
                    "street",
                    "city",
                    "state_id",
                    "routing_scheme",
                    "routing_endpoint",
                    "l10n_ae_registration_type",
                    "l10n_ae_registration_authority",
                    "additional_identifiers",
                ]
            }
        )
        operator = new_test_user(
            self.env,
            login="synthetic-cbuae-branch-invoice-writer",
            groups="base.group_user,account.group_account_invoice",
            company_id=branch.id,
            company_ids=branch.ids,
        )
        self.assertNotIn(root, operator.company_ids)
        binding = (
            self.env["l10n_ae.edi.binding"]
            .with_company(branch)
            .create(
                {
                    "name": "Synthetic branch CBUAE source",
                    "company_id": branch.id,
                    "fiscal_uuid_policy": "controlled_local",
                }
            )
        )
        contract_reviewer = new_test_user(
            self.env,
            login="synthetic-cbuae-branch-contract-reviewer",
            groups="base.group_user,account.group_account_manager",
            company_id=branch.id,
            company_ids=branch.ids,
        )
        review_controlled_binding(
            binding,
            contract_reviewer,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic branch contract; source reader has no review authority.",
        )
        invoice = (
            self.init_invoice(
                "out_invoice",
                amounts=[100],
                taxes=self.tax_sale_a,
                company=branch,
                currency=usd,
                invoice_date="2026-08-27",
            )
            .with_user(operator)
            .with_context(allowed_company_ids=branch.ids)
        )
        self.assertFalse(invoice.env.su)
        self.assertEqual(invoice.env.companies, branch)
        self.assertEqual(invoice.company_id, branch)
        with Form(invoice) as form:
            form.l10n_ae_binding_id = binding
        invoice.action_post()
        self.assertEqual(invoice.amount_total_signed, 385.61)
        self.assertEqual(invoice.amount_tax_signed, 18.36)
        invoice.line_ids.flush_recordset()
        fields = [
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "account_id",
            "tax_ids",
            "write_uid",
            "write_date",
        ]
        ledger = invoice.line_ids.sorted("id").read(fields)
        invoice.action_l10n_ae_validate()
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        evidence = json.loads(issue.payload)["tax_exchange"]["evidence"]
        self.assertEqual(evidence["cbuae_provenance"]["review_id"], review.id)
        self.assertEqual(
            evidence["cbuae_provenance"]["native_rate_id"], review.native_rate_id.id
        )
        self.assertFalse(evidence["cbuae_origin_verified"])
        self.assertEqual(invoice.l10n_ae_effective_cbuae_review_id, review)
        selected = invoice.l10n_ae_effective_cbuae_review_id
        self.assertEqual(selected.read(["quote_literal"])[0]["quote_literal"], "3.6725")
        publication = selected.publication_id
        self.assertEqual(
            publication.source_body.content, review.publication_id.source_body.content
        )
        self.assertEqual(
            publication.read(["source_sha256"])[0]["source_sha256"],
            evidence["cbuae_provenance"]["source"]["sha256"],
        )
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        frozen = issue.read(
            ["payload", "xml_content", "payload_hash", "xml_hash", "idempotency_key"]
        )
        invoice.action_l10n_ae_prepare()
        self.assertEqual(
            issue.read(
                [
                    "payload",
                    "xml_content",
                    "payload_hash",
                    "xml_hash",
                    "idempotency_key",
                ]
            ),
            frozen,
        )
        self.assertEqual(invoice.line_ids.sorted("id").read(fields), ledger)
        for action in [
            selected.action_import,
            publication.action_capture,
            lambda: selected.write({"review_note": "Unauthorized root change"}),
            lambda: publication.unlink(),
        ]:
            with self.assertRaises(AccessError):
                action()
        pending = (
            self.env["l10n_ae.cbuae.publication"]
            .with_user(self.simple_accountman)
            .create({"requested_date": "2026-08-28"})
        )
        with self.assertRaises(AccessError):
            pending.with_user(operator).with_context(
                allowed_company_ids=branch.ids
            ).read(["requested_date"])

    def test_branch_manager_cannot_mutate_root_or_read_unimported_foreign_sources(self):
        usd, _anchor, imported = self._reviewed_cbuae_usd()
        root = self.env.company
        branch = self.env["res.company"].create(
            {"name": "Synthetic CBUAE restricted branch", "parent_id": root.id}
        )
        sibling = self.env["res.company"].create(
            {"name": "Synthetic CBUAE sibling", "parent_id": root.id}
        )
        foreign = self.env["res.company"].create(
            {
                "name": "Synthetic CBUAE unrelated root",
                "country_id": self.env.ref("base.ae").id,
                "currency_id": self.env.ref("base.AED").id,
            }
        )
        setup_manager = new_test_user(
            self.env,
            login="synthetic-cbuae-branch-source-setup",
            groups="base.group_user,account.group_account_manager",
            company_id=root.id,
            company_ids=(root + sibling + foreign).ids,
        )
        manager = new_test_user(
            self.env,
            login="synthetic-cbuae-only-branch-manager",
            groups="base.group_user,account.group_account_manager",
            company_id=branch.id,
            company_ids=branch.ids,
        )
        source = imported.publication_id
        unimported = (
            self.env["l10n_ae.cbuae.rate.review"]
            .with_user(self.simple_accountman)
            .create(
                {
                    "publication_id": source.id,
                    "source_label": "US Dollar",
                    "currency_id": usd.id,
                    "review_note": "Separate review not imported.",
                }
            )
        )
        hidden_publications = self.env["l10n_ae.cbuae.publication"]
        for company in (root, sibling, foreign):
            publication = (
                self.env["l10n_ae.cbuae.publication"]
                .with_user(setup_manager)
                .with_context(allowed_company_ids=company.ids)
                .create({"requested_date": "2026-08-26", "company_id": company.id})
            )
            hidden_publications |= publication
        foreign_publication = (
            hidden_publications.filtered(lambda record: record.company_id == foreign)
            .with_user(setup_manager)
            .with_context(allowed_company_ids=foreign.ids)
        )
        response = Response()
        response.status_code = 200
        response.url = (
            "https://centralbank.ae/umbraco/Surface/Exchange/"
            "GetExchangeRateAllCurrencyDate?dateTime=2026-08-26"
        )
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response._content = source.source_body.content
        response._content_consumed = True
        with patch("requests.get", return_value=response):
            foreign_publication.action_capture()
        self.assertEqual(foreign_publication.state, "captured")
        foreign_review = (
            self.env["l10n_ae.cbuae.rate.review"]
            .with_user(setup_manager)
            .with_context(allowed_company_ids=foreign.ids)
            .create(
                {
                    "publication_id": foreign_publication.id,
                    "source_label": "US Dollar",
                    "currency_id": usd.id,
                    "review_note": "Unrelated root reviewed separately.",
                }
            )
        )
        foreign_review.action_import()
        self.assertTrue(foreign_review.native_rate_id)
        branch_reviews = (
            self.env["l10n_ae.cbuae.rate.review"]
            .with_user(manager)
            .with_context(allowed_company_ids=branch.ids)
        )
        branch_sources = (
            self.env["l10n_ae.cbuae.publication"]
            .with_user(manager)
            .with_context(allowed_company_ids=branch.ids)
        )
        self.assertFalse(branch_reviews.env.su)
        self.assertEqual(branch_reviews.search([]), imported)
        self.assertEqual(branch_sources.search([]), source)
        for record in unimported + foreign_review:
            with self.assertRaises(AccessError):
                branch_reviews.browse(record.id).read(["review_note"])
        for publication in hidden_publications:
            with self.assertRaises(AccessError):
                branch_sources.browse(publication.id).read(["source_body"])
        reviewed_source = branch_reviews.browse(imported.id)
        publication = branch_sources.browse(source.id)
        self.assertEqual(publication.source_body.content, source.source_body.content)
        source_before = source.read(["source_sha256", "write_uid", "write_date"])
        review_before = imported.read(
            ["native_rate_id", "reviewed_by", "reviewed_at", "write_uid", "write_date"]
        )
        for actor_review, actor_publication in (
            (reviewed_source, publication),
            (reviewed_source.sudo(), publication.sudo()),
        ):
            for action in [
                actor_review.action_import,
                actor_publication.action_capture,
                partial(actor_review.write, {"review_note": "Unauthorized root edit"}),
                actor_review.unlink,
                actor_publication.unlink,
                partial(actor_publication.write, {"requested_date": "2026-08-28"}),
                partial(
                    actor_publication.create,
                    {"company_id": root.id, "requested_date": "2026-08-28"},
                ),
                partial(
                    actor_review.create,
                    {
                        "publication_id": source.id,
                        "source_label": "US Dollar",
                        "currency_id": usd.id,
                        "review_note": "Unauthorized review",
                    },
                ),
            ]:
                with self.assertRaises(AccessError):
                    action()
        self.assertEqual(
            source.read(["source_sha256", "write_uid", "write_date"]), source_before
        )
        self.assertEqual(
            imported.read(
                [
                    "native_rate_id",
                    "reviewed_by",
                    "reviewed_at",
                    "write_uid",
                    "write_date",
                ]
            ),
            review_before,
        )

    def _reviewed_cbuae_usd(self):
        manager = self.simple_accountman
        company = self.env.company
        usd = self.env.ref("base.USD")
        usd.active = True
        anchor = self.env["res.currency.rate"].create(
            {
                "name": "2026-08-25",
                "company_id": company.id,
                "currency_id": self.env.ref("base.AED").id,
                "rate": 1.0,
            }
        )
        publication = (
            self.env["l10n_ae.cbuae.publication"]
            .with_user(manager)
            .create({"requested_date": "2026-08-26"})
        )
        response = Response()
        response.status_code = 200
        response.url = (
            "https://centralbank.ae/umbraco/Surface/Exchange/"
            "GetExchangeRateAllCurrencyDate?dateTime=2026-08-26"
        )
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response._content = (
            Path(__file__).parents[2]
            / "l10n_ae_edi/tests/cassettes/cbuae/cbuae-2026-08-26.html"
        ).read_bytes()
        response._content_consumed = True
        with patch("requests.get", return_value=response):
            publication.action_capture()
        with Form(self.env["l10n_ae.cbuae.rate.review"].with_user(manager)) as form:
            form.publication_id = publication
            form.source_label = "US Dollar"
            form.currency_id = usd
            form.review_note = "Reviewed exact source label and USD mapping."
        review = form.record
        review.action_import()
        return usd, anchor, review

    def test_selected_cbuae_import_is_retained_without_fiscal_approval(self):
        usd, anchor, review = self._reviewed_cbuae_usd()
        self.env.company.l10n_ae_edi_auto_prepare = False
        binding = review_controlled_binding(
            self.env["l10n_ae.edi.binding"].create(
                {
                    "name": "Synthetic selected CBUAE source",
                    "fiscal_uuid_policy": "controlled_local",
                }
            ),
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic reviewed controlled contract fixture.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd
        ).with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        with Form(invoice) as form:
            form.invoice_date = "2026-08-27"
            form.l10n_ae_binding_id = binding
        invoice.action_post()
        self.assertEqual(invoice.amount_tax_signed, 18.36)
        self.assertEqual(invoice.amount_total_signed, 385.61)
        invoice.line_ids.flush_recordset()
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "write_uid",
            "write_date",
        ]
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        exchange = json.loads(issue.payload)["tax_exchange"]
        self.assertEqual(exchange["rate"], "3.672500")
        self.assertEqual(exchange["date"], "2026-08-26")
        evidence = exchange["evidence"]
        self.assertEqual(evidence["conversion_date"], "2026-08-27")
        self.assertEqual(evidence["kind"], "odoo_currency_table")
        self.assertFalse(evidence["cbuae_origin_verified"])
        provenance = evidence.get("cbuae_provenance")
        self.assertTrue(
            provenance, "Selected CBUAE review/publication chain is missing."
        )
        self.assertEqual(provenance["review_id"], review.id)
        self.assertEqual(provenance["publication_id"], review.publication_id.id)
        self.assertEqual(provenance["native_rate_id"], review.native_rate_id.id)
        self.assertEqual(provenance["source_label"], "US Dollar")
        self.assertEqual(provenance["quote_literal"], "3.6725")
        self.assertEqual(provenance["currency_code"], "USD")
        self.assertEqual(provenance["review_note"], review.review_note)
        self.assertEqual(provenance["reviewed_by"], self.simple_accountman.id)
        self.assertTrue(provenance["reviewed_at"])
        self.assertEqual(provenance["import_facts"], review.import_facts)
        self.assertEqual(provenance["source"]["date"], "2026-08-26")
        self.assertEqual(
            provenance["source"]["sha256"],
            "26998f220ca4b9e5376ef368e66206da41754b66ac545f9a24c3779b77932538",
        )
        self.assertEqual(provenance["source"]["http_status"], 200)
        self.assertTrue(provenance["source"]["retrieved_at"])
        self.assertTrue(provenance["source"]["complete"])
        self.assertEqual(invoice.l10n_ae_effective_cbuae_review_id, review)
        selected = next(f for f in evidence["rates"] if f["currency"] == "USD")
        self.assertEqual(selected["record_id"], review.native_rate_id.id)
        self.assertTrue(invoice.validate_l10n_ae_pint()["valid"])
        immutable = [
            "payload",
            "payload_hash",
            "xml_content",
            "xml_hash",
            "idempotency_key",
        ]
        frozen = issue.read(immutable)
        anchor.rate = 2.0
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.preview_l10n_ae_pint(), frozen[0]["xml_content"])
        self.assertEqual(issue.read(immutable), frozen)
        self.assertEqual(invoice.l10n_ae_effective_cbuae_review_id, review)
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)

    def test_changed_aed_anchor_requires_review_without_aborting_post(self):
        usd, anchor, review = self._reviewed_cbuae_usd()
        retained = review.read(["quote_literal", "import_facts", "reviewed_at"])
        anchor.rate = 2.0
        self.env.company.l10n_ae_edi_auto_prepare = True
        binding = review_controlled_binding(
            self.env["l10n_ae.edi.binding"].create(
                {
                    "name": "Synthetic changed AED anchor",
                    "fiscal_uuid_policy": "controlled_local",
                }
            ),
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic reviewed controlled contract fixture.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd
        ).with_user(self.simple_accountman)
        with Form(invoice) as form:
            form.invoice_date = "2026-08-27"
            form.l10n_ae_binding_id = binding
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        # The changed native anchor doubles the ledger conversion to7.345 AED/USD.
        # This is not the retained publication's3.6725 AED/USD fiscal quote.
        self.assertEqual(invoice.amount_tax_signed, 36.73)
        self.assertEqual(invoice.amount_total_signed, 771.23)
        self.assertEqual(invoice.l10n_ae_preparation_state, "review")
        self.assertFalse(invoice.l10n_ae_transmission_id)
        self.assertFalse(invoice.l10n_ae_fiscal_uuid)
        self.assertEqual(invoice.l10n_ae_effective_tax_exchange_origin, "review")
        invoice.line_ids.flush_recordset()
        ledger_fields = [
            "account_id",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "tax_ids",
            "write_uid",
            "write_date",
        ]
        ledger = invoice.line_ids.sorted("id").read(ledger_fields)
        for action in (invoice.action_l10n_ae_validate, invoice.action_l10n_ae_prepare):
            with self.assertRaisesRegex(UserError, "reviewed CBUAE quote"):
                action()
            self.assertFalse(invoice.l10n_ae_transmission_id)
            self.assertFalse(invoice.l10n_ae_fiscal_uuid)
            self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)
        self.assertEqual(
            review.read(["quote_literal", "import_facts", "reviewed_at"]), retained
        )

    def test_invoice_writer_reads_source_without_capture_or_import_authority(self):
        usd, _anchor, review = self._reviewed_cbuae_usd()
        self.env.company.l10n_ae_edi_auto_prepare = False
        operator = new_test_user(
            self.env,
            login="synthetic-cbuae-invoice-writer",
            groups="base.group_user,account.group_account_invoice",
            company_id=self.env.company.id,
            company_ids=self.env.company.ids,
        )
        self.assertFalse(operator.has_group("account.group_account_manager"))
        self.assertFalse(operator.has_group("account.group_account_readonly"))
        self.assertFalse(operator.has_group("base.group_system"))
        binding = review_controlled_binding(
            self.env["l10n_ae.edi.binding"].create(
                {
                    "name": "Synthetic CBUAE invoice reader",
                    "fiscal_uuid_policy": "controlled_local",
                }
            ),
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic reviewed controlled contract fixture.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd
        ).with_user(operator)
        self.assertFalse(invoice.env.su)
        with Form(invoice) as form:
            form.invoice_date = "2026-08-27"
            form.l10n_ae_binding_id = binding
        invoice.action_post()
        invoice.action_l10n_ae_validate()
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.amount_total_signed, 385.61)
        self.assertEqual(
            invoice.l10n_ae_effective_tax_exchange_origin, "native_unverified"
        )
        evidence = json.loads(invoice.l10n_ae_transmission_id.payload)["tax_exchange"][
            "evidence"
        ]
        self.assertFalse(evidence["cbuae_origin_verified"])
        self.assertEqual(evidence["cbuae_provenance"]["review_id"], review.id)
        readonly_review = review.with_user(operator)
        publication = review.publication_id.with_user(operator)
        self.assertEqual(
            readonly_review.read(["quote_literal"])[0]["quote_literal"], "3.6725"
        )
        self.assertEqual(
            publication.read(["source_sha256"])[0]["source_sha256"],
            evidence["cbuae_provenance"]["source"]["sha256"],
        )
        for action in (
            readonly_review.action_import,
            publication.action_capture,
            lambda: readonly_review.write({"review_note": "Unauthorized revision"}),
            lambda: publication.write({"requested_date": "2026-08-28"}),
            lambda: invoice.env["l10n_ae.cbuae.publication"].create(
                {"requested_date": "2026-08-28"}
            ),
        ):
            with self.assertRaises(AccessError):
                action()
        other = self.env["res.company"].create(
            {"name": "Synthetic isolated CBUAE company"}
        )
        other_manager = new_test_user(
            self.env,
            login="synthetic-cbuae-other-manager",
            groups="base.group_user,account.group_account_manager",
            company_id=other.id,
            company_ids=other.ids,
        )
        other_publication = (
            self.env["l10n_ae.cbuae.publication"]
            .with_user(other_manager)
            .with_context(allowed_company_ids=other.ids)
            .create({"requested_date": "2026-08-26"})
        )
        with self.assertRaises(AccessError):
            other_publication.with_user(operator).with_context(
                allowed_company_ids=self.env.company.ids
            ).read(["requested_date"])

    def test_only_selected_native_row_carries_cbuae_provenance(self):
        usd, _anchor, review = self._reviewed_cbuae_usd()
        self.env.company.l10n_ae_edi_auto_prepare = False
        binding = review_controlled_binding(
            self.env["l10n_ae.edi.binding"].create(
                {
                    "name": "Synthetic root-global CBUAE precedence",
                    "fiscal_uuid_policy": "controlled_local",
                }
            ),
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic reviewed controlled contract fixture.",
        )
        self.env["res.currency.rate"].create(
            {
                "currency_id": usd.id,
                "company_id": False,
                "name": "2026-08-28",
                "rate": 1 / 4.1,
            }
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd
        ).with_user(self.simple_accountman)
        with Form(invoice) as form:
            form.invoice_date = "2026-08-29"
            form.l10n_ae_binding_id = binding
        invoice.action_post()
        invoice.action_l10n_ae_prepare()
        imported_exchange = json.loads(invoice.l10n_ae_transmission_id.payload)[
            "tax_exchange"
        ]
        self.assertEqual(imported_exchange["rate"], "3.672500")
        self.assertEqual(imported_exchange["date"], "2026-08-26")
        self.assertEqual(
            imported_exchange["evidence"]["cbuae_provenance"]["native_rate_id"],
            review.native_rate_id.id,
        )
        self.assertEqual(invoice.amount_total_signed, 385.61)
        original = invoice.l10n_ae_transmission_id.read(
            ["payload", "xml_content", "xml_hash"]
        )

        ordinary_rate = self.env["res.currency.rate"].create(
            {
                "currency_id": usd.id,
                "company_id": self.env.company.id,
                "name": "2026-08-28",
                "rate": 0.25,
            }
        )
        following = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, currency=usd
        ).with_user(self.simple_accountman)
        with Form(following) as form:
            form.invoice_date = "2026-08-29"
            form.l10n_ae_binding_id = binding
        following.action_post()
        following.action_l10n_ae_prepare()
        exchange = json.loads(following.l10n_ae_transmission_id.payload)["tax_exchange"]
        self.assertEqual(exchange["rate"], "4.000000")
        self.assertEqual(exchange["date"], "2026-08-28")
        self.assertEqual(following.amount_tax_signed, 20)
        self.assertEqual(following.amount_total_signed, 420)
        self.assertFalse(exchange["evidence"].get("cbuae_provenance"))
        self.assertFalse(following.l10n_ae_effective_cbuae_review_id)
        self.assertFalse(exchange["evidence"]["cbuae_origin_verified"])
        self.assertEqual(
            next(f for f in exchange["evidence"]["rates"] if f["currency"] == "USD")[
                "record_id"
            ],
            ordinary_rate.id,
        )
        self.assertEqual(
            invoice.l10n_ae_transmission_id.read(
                ["payload", "xml_content", "xml_hash"]
            ),
            original,
        )
