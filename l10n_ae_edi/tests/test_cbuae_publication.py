# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from pathlib import Path
from unittest.mock import patch

from requests import Response

from odoo.tests import Form, tagged

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
