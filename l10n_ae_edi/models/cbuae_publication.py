# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import ClassVar

import requests
from lxml import etree, html

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.binary import BinaryBytes


class CbuaePublication(models.Model):
    _name = "l10n_ae.cbuae.publication"
    _description = "CBUAE Dated Exchange Publication"
    _rec_name = "requested_date"
    _check_company_auto = True

    requested_date = fields.Date(required=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        readonly=True,
    )
    state = fields.Selection(
        [
            ("draft", "Not Captured"),
            ("captured", "Requires Review"),
            ("error", "Capture Requires Review"),
        ],
        default="draft",
        required=True,
        readonly=True,
        copy=False,
    )
    diagnostic = fields.Text(readonly=True, copy=False)
    source_url = fields.Char(readonly=True, copy=False)
    retrieved_at = fields.Datetime(readonly=True, copy=False)
    http_status = fields.Integer(readonly=True, copy=False)
    content_type = fields.Char(readonly=True, copy=False)
    source_body = fields.Binary(readonly=True, copy=False, attachment=False)
    source_complete = fields.Boolean(readonly=True, copy=False)
    source_sha256 = fields.Char(readonly=True, copy=False)
    publication_stamp = fields.Char(readonly=True, copy=False)
    publication_date = fields.Date(readonly=True, copy=False)
    quotes = fields.Json(readonly=True, copy=False)

    _captured_fields: ClassVar[set[str]] = {
        "state",
        "diagnostic",
        "source_url",
        "retrieved_at",
        "http_status",
        "content_type",
        "source_body",
        "source_sha256",
        "source_complete",
        "publication_stamp",
        "publication_date",
        "quotes",
    }

    def _check_actor(self, operation):
        principal = self.env.user.with_user(self.env.uid)
        if (
            not principal.active
            or principal.share
            or not principal.has_group("account.group_account_manager")
        ):
            raise AccessError(
                self.env._("An active CBUAE company reviewer is required.")
            )
        self.with_user(self.env.uid).check_access(operation)
        for record in self:
            self._check_capture_company(record.company_id)

    def _check_capture_company(self, company):
        principal = self.env.user.with_user(self.env.uid)
        if (
            len(company) != 1
            or company.id not in principal.company_ids.ids
            or company.id not in self.with_user(self.env.uid).env.companies.ids
        ):
            raise AccessError(self.env._("This CBUAE company is not accessible."))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_actor("create")
        prepared = []
        for values in vals_list:
            if self._captured_fields.intersection(values):
                raise UserError(self.env._("Use the CBUAE capture action."))
            vals = dict(values)
            company_id = vals.get(
                "company_id", self.default_get(["company_id"]).get("company_id")
            )
            company = self.env["res.company"].browse(company_id).exists()
            self._check_capture_company(company)
            vals["company_id"] = company.id
            vals.update(dict.fromkeys(self._captured_fields, False))
            vals["state"] = "draft"
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        self._check_actor("write")
        self._lock_capture()
        if self._captured_fields.union({"company_id"}).intersection(vals):
            raise UserError(
                self.env._("CBUAE capture identity and evidence are protected.")
            )
        if self.filtered(lambda record: record.state != "draft"):
            raise UserError(self.env._("Completed CBUAE acquisitions are immutable."))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_completed_capture(self):
        self._check_actor("unlink")
        self._lock_capture()
        if self.filtered(lambda record: record.state != "draft"):
            raise UserError(
                self.env._("Completed CBUAE acquisitions cannot be deleted.")
            )

    def _lock_capture(self):
        if self:
            self.env.cr.execute(
                "SELECT id FROM l10n_ae_cbuae_publication WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(self.ids),),
            )
            self.invalidate_recordset()

    def _parse_publication(self, body, values):
        tree = html.fromstring(body, parser=html.HTMLParser(no_network=True))
        sections = tree.xpath("//section[@id='section2']")
        if len(sections) != 1:
            raise ValueError(self.env._("CBUAE source table structure changed."))
        section = sections[0]
        headings = section.xpath(".//p[contains(concat(' ', @class, ' '), ' h1 ')]")
        if len(headings) != 1 or " ".join(headings[0].text_content().split()) != (
            "Exchange rates against UAE Dirham for VAT related obligations"
        ):
            raise ValueError(self.env._("CBUAE source heading changed."))
        tables = section.xpath(".//table")
        if len(tables) != 1:
            raise ValueError(self.env._("CBUAE source table structure changed."))
        table = tables[0]
        headers = [cell.text_content().strip() for cell in table.xpath("./thead/tr/th")]
        if headers != ["", "Currency", "Rates"]:
            raise ValueError(self.env._("CBUAE source table headers changed."))
        rows = table.xpath("./tbody/tr")
        if not rows:
            raise ValueError(
                self.env._(
                    "Dated CBUAE publication is unavailable; review the original."
                )
            )
        stamps = section.xpath(".//p[contains(., 'Last updated:')]")
        if len(stamps) != 1:
            raise ValueError(self.env._("CBUAE publication timestamp is ambiguous."))
        stamp = " ".join(stamps[0].text_content().split()).removeprefix(
            "Last updated: "
        )
        values["publication_stamp"] = stamp
        match = re.fullmatch(
            r"([A-Za-z]+) ([0-9]{1,2}) ([A-Za-z]+) ([0-9]{4}) "
            r"([0-9]{2}):([0-9]{2}):([0-9]{2}) (AM|PM)",
            stamp,
        )
        months = (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        )
        weekdays = (
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        )
        if not match or match[3] not in months:
            raise ValueError(self.env._("CBUAE publication timestamp changed."))
        publication_date = date(
            int(match[4]), months.index(match[3]) + 1, int(match[2])
        )
        values["publication_date"] = publication_date
        if (
            match[1] != weekdays[publication_date.weekday()]
            or not 1 <= int(match[5]) <= 12
            or int(match[6]) > 59
            or int(match[7]) > 59
        ):
            raise ValueError(self.env._("CBUAE publication timestamp is inconsistent."))
        if publication_date != self.requested_date:
            raise ValueError(
                self.env._("CBUAE publication date differs from the requested date.")
            )
        quotes = {}
        for row in rows:
            cells = row.xpath("./td")
            if len(cells) != 3 or cells[0].text_content().strip():
                raise ValueError(self.env._("CBUAE quote row structure changed."))
            label = cells[1].text_content()
            literal = cells[2].text_content().strip()
            if not label.strip() or len(label) > 128 or label in quotes:
                raise ValueError(
                    self.env._("CBUAE quote labels are missing or duplicated.")
                )
            if len(literal) > 64 or not re.fullmatch(
                r"[0-9]+(?:\.[0-9]+)?(?:[Ee][+-]?[0-9]+)?", literal
            ):
                raise ValueError(self.env._("CBUAE quote is not a positive decimal."))
            try:
                quote = Decimal(literal)
            except InvalidOperation as exc:
                raise ValueError(self.env._("CBUAE quote decimal is invalid.")) from exc
            if not quote.is_finite() or quote <= 0:
                raise ValueError(self.env._("CBUAE quote must be positive and finite."))
            quotes[label] = literal
        values["quotes"] = quotes

    def action_capture(self):
        self.ensure_one()
        self._check_actor("write")
        self._lock_capture()
        if self.state != "draft":
            return True
        url = (
            "https://centralbank.ae/umbraco/Surface/Exchange/"
            "GetExchangeRateAllCurrencyDate?dateTime="
            + fields.Date.to_string(self.requested_date)
        )
        values = {
            "state": "captured",
            "diagnostic": False,
            "source_url": url,
            "retrieved_at": fields.Datetime.now(),
            "source_complete": False,
        }
        body = bytearray()
        try:
            with requests.get(
                url, timeout=(5, 15), allow_redirects=False, stream=True
            ) as response:
                values.update(
                    source_url=response.url,
                    http_status=response.status_code,
                    content_type=response.headers.get("Content-Type", ""),
                )
                for chunk in response.iter_content(chunk_size=8192):
                    body.extend(chunk)
                values["source_complete"] = True
                if response.status_code != 200 or response.url != url:
                    raise ValueError(
                        self.env._("CBUAE HTTP status or URL requires review.")
                    )
                if (
                    values["content_type"].split(";", 1)[0].strip().lower()
                    != "text/html"
                ):
                    raise ValueError(
                        self.env._("CBUAE HTTP content type requires review.")
                    )
                self._parse_publication(bytes(body), values)
        except requests.RequestException as exc:
            values.update(
                state="error",
                quotes=False,
                diagnostic=self.env._(
                    "CBUAE acquisition failed (%s); review the retained capture.",
                    type(exc).__name__,
                ),
            )
        except (ValueError, etree.ParserError) as exc:
            values.update(state="error", diagnostic=str(exc), quotes=False)
        values.update(
            source_body=BinaryBytes(bytes(body)),
            source_sha256=hashlib.sha256(body).hexdigest(),
        )
        super().write(values)
        self.flush_recordset()
        return True
