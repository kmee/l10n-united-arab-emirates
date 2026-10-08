# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import time
from datetime import date

import requests
from lxml import html

from odoo import fields, models
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
        [("draft", "Not Captured"), ("captured", "Requires Review")],
        default="draft",
        required=True,
        readonly=True,
        copy=False,
    )
    source_url = fields.Char(readonly=True, copy=False)
    retrieved_at = fields.Datetime(readonly=True, copy=False)
    http_status = fields.Integer(readonly=True, copy=False)
    content_type = fields.Char(readonly=True, copy=False)
    source_body = fields.Binary(readonly=True, copy=False, attachment=False)
    source_sha256 = fields.Char(readonly=True, copy=False)
    publication_stamp = fields.Char(readonly=True, copy=False)
    publication_date = fields.Date(readonly=True, copy=False)
    quotes = fields.Json(readonly=True, copy=False)

    def action_capture(self):
        self.ensure_one()
        self.check_access("write")
        url = (
            "https://centralbank.ae/umbraco/Surface/Exchange/"
            "GetExchangeRateAllCurrencyDate?dateTime="
            + fields.Date.to_string(self.requested_date)
        )
        with requests.get(
            url, timeout=(5, 15), allow_redirects=False, stream=True
        ) as response:
            body = b"".join(response.iter_content(chunk_size=8192))
            tree = html.fromstring(body)
            stamp_node = tree.xpath("//p[contains(., 'Last updated:')]")[0]
            stamp = " ".join(stamp_node.text_content().split()).removeprefix(
                "Last updated: "
            )
            stamp_parts = time.strptime(stamp, "%A %d %B %Y %I:%M:%S %p")
            publication_date = date(
                stamp_parts.tm_year, stamp_parts.tm_mon, stamp_parts.tm_mday
            )
            quotes = {
                row.xpath("./td[2]")[0].text_content().strip(): row.xpath("./td[3]")[0]
                .text_content()
                .strip()
                for row in tree.xpath("//table/tbody/tr")
            }
            self.write(
                {
                    "state": "captured",
                    "source_url": response.url,
                    "retrieved_at": fields.Datetime.now(),
                    "http_status": response.status_code,
                    "content_type": response.headers.get("Content-Type"),
                    "source_body": BinaryBytes(body),
                    "source_sha256": hashlib.sha256(body).hexdigest(),
                    "publication_stamp": stamp,
                    "publication_date": publication_date,
                    "quotes": quotes,
                }
            )
        return True
