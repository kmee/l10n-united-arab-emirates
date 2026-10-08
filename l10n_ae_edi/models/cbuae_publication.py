# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import time
from datetime import date
from typing import ClassVar

import requests
from lxml import html

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
        with requests.get(
            url, timeout=(5, 15), allow_redirects=False, stream=True
        ) as response:
            body = b"".join(response.iter_content(chunk_size=8192))
            tree = html.fromstring(body)
            stamp_nodes = tree.xpath("//p[contains(., 'Last updated:')]")
            if not tree.xpath("//table/tbody/tr"):
                super().write(
                    {
                        "state": "error",
                        "diagnostic": self.env._(
                            "Dated CBUAE publication is unavailable; "
                            "review the original."
                        ),
                        "source_url": response.url,
                        "retrieved_at": fields.Datetime.now(),
                        "http_status": response.status_code,
                        "content_type": response.headers.get("Content-Type"),
                        "source_body": BinaryBytes(body),
                        "source_sha256": hashlib.sha256(body).hexdigest(),
                    }
                )
                self.flush_recordset()
                return True
            stamp_node = stamp_nodes[0]
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
            same_date = publication_date == self.requested_date
            super().write(
                {
                    "state": "captured" if same_date else "error",
                    "diagnostic": False
                    if same_date
                    else self.env._(
                        "CBUAE publication date differs from the requested date."
                    ),
                    "source_url": response.url,
                    "retrieved_at": fields.Datetime.now(),
                    "http_status": response.status_code,
                    "content_type": response.headers.get("Content-Type"),
                    "source_body": BinaryBytes(body),
                    "source_sha256": hashlib.sha256(body).hexdigest(),
                    "publication_stamp": stamp,
                    "publication_date": publication_date,
                    "quotes": quotes if same_date else False,
                }
            )
            self.flush_recordset()
        return True
