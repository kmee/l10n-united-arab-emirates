# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json
from pathlib import Path
from unittest.mock import patch

from requests import Response

from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from .test_invoice_xml import TestUaeInvoiceXml


@tagged("post_install", "-at_install")
class TestUaeCbuaeFx(TestUaeInvoiceXml):
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic selected CBUAE source",
                "fiscal_uuid_policy": "controlled_local",
            }
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
        self.assertEqual(invoice.line_ids.sorted("id").read(ledger_fields), ledger)

    def test_changed_aed_anchor_requires_review_without_aborting_post(self):
        usd, anchor, review = self._reviewed_cbuae_usd()
        retained = review.read(["quote_literal", "import_facts", "reviewed_at"])
        anchor.rate = 2.0
        self.env.company.l10n_ae_edi_auto_prepare = True
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic changed AED anchor",
                "fiscal_uuid_policy": "controlled_local",
            }
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic CBUAE invoice reader",
                "fiscal_uuid_policy": "controlled_local",
            }
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
        binding = self.env["l10n_ae.edi.binding"].create(
            {
                "name": "Synthetic root-global CBUAE precedence",
                "fiscal_uuid_policy": "controlled_local",
            }
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
