# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
import re
import uuid
from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import UserError

UAE_TRANSACTION_FLAG_FIELDS = (
    "l10n_ae_free_zone",
    "l10n_ae_deemed_supply",
    "l10n_ae_margin_scheme",
    "l10n_ae_summary",
    "l10n_ae_continuous_supply",
    "l10n_ae_disclosed_agent",
    "l10n_ae_ecommerce",
    "l10n_ae_exports",
)


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_ae_received_document = fields.Boolean(
        string="Received UAE Document", readonly=True, copy=False
    )
    l10n_ae_received_process = fields.Selection(
        [("billing", "Billing"), ("selfbilling", "Self-Billing")],
        string="Received UAE Process",
        readonly=True,
        copy=False,
    )
    l10n_ae_received_agreement_snapshot = fields.Json(readonly=True, copy=False)
    l10n_ae_received_agreement_reference = fields.Char(
        string="Agreement Reference at Receipt",
        compute="_compute_l10n_ae_received_agreement_reference",
    )

    @api.depends("l10n_ae_received_agreement_snapshot")
    def _compute_l10n_ae_received_agreement_reference(self):
        for move in self:
            move.l10n_ae_received_agreement_reference = (
                move.l10n_ae_received_agreement_snapshot or {}
            ).get("reference")

    l10n_ae_received_currency_id = fields.Many2one(
        "res.currency", string="Received Document Currency", readonly=True, copy=False
    )
    l10n_ae_received_prepaid_amount = fields.Monetary(
        string="Received Declared Prepaid Amount",
        readonly=True,
        copy=False,
        currency_field="l10n_ae_received_currency_id",
    )
    l10n_ae_received_payable_amount = fields.Monetary(
        string="Received Declared Payable Amount",
        readonly=True,
        copy=False,
        currency_field="l10n_ae_received_currency_id",
    )
    l10n_ae_received_fiscal_currency_id = fields.Many2one(
        "res.currency", string="Received Fiscal Currency", readonly=True, copy=False
    )
    l10n_ae_received_tax_exchange_rate = fields.Char(
        string="Received Declared AED Exchange Rate", readonly=True, copy=False
    )
    l10n_ae_received_tax_amount = fields.Monetary(
        string="Received Declared AED VAT",
        readonly=True,
        copy=False,
        currency_field="l10n_ae_received_fiscal_currency_id",
    )
    l10n_ae_received_tax_inclusive_amount = fields.Monetary(
        string="Received Declared AED Gross Amount",
        readonly=True,
        copy=False,
        currency_field="l10n_ae_received_fiscal_currency_id",
    )
    l10n_ae_tax_exchange_rate = fields.Char(
        string="Reviewed AED Exchange Rate",
        copy=False,
        help=(
            "AED per one unit of the document currency, preserving the source decimals."
        ),
    )
    l10n_ae_tax_exchange_date = fields.Date(string="AED Exchange Rate Date", copy=False)
    l10n_ae_tax_exchange_source = fields.Char(
        string="AED Exchange Rate Source", copy=False
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any("l10n_ae_received_document" in vals for vals in vals_list):
            raise UserError(self.env._("The received UAE origin is protected."))
        return super().create(vals_list)

    def _l10n_ae_mark_received(self):
        return super().write({"l10n_ae_received_document": True})

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
        help="Fiscal UUID supplied by the ASP. Local assignment is available "
        "only in controlled simulation.",
    )
    l10n_ae_credit_reference_ids = fields.One2many(
        "l10n_ae.credit.reference",
        "move_id",
        string="UAE Credit Allocations",
        copy=False,
        help="Reviewed allocations and original references; "
        "these do not reconcile accounting.",
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
    l10n_ae_submission_state = fields.Selection(
        related="l10n_ae_transmission_id.submission_state",
        string="UAE Submission Status",
    )
    l10n_ae_exchange_mode = fields.Selection(
        related="l10n_ae_transmission_id.exchange_mode",
        string="UAE Exchange Mode",
    )
    l10n_ae_confirmation_state = fields.Selection(
        related="l10n_ae_transmission_id.confirmation_state",
        string="UAE Confirmation Status",
    )
    l10n_ae_validation_c2_state = fields.Selection(
        related="l10n_ae_transmission_id.validation_c2_state",
        string="ASP Validation",
    )
    l10n_ae_delivery_c3_state = fields.Selection(
        related="l10n_ae_transmission_id.delivery_c3_state",
        string="Invoice Delivery",
    )
    l10n_ae_reporting_c2_state = fields.Selection(
        related="l10n_ae_transmission_id.reporting_c2_state",
        string="Supplier ASP Tax Report",
    )
    l10n_ae_reporting_c3_state = fields.Selection(
        related="l10n_ae_transmission_id.reporting_c3_state",
        string="Buyer ASP Tax Report",
    )
    l10n_ae_event_review_required = fields.Boolean(
        related="l10n_ae_transmission_id.event_review_required",
        string="UAE Events Require Review",
    )
    l10n_ae_origin_system = fields.Char(default="odoo", required=True, copy=False)
    l10n_ae_selfbilling_agreement_id = fields.Many2one(
        "l10n_ae.selfbilling.agreement",
        string="UAE Self-Billing Agreement",
        check_company=True,
        copy=False,
    )
    l10n_ae_origin_document_id = fields.Char(
        default=lambda self: str(uuid.uuid4()), required=True, copy=False
    )

    def _post(self, soft=True):
        candidates = self.filtered(
            lambda move: (
                move.company_id.country_id.code == "AE"
                and not move.l10n_ae_received_document
                and move.company_id.l10n_ae_edi_auto_prepare
                and (
                    move.move_type in ("out_invoice", "out_refund")
                    or move.move_type in ("in_invoice", "in_refund")
                    and move.is_self_billing
                )
                and (
                    not soft
                    or not move.date
                    or move.date <= fields.Date.context_today(move)
                )
            )
        )
        candidates.action_l10n_ae_validate()
        posted = super()._post(soft=soft)
        for move in posted & candidates:
            if not move.l10n_ae_binding_id:
                move.l10n_ae_binding_id = move.company_id.l10n_ae_edi_binding_id
            move.with_company(move.company_id).action_l10n_ae_prepare()
        return posted

    def write(self, vals):
        if "l10n_ae_received_document" in vals:
            raise UserError(self.env._("The received UAE origin is protected."))
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
            "l10n_ae_selfbilling_agreement_id",
            "l10n_ae_tax_exchange_rate",
            "l10n_ae_tax_exchange_date",
            "l10n_ae_tax_exchange_source",
            "l10n_ae_beneficiary_id",
            "l10n_ae_document_category",
            "l10n_ae_credit_reason",
            "l10n_ae_credit_reference_ids",
        } | set(UAE_TRANSACTION_FLAG_FIELDS)
        received_fields = identity_fields | {
            "l10n_ae_received_agreement_snapshot",
            "l10n_ae_received_process",
            "l10n_ae_document_category",
            "l10n_ae_credit_reason",
            "l10n_ae_received_currency_id",
            "l10n_ae_received_prepaid_amount",
            "l10n_ae_received_payable_amount",
            "l10n_ae_received_fiscal_currency_id",
            "l10n_ae_received_tax_exchange_rate",
            "l10n_ae_received_tax_amount",
            "l10n_ae_received_tax_inclusive_amount",
        }
        if received_fields.intersection(vals) and self.filtered(
            "l10n_ae_received_document"
        ):
            raise UserError(
                self.env._("The received UAE fiscal identity cannot be changed.")
            )
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

    def _l10n_ae_transaction_flags(self):
        self.ensure_one()
        return "".join(
            "1" if self[name] else "0" for name in UAE_TRANSACTION_FLAG_FIELDS
        )

    def _l10n_ae_destination_override(self):
        self.ensure_one()
        if not self.l10n_ae_deemed_supply:
            return None
        if self.l10n_ae_document_category == "commercial":
            raise UserError(
                self.env._(
                    "IBR-157-AE: deemed supply is not eligible for a commercial "
                    "invoice or credit note. Review the document category."
                )
            )
        if self.is_self_billing:
            raise UserError(
                self.env._(
                    "Deemed supply self-billing requires a reviewed routing "
                    "policy before preparation."
                )
            )
        return {
            "scheme": "0235",
            "endpoint": "9900000097",
            "reason": "deemed_supply",
        }

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

        company_party = {**party(company.partner_id), "tin": company.l10n_ae_tin}
        counterparty = party(partner)
        agreement = self.l10n_ae_selfbilling_agreement_id
        destination = self._l10n_ae_destination_override()
        return {
            "contract_version": "kmee-controlled-1",
            "company": company_party,
            "counterparty": counterparty,
            "issuer_role": "buyer" if self.is_self_billing else "supplier",
            "supplier": counterparty if self.is_self_billing else company_party,
            "buyer": company_party if self.is_self_billing else counterparty,
            "destination": destination,
            "exchange_mode": "reporting_only" if destination else "exchange",
            "selfbilling_agreement": {
                "reference": agreement.name,
                "local_role": agreement.local_role,
                "start_date": fields.Date.to_string(agreement.start_date),
                "end_date": fields.Date.to_string(agreement.end_date),
                "scope": agreement.scope,
                "onboarding_reference": agreement.onboarding_reference,
            }
            if self.is_self_billing
            else None,
            "invoice_number": self.name,
            "issue_date": fields.Date.to_string(self.invoice_date),
            "document_category": self.l10n_ae_document_category,
            "transaction_flags": self._l10n_ae_transaction_flags(),
            "beneficiary_id": self.l10n_ae_beneficiary_id or None,
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
            "preceding_invoices": [
                {
                    "number": ref.number,
                    "issue_date": fields.Date.to_string(ref.issue_date),
                    "allocated_net": amount(ref.allocated_net),
                    "allocated_vat": amount(ref.allocated_tax),
                    "review_evidence": ref.review_evidence or None,
                }
                for ref in self.l10n_ae_credit_reference_ids
            ],
            "process": "selfbilling" if self.is_self_billing else "billing",
            "currency": self.currency_id.name,
            "tax_exchange": {
                "source_currency": self.currency_id.name,
                "target_currency": "AED",
                "rate": self.l10n_ae_tax_exchange_rate,
                "date": fields.Date.to_string(self.l10n_ae_tax_exchange_date),
                "source": self.l10n_ae_tax_exchange_source,
            }
            if self.currency_id.name != "AED"
            else None,
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
        # Serialize first preparation before looking at a possibly cached empty
        # reference. A stale PostgreSQL snapshot is retried at Odoo's request
        # boundary, never by committing or retrying part of a business action.
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM account_move WHERE id = %s FOR UPDATE", [self.id]
        )
        self.invalidate_recordset()
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
            with self.env.cr.savepoint():
                uuid_source = "supplied" if self.l10n_ae_fiscal_uuid else None
                if (
                    not self.l10n_ae_fiscal_uuid
                    and binding.fiscal_uuid_policy == "controlled_local"
                ):
                    self.l10n_ae_fiscal_uuid = str(uuid.uuid4())
                    uuid_source = "controlled_local"
                snapshot = self._l10n_ae_snapshot()
                snapshot["fiscal_uuid_assignment"] = {
                    "policy": binding.fiscal_uuid_policy,
                    "source": uuid_source,
                    "environment": binding.environment,
                    "simulation": True,
                }
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
                            "exchange_mode": snapshot["exchange_mode"],
                            "delivery_c3_state": "not_applicable"
                            if snapshot["exchange_mode"] == "reporting_only"
                            else "unknown",
                            "reporting_c3_state": "not_applicable"
                            if snapshot["exchange_mode"] == "reporting_only"
                            else "unknown",
                            "origin_system": self.l10n_ae_origin_system,
                            "origin_document_id": self.l10n_ae_origin_document_id,
                            "payload": payload,
                            "payload_hash": hashlib.sha256(
                                payload.encode()
                            ).hexdigest(),
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

    def _check_l10n_ae_credit_references(self):
        self.ensure_one()
        references = self.l10n_ae_credit_reference_ids
        if references.filtered(
            lambda ref: not ref.invoice_id and not (ref.review_evidence or "").strip()
        ):
            raise UserError(
                self.env._(
                    "Record reviewed evidence for the external original invoices "
                    "and their credit allocations before emission."
                )
            )
        for reference in references.filtered("invoice_id"):
            original = reference.invoice_id
            number, issue_date = reference._invoice_identity(original)
            if (
                reference.number != number
                or reference.issue_date != fields.Date.to_date(issue_date)
            ):
                raise UserError(
                    self.env._(
                        "Preserve the local original identifiers: its invoice "
                        "number and issue date must match the reviewed reference."
                    )
                )
            expected_type = "in_invoice" if self.is_self_billing else "out_invoice"
            if (
                original.state != "posted"
                or original.move_type != expected_type
                or original.company_id != self.company_id
                or original.commercial_partner_id != self.commercial_partner_id
                or original.currency_id != self.currency_id
                or original.l10n_ae_document_category != self.l10n_ae_document_category
                or original.is_self_billing != self.is_self_billing
                or original.l10n_ae_received_document
            ):
                raise UserError(
                    self.env._(
                        "Select a posted original invoice for the same company, "
                        "counterparty, currency, category and process."
                    )
                )
        if references and (
            self.currency_id.compare_amounts(
                sum(references.mapped("allocated_net")), self.amount_untaxed
            )
            or self.currency_id.compare_amounts(
                sum(references.mapped("allocated_tax")), self.amount_tax
            )
        ):
            raise UserError(
                self.env._(
                    "The UAE credit allocations must match the credit's net "
                    "amount and VAT, not the original invoices' totals."
                )
            )

    def action_l10n_ae_validate(self):
        for move in self:
            if move.l10n_ae_received_document:
                raise UserError(
                    self.env._("A received UAE document cannot be emitted again.")
                )
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
            if move.move_type in ("out_refund", "in_refund") and not (
                move.l10n_ae_credit_reason
            ):
                raise UserError(
                    self.env._(
                        "Select the official UAE credit note reason before preparing "
                        "the e-invoice. Free text does not replace the fiscal code."
                    )
                )
            if move.l10n_ae_free_zone and not move.l10n_ae_beneficiary_id:
                raise UserError(
                    self.env._(
                        "IBR-007-AE: a Free Zone transaction requires a beneficiary "
                        "identifier."
                    )
                )
            move._l10n_ae_destination_override()
            move._check_l10n_ae_credit_references()
            if move.currency_id.name != "AED" and not (
                move.l10n_ae_tax_exchange_rate
                and move.l10n_ae_tax_exchange_date
                and (move.l10n_ae_tax_exchange_source or "").strip()
            ):
                raise UserError(
                    self.env._(
                        "Record the reviewed AED exchange rate, its date and source "
                        "before preparing a foreign-currency e-invoice."
                    )
                )
            if move.currency_id.name != "AED" and (
                not re.fullmatch(
                    r"[0-9]+(?:\.[0-9]{1,6})?", move.l10n_ae_tax_exchange_rate
                )
                or Decimal(move.l10n_ae_tax_exchange_rate) <= 0
            ):
                raise UserError(
                    self.env._(
                        "The AED exchange rate must be a positive decimal with at "
                        "most six decimal places; preserve the reviewed source value."
                    )
                )
            if move.is_self_billing:
                if move.move_type not in ("in_invoice", "in_refund"):
                    raise UserError(
                        self.env._(
                            "Outbound self-billing requires a purchase invoice "
                            "or credit note."
                        )
                    )
                if move.l10n_ae_document_category != "tax":
                    raise UserError(
                        self.env._("Self-billing supports tax documents only.")
                    )
                agreement = move.l10n_ae_selfbilling_agreement_id
                issue_date = move.invoice_date
                if (
                    not agreement
                    or not agreement.active
                    or agreement.local_role != "buyer"
                    or agreement.company_id != move.company_id
                    or agreement.partner_id != move.partner_id.commercial_partner_id
                    or not issue_date
                    or issue_date < agreement.start_date
                    or agreement.end_date
                    and issue_date > agreement.end_date
                ):
                    raise UserError(
                        self.env._(
                            "Select an active self-billing agreement for this company, "
                            "supplier and invoice date."
                        )
                    )
                parties = (
                    move.company_id.partner_id | move.partner_id.commercial_partner_id
                )
                if any(
                    not re.fullmatch(r"[0-9]{15}", party.vat or "") for party in parties
                ):
                    raise UserError(
                        self.env._(
                            "Self-billing requires UAE VAT registration for both buyer "
                            "and supplier. Review their 15-digit TRNs."
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
