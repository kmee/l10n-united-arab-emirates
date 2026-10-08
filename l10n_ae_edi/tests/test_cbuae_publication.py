# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from pathlib import Path
from unittest.mock import patch

from requests import ConnectionError as RequestConnectionError
from requests import ReadTimeout, Response

from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestCbuaePublication(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()

    def test_manager_captures_dated_original_without_importing_currency_rates(self):
        publication_model = self.env["l10n_ae.cbuae.publication"].with_user(
            self.simple_accountman
        )
        self.assertFalse(publication_model.env.su)
        with Form(publication_model) as form:
            form.requested_date = "2026-08-26"
        publication = form.record
        rates = self.env["res.currency.rate"].search([])
        rate_fields = ["name", "rate", "company_id", "currency_id", "write_date"]
        before = rates.read(rate_fields)
        original = (
            Path(__file__).parent / "cassettes/cbuae/cbuae-2026-08-26.html"
        ).read_bytes()
        response = Response()
        response.status_code = 200
        response.url = (
            "https://centralbank.ae/umbraco/Surface/Exchange/"
            "GetExchangeRateAllCurrencyDate?dateTime=2026-08-26"
        )
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response._content = original
        response._content_consumed = True
        with patch("requests.get", return_value=response) as http:
            publication.action_capture()
        self.assertEqual(publication.state, "captured")
        self.assertEqual(publication.publication_date.isoformat(), "2026-08-26")
        self.assertEqual(
            publication.publication_stamp, "Wednesday 26 August 2026 06:05:16 PM"
        )
        self.assertEqual(publication.source_body.content, original)
        self.assertEqual(
            publication.source_sha256,
            "26998f220ca4b9e5376ef368e66206da41754b66ac545f9a24c3779b77932538",
        )
        self.assertEqual(publication.source_url, response.url)
        self.assertEqual(publication.http_status, 200)
        self.assertEqual(publication.content_type, "text/html; charset=utf-8")
        self.assertTrue(publication.retrieved_at)
        self.assertEqual(len(publication.quotes), 77)
        self.assertEqual(publication.quotes["US Dollar"], "3.6725")
        self.assertEqual(publication.quotes["Euro"], "4.278807")
        self.assertEqual(publication.quotes["Lebanon Pound"], "4.1E-05")
        self.assertEqual(publication.quotes["Iranian rial"], "3E-06")
        self.assertEqual(self.env["res.currency.rate"].search([]), rates)
        self.assertEqual(rates.read(rate_fields), before)
        http.assert_called_once_with(
            response.url, timeout=(5, 15), allow_redirects=False, stream=True
        )

    def test_unavailable_dated_response_is_retained_without_import_or_fallback(self):
        publication = (
            self.env["l10n_ae.cbuae.publication"]
            .with_user(self.simple_accountman)
            .create({"requested_date": "2026-08-29"})
        )
        rates = self.env["res.currency.rate"].search([])
        rate_fields = ["name", "rate", "company_id", "currency_id", "write_date"]
        before = rates.read(rate_fields)
        original = (
            Path(__file__).parent / "cassettes/cbuae/cbuae-2026-08-29.html"
        ).read_bytes()
        response = Response()
        response.status_code = 200
        response.url = (
            "https://centralbank.ae/umbraco/Surface/Exchange/"
            "GetExchangeRateAllCurrencyDate?dateTime=2026-08-29"
        )
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response._content = original
        response._content_consumed = True
        with patch("requests.get", return_value=response) as http:
            publication.action_capture()
        self.assertEqual(publication.state, "error")
        self.assertIn("unavailable", publication.diagnostic.lower())
        self.assertEqual(publication.source_body.content, original)
        self.assertEqual(
            publication.source_sha256,
            "12f6feb3563dcfdb2ba226b66eb40c4b8488dc5d8e576b10d50c622de9de0d44",
        )
        self.assertEqual(publication.source_url, response.url)
        self.assertEqual(publication.http_status, 200)
        self.assertFalse(publication.publication_date)
        self.assertFalse(publication.quotes)
        self.assertEqual(self.env["res.currency.rate"].search([]), rates)
        self.assertEqual(rates.read(rate_fields), before)
        http.assert_called_once_with(
            response.url, timeout=(5, 15), allow_redirects=False, stream=True
        )

    def test_different_publication_date_retains_original_and_requires_review(self):
        publication = (
            self.env["l10n_ae.cbuae.publication"]
            .with_user(self.simple_accountman)
            .create({"requested_date": "2026-08-26"})
        )
        rates = self.env["res.currency.rate"].search([])
        before = rates.read(["name", "rate", "company_id", "currency_id", "write_date"])
        original = (
            Path(__file__).parent / "cassettes/cbuae/cbuae-2026-08-28.html"
        ).read_bytes()
        response = Response()
        response.status_code = 200
        response.url = (
            "https://centralbank.ae/umbraco/Surface/Exchange/"
            "GetExchangeRateAllCurrencyDate?dateTime=2026-08-26"
        )
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response._content = original
        response._content_consumed = True
        with patch("requests.get", return_value=response):
            publication.action_capture()
        self.assertEqual(publication.state, "error")
        self.assertIn("date", publication.diagnostic.lower())
        self.assertEqual(publication.requested_date.isoformat(), "2026-08-26")
        self.assertEqual(publication.publication_date.isoformat(), "2026-08-28")
        self.assertEqual(
            publication.publication_stamp, "Friday 28 August 2026 06:05:16 PM"
        )
        self.assertEqual(publication.source_body.content, original)
        self.assertEqual(
            publication.source_sha256,
            "e911a79d383763efbb938d72f5c09cf0758415d2c16167dc4aaf52e9c79ab27b",
        )
        self.assertFalse(publication.quotes)
        self.assertEqual(self.env["res.currency.rate"].search([]), rates)
        self.assertEqual(
            rates.read(["name", "rate", "company_id", "currency_id", "write_date"]),
            before,
        )

    def test_completed_capture_cannot_be_forged_overwritten_deleted_or_refetched(self):
        model = self.env["l10n_ae.cbuae.publication"].with_user(self.simple_accountman)
        original = (
            Path(__file__).parent / "cassettes/cbuae/cbuae-2026-08-26.html"
        ).read_bytes()
        response = Response()
        response.status_code = 200
        response.url = "https://centralbank.ae/umbraco/Surface/Exchange/GetExchangeRateAllCurrencyDate?dateTime=2026-08-26"
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response._content = original
        response._content_consumed = True
        publication = model.create({"requested_date": "2026-08-26"})
        with patch("requests.get", return_value=response):
            publication.action_capture()
        fields = [
            "state",
            "requested_date",
            "company_id",
            "source_sha256",
            "retrieved_at",
            "write_uid",
            "write_date",
            "quotes",
        ]
        before = publication.read(fields)
        for values in (
            {"source_sha256": "forged"},
            {"quotes": {"US Dollar": "99"}},
            {"state": "draft"},
            {"requested_date": "2026-08-28"},
            {"company_id": publication.company_id.id},
        ):
            with self.subTest(values=values), self.assertRaises(UserError):
                publication.write(values)
        with self.assertRaises(UserError):
            publication.unlink()
        with self.assertRaises(UserError):
            model.create({"requested_date": "2026-08-26", "source_sha256": "forged"})
        with patch("requests.get") as http:
            publication.action_capture()
            http.assert_not_called()
        self.assertEqual(publication.read(fields), before)
        self.assertEqual(publication.source_body.content, original)
        draft = model.with_context(
            default_state="captured", default_source_sha256="forged"
        ).create({"requested_date": "2026-08-28"})
        self.assertEqual(draft.state, "draft")
        self.assertFalse(draft.source_sha256)
        draft.write({"requested_date": "2026-08-26"})
        duplicate = publication.copy()
        self.assertEqual(duplicate.state, "draft")
        self.assertFalse(duplicate.source_body)

    def test_denies_unauthorized_capture_before_http_including_sudo(
        self,
    ):
        publication = (
            self.env["l10n_ae.cbuae.publication"]
            .with_user(self.simple_accountman)
            .create({"requested_date": "2026-08-26"})
        )
        reader = new_test_user(
            self.env,
            login="cbuae-reader",
            groups="account.group_account_readonly",
            company_id=self.env.company.id,
            company_ids=self.env.company.ids,
        )
        other = self.env["res.company"].create(
            {"name": "Other CBUAE entity", "country_id": self.env.ref("base.ae").id}
        )
        isolated = new_test_user(
            self.env,
            login="cbuae-other-manager",
            groups="account.group_account_manager",
            company_id=other.id,
            company_ids=other.ids,
        )
        self.assertEqual(
            publication.with_user(reader)
            .read(["requested_date"])[0]["requested_date"]
            .isoformat(),
            "2026-08-26",
        )
        with patch("requests.get") as http:
            for user in (reader, isolated):
                for elevated in (False, True):
                    with (
                        self.subTest(user=user.login, elevated=elevated),
                        self.cr.savepoint(),
                    ):
                        actor = (
                            publication.with_user(user)
                            .with_context(allowed_company_ids=user.company_ids.ids)
                            .sudo(elevated)
                        )
                        with self.assertRaises(AccessError):
                            actor.action_capture()
            reader.active = False
            with self.assertRaises(AccessError):
                publication.with_user(reader).sudo().action_capture()
            http.assert_not_called()
        self.assertEqual(publication.state, "draft")
        self.assertFalse(publication.source_body)

    def test_invalid_http_or_table_preserves_response_without_usable_quotes(self):
        model = self.env["l10n_ae.cbuae.publication"].with_user(self.simple_accountman)
        original = (
            Path(__file__).parent / "cassettes/cbuae/cbuae-2026-08-26.html"
        ).read_bytes()
        url = (
            "https://centralbank.ae/umbraco/Surface/Exchange/"
            "GetExchangeRateAllCurrencyDate?dateTime=2026-08-26"
        )
        duplicate = b"<tr><td></td><td>US Dollar</td><td>3.6725</td></tr>"
        cases = [
            ("HTTP error", original, 503, "text/html", url),
            ("redirect", original, 302, "text/html", url),
            ("different URL", original, 200, "text/html", "https://example.invalid"),
            ("wrong MIME", original, 200, "application/json", url),
            (
                "challenge",
                b"<html><title>Just a moment...</title></html>",
                200,
                "text/html",
                url,
            ),
            (
                "heading",
                original.replace(b"VAT related obligations", b"unrelated data"),
                200,
                "text/html",
                url,
            ),
            (
                "header",
                original.replace(b"Currency", b"Unknown header", 1),
                200,
                "text/html",
                url,
            ),
            (
                "no stamp",
                original.replace(b"Last updated:", b"Updated maybe:"),
                200,
                "text/html",
                url,
            ),
            (
                "weekday",
                original.replace(b"Wednesday 26", b"Friday 26"),
                200,
                "text/html",
                url,
            ),
            (
                "duplicate",
                original.replace(b"</tbody>", duplicate + b"</tbody>"),
                200,
                "text/html",
                url,
            ),
            ("zero", original.replace(b">3.6725<", b">0<"), 200, "text/html", url),
            (
                "negative",
                original.replace(b">3.6725<", b">-3.6725<"),
                200,
                "text/html",
                url,
            ),
            (
                "not finite",
                original.replace(b">3.6725<", b">NaN<"),
                200,
                "text/html",
                url,
            ),
        ]
        rates = self.env["res.currency.rate"].search([])
        before = rates.read(["name", "rate", "company_id", "currency_id", "write_date"])
        for label, body, status, mime, actual_url in cases:
            with self.subTest(label=label):
                response = Response()
                response.status_code = status
                response.url = actual_url
                response.headers["Content-Type"] = mime
                response._content = body
                response._content_consumed = True
                publication = model.create({"requested_date": "2026-08-26"})
                with patch("requests.get", return_value=response):
                    publication.action_capture()
                self.assertEqual(publication.state, "error")
                self.assertTrue(publication.diagnostic)
                self.assertFalse(publication.quotes)
                self.assertEqual(publication.source_body.content, body)
                self.assertEqual(publication.http_status, status)
                self.assertEqual(publication.content_type, mime)
                self.assertEqual(publication.source_url, actual_url)
        self.assertEqual(self.env["res.currency.rate"].search([]), rates)
        self.assertEqual(
            rates.read(["name", "rate", "company_id", "currency_id", "write_date"]),
            before,
        )

    def test_transport_failure_retains_partial_source_and_safe_diagnostic(self):
        class InterruptedResponse(Response):
            def iter_content(self, chunk_size=1, decode_unicode=False):
                yield b"partial publication"
                raise ReadTimeout("credential-like-secret")

        model = self.env["l10n_ae.cbuae.publication"].with_user(self.simple_accountman)
        response = InterruptedResponse()
        response.status_code = 200
        response.url = (
            "https://centralbank.ae/umbraco/Surface/Exchange/"
            "GetExchangeRateAllCurrencyDate?dateTime=2026-08-26"
        )
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response._content_consumed = True
        rates = self.env["res.currency.rate"].search([])
        before = rates.read(["name", "rate", "company_id", "currency_id", "write_date"])
        for label, options, expected_body in (
            ("ReadTimeout", {"return_value": response}, b"partial publication"),
            (
                "ConnectionError",
                {"side_effect": RequestConnectionError("credential-like-secret")},
                b"",
            ),
        ):
            with self.subTest(label=label):
                publication = model.create({"requested_date": "2026-08-26"})
                with patch("requests.get", **options):
                    publication.action_capture()
                self.assertEqual(publication.state, "error")
                self.assertIn(label, publication.diagnostic)
                self.assertNotIn("credential-like-secret", publication.diagnostic)
                self.assertFalse(publication.source_complete)
                self.assertEqual(
                    publication.source_body.content if publication.source_body else b"",
                    expected_body,
                )
                self.assertFalse(publication.quotes)
                self.assertTrue(publication.retrieved_at)
                self.assertEqual(publication.source_url, response.url)
                with patch("requests.get") as http:
                    publication.action_capture()
                    http.assert_not_called()
        self.assertEqual(self.env["res.currency.rate"].search([]), rates)
        self.assertEqual(
            rates.read(["name", "rate", "company_id", "currency_id", "write_date"]),
            before,
        )

    def test_changed_source_label_is_preserved_without_expanding_iso_aliases(self):
        original = (
            (Path(__file__).parent / "cassettes/cbuae/cbuae-2026-08-26.html")
            .read_bytes()
            .replace(b">US Dollar<", b"> US Dollar <")
        )
        response = Response()
        response.status_code = 200
        response.url = (
            "https://centralbank.ae/umbraco/Surface/Exchange/"
            "GetExchangeRateAllCurrencyDate?dateTime=2026-08-26"
        )
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response._content = original
        response._content_consumed = True
        publication = (
            self.env["l10n_ae.cbuae.publication"]
            .with_user(self.simple_accountman)
            .create({"requested_date": "2026-08-26"})
        )
        with patch("requests.get", return_value=response):
            publication.action_capture()
        self.assertEqual(publication.state, "captured")
        self.assertEqual(publication.quotes[" US Dollar "], "3.6725")
        self.assertNotIn("US Dollar", publication.quotes)
        self.assertNotIn("USD", publication.quotes)
        self.assertEqual(publication.source_body.content, original)
