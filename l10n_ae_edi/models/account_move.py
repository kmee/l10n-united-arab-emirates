# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
import re
import uuid
from decimal import Decimal

from odoo import fields, models
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_ae_document_category = fields.Selection(
        [("tax", "Tax Document"), ("commercial", "Commercial Document")],
        string="UAE Document Category",
        default="tax",
        required=True,
    )

    l10n_ae_free_zone = fields.Boolean(string="Free Zone Transaction", copy=False)
    l10n_ae_beneficiary_id = fields.Char(string="Beneficiary Identifier", copy=False)
    l10n_ae_deemed_supply = fields.Boolean(string="Deemed Supply", copy=False)
    l10n_ae_margin_scheme = fields.Boolean(string="Profit Margin Scheme", copy=False)
    l10n_ae_summary = fields.Boolean(string="Summary Invoice", copy=False)
    l10n_ae_continuous_supply = fields.Boolean(string="Continuous Supply", copy=False)
    l10n_ae_disclosed_agent = fields.Boolean(
        string="Disclosed Agent Billing", copy=False
    )
    l10n_ae_ecommerce = fields.Boolean(string="Supply Through E-Commerce", copy=False)
    l10n_ae_exports = fields.Boolean(string="Exports", copy=False)
    l10n_ae_fiscal_uuid = fields.Char(
        string="UAE Fiscal UUID",
        copy=False,
        help="UUID assigned by the ASP or by an explicitly agreed issuance mechanism.",
    )
    l10n_ae_credit_reason = fields.Selection(
        [
            ("DL8.61.1.A", "Cancelled Supply"),
            ("DL8.61.1.B", "Changed Supply Tax Treatment"),
            ("DL8.61.1.C", "Changed Consideration"),
            ("DL8.61.1.D", "Returned Supply and Consideration"),
            ("DL8.61.1.E", "Incorrect Tax Amount or Treatment"),
            ("VD", "Volume Discount"),
        ],
        string="UAE Credit Note Reason",
        copy=False,
    )
    l10n_ae_binding_id = fields.Many2one(
        "l10n_ae.edi.binding", string="UAE ASP Binding", check_company=True, copy=False
    )
    l10n_ae_transmission_id = fields.Many2one(
        "l10n_ae.edi.transmission",
        string="Frozen UAE Invoice",
        readonly=True,
        copy=False,
    )
    l10n_ae_origin_system = fields.Char(default="odoo", required=True, copy=False)
    l10n_ae_origin_document_id = fields.Char(
        default=lambda self: str(uuid.uuid4()), required=True, copy=False
    )

    def write(self, vals):
        if vals.get("state") in ("draft", "cancel") and self.filtered(
            "l10n_ae_transmission_id"
        ):
            raise UserError(
                self.env._(
                    "Reconcile the frozen UAE transmission before resetting or "
                    "cancelling this invoice."
                )
            )
        identity_fields = {
            "l10n_ae_transmission_id",
            "l10n_ae_origin_system",
            "l10n_ae_origin_document_id",
            "l10n_ae_fiscal_uuid",
        }
        if identity_fields.intersection(vals) and self.filtered(
            "l10n_ae_transmission_id"
        ):
            raise UserError(
                self.env._(
                    "The frozen UAE invoice reference and business identity cannot be "
                    "changed."
                )
            )
        return super().write(vals)

    def _l10n_ae_snapshot(self):
        self.ensure_one()
        company = self.company_id
        partner = self.partner_id.commercial_partner_id
        scale = Decimal(1).scaleb(-self.currency_id.decimal_places)

        def amount(value):
            return str(Decimal(str(value)).quantize(scale))

        def party(record):
            return {
                "name": record.name,
                "vat": record.vat or None,
                "street": record.street or None,
                "street2": record.street2 or None,
                "city": record.city or None,
                "zip": record.zip or None,
                "country": record.country_id.code or None,
                "subdivision": record.state_id.name or None,
                "endpoint_scheme": record.routing_scheme or None
                if "routing_scheme" in record._fields
                else None,
                "endpoint": record.routing_endpoint or None
                if "routing_endpoint" in record._fields
                else None,
                "legal_registration": record._l10n_ae_legal_registration_number()
                or None,
                "legal_registration_type": record.l10n_ae_registration_type or None,
                "legal_registration_authority": record.l10n_ae_registration_authority
                or None,
            }

        return {
            "contract_version": "kmee-controlled-1",
            "company": {**party(company.partner_id), "tin": company.l10n_ae_tin},
            "counterparty": party(partner),
            "invoice_number": self.name,
            "issue_date": fields.Date.to_string(self.invoice_date),
            "document_category": self.l10n_ae_document_category,
            "fiscal_uuid": self.l10n_ae_fiscal_uuid or None,
            "credit_reason": self.l10n_ae_credit_reason or None,
            "preceding_invoice": {
                "number": self.reversed_entry_id.name,
                "issue_date": fields.Date.to_string(
                    self.reversed_entry_id.invoice_date
                ),
            }
            if self.reversed_entry_id
            else None,
            "process": "selfbilling" if self.is_self_billing else "billing",
            "currency": self.currency_id.name,
            "origin": {
                "system": self.l10n_ae_origin_system,
                "document_id": self.l10n_ae_origin_document_id,
            },
            "totals": {
                "net": amount(self.amount_untaxed),
                "vat": amount(self.amount_tax),
                "payable": amount(self.amount_total),
            },
            "lines": [
                {
                    "name": line.name,
                    "quantity": str(line.quantity),
                    "unit_price": str(line.price_unit),
                    "discount": str(line.discount),
                    "net": amount(line.price_subtotal),
                    "gross": amount(line.price_total),
                    "taxes": [
                        {
                            "name": tax.name,
                            "rate": str(tax.amount),
                            "type": tax.amount_type,
                        }
                        for tax in line.tax_ids
                    ],
                }
                for line in self.invoice_line_ids
                if not line.display_type or line.display_type == "product"
            ],
        }

    def _l10n_ae_prepare_xml(self):
        """Format addons provide submitted bytes at the preparation boundary."""
        return False

    def action_l10n_ae_prepare(self):
        self.ensure_one()
        self.check_access("write")
        if not self.l10n_ae_transmission_id:
            self.action_l10n_ae_validate()
            if self.state != "posted":
                raise UserError(
                    self.env._(
                        "Post the invoice before preparing its UAE transmission."
                    )
                )
            binding = self.l10n_ae_binding_id
            if (
                not binding
                or not binding.active
                or binding.company_id != self.company_id
            ):
                raise UserError(
                    self.env._(
                        "Select an active ASP binding belonging to this invoice's "
                        "company."
                    )
                )
            if binding.environment != "sandbox":
                raise UserError(
                    self.env._(
                        "Only the controlled sandbox contract is currently available."
                    )
                )
            snapshot = self._l10n_ae_snapshot()
            xml_content = self._l10n_ae_prepare_xml()
            payload = json.dumps(
                snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            # This narrow elevation creates evidence only after checking the
            # operator's invoice access; the evidence itself is read-only.
            transmission = (
                self.env["l10n_ae.edi.transmission"]
                .sudo()
                .create(
                    {
                        "move_id": self.id,
                        "company_id": self.company_id.id,
                        "binding_id": binding.id,
                        "provider": binding.provider,
                        "environment": binding.environment,
                        "process": snapshot["process"],
                        "origin_system": self.l10n_ae_origin_system,
                        "origin_document_id": self.l10n_ae_origin_document_id,
                        "payload": payload,
                        "payload_hash": hashlib.sha256(payload.encode()).hexdigest(),
                        "xml_content": xml_content,
                        "xml_hash": hashlib.sha256(xml_content.encode()).hexdigest()
                        if xml_content
                        else False,
                    }
                )
            )
            self.l10n_ae_transmission_id = transmission
        return {
            "type": "ir.actions.act_window",
            "res_model": "l10n_ae.edi.transmission",
            "res_id": self.l10n_ae_transmission_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_l10n_ae_validate(self):
        for move in self:
            if move.move_type not in ("out_invoice", "out_refund") and not (
                move.move_type in ("in_invoice", "in_refund") and move.is_self_billing
            ):
                raise UserError(
                    self.env._(
                        "Only sales and an eligible self-billing purchase can be "
                        "prepared for outbound UAE e-invoicing."
                    )
                )
            if not move.company_id.l10n_ae_tin:
                raise UserError(
                    self.env._(
                        "Configure the company's UAE Taxpayer Identification Number "
                        "before preparing an e-invoice."
                    )
                )
            if not re.fullmatch(r"[0-9]{10}", move.company_id.l10n_ae_tin):
                raise UserError(
                    self.env._(
                        "The UAE Taxpayer Identification Number must contain 10 ASCII "
                        "digits."
                    )
                )
            if not move.company_id.l10n_ae_tin_confirmed:
                raise UserError(
                    self.env._(
                        "The UAE TIN must be confirmed during onboarding before "
                        "preparing an e-invoice."
                    )
                )
            if move.l10n_ae_free_zone and not move.l10n_ae_beneficiary_id:
                raise UserError(
                    self.env._(
                        "IBR-007-AE: a Free Zone transaction requires a beneficiary "
                        "identifier."
                    )
                )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("UAE local validation complete"),
                "message": self.env._("Identity and scenario checks passed."),
                "type": "success",
                "sticky": False,
            },
        }
