# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
import re
import uuid
from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

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

    l10n_ae_correction_id = fields.Many2one(
        "l10n_ae.edi.correction",
        string="Local UAE Correction",
        readonly=True,
        copy=False,
        check_company=True,
        ondelete="restrict",
    )
    l10n_ae_transmission_ids = fields.One2many(
        "l10n_ae.edi.transmission",
        "move_id",
        string="UAE Issue History",
        readonly=True,
        copy=False,
    )

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
        history_fields = {
            "l10n_ae_correction_id",
            "l10n_ae_transmission_id",
            "l10n_ae_transmission_ids",
        }
        if any(any(vals.get(name) for name in history_fields) for vals in vals_list):
            raise UserError(
                self.env._("UAE issue and correction history references are protected.")
            )
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
    l10n_ae_invoicing_period_start = fields.Date(
        string="Invoice Period Start", copy=False
    )
    l10n_ae_invoicing_period_end = fields.Date(string="Invoice Period End", copy=False)
    l10n_ae_continuous_supply = fields.Boolean(string="Continuous Supply", copy=False)
    l10n_ae_disclosed_agent = fields.Boolean(
        string="Disclosed Agent Billing", copy=False
    )
    l10n_ae_principal_id = fields.Char(
        string="Principal ID",
        copy=False,
        help="The principal's TRN for disclosed-agent billing; separate from "
        "the supplier VAT and beneficiary.",
    )
    l10n_ae_ecommerce = fields.Boolean(string="Supply Through E-Commerce", copy=False)
    l10n_ae_exports = fields.Boolean(string="Exports", copy=False)
    l10n_ae_export_buyer_no_peppol = fields.Boolean(
        string="Export Buyer Not Registered in Peppol",
        copy=False,
        help="Confirm the reviewed recipient status for this export. "
        "Missing data or a failed lookup is insufficient.",
    )
    l10n_ae_buyer_not_implemented = fields.Boolean(
        string="UAE Business Buyer Not Yet Implemented",
        copy=False,
        help="Confirm a domestic business buyer without e-invoicing or a "
        "Participant ID; missing data or a failed lookup is insufficient.",
    )
    l10n_ae_buyer_onboarding_review_date = fields.Date(
        string="Buyer Status Review Date",
        copy=False,
    )
    l10n_ae_buyer_onboarding_review_reference = fields.Char(
        string="Buyer Status Review Reference",
        copy=False,
    )
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
    l10n_ae_dispatch_review_required = fields.Boolean(
        related="l10n_ae_transmission_id.dispatch_review_required",
        string="Automatic Sending Requires Review",
    )
    l10n_ae_dispatch_error_kind = fields.Selection(
        related="l10n_ae_transmission_id.dispatch_error_kind",
        string="Local Review Reason",
    )
    l10n_ae_dispatch_review_at = fields.Datetime(
        related="l10n_ae_transmission_id.dispatch_review_at",
        string="Local Review Recorded At",
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
        protected = {
            "l10n_ae_correction_id",
            "l10n_ae_transmission_ids",
            "l10n_ae_transmission_id",
        }
        origin = {
            "l10n_ae_origin_system",
            "l10n_ae_origin_document_id",
            "company_id",
            "move_type",
        }
        if protected.intersection(vals) or (
            origin.intersection(vals) and self.filtered("l10n_ae_transmission_ids")
        ):
            raise UserError(
                self.env._(
                    "The frozen UAE invoice reference and business identity "
                    "cannot be changed."
                )
            )
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
            "l10n_ae_invoicing_period_start",
            "l10n_ae_invoicing_period_end",
            "l10n_ae_beneficiary_id",
            "l10n_ae_principal_id",
            "l10n_ae_document_category",
            "l10n_ae_credit_reason",
            "l10n_ae_credit_reference_ids",
            "l10n_ae_buyer_not_implemented",
            "l10n_ae_export_buyer_no_peppol",
            "l10n_ae_buyer_onboarding_review_date",
            "l10n_ae_buyer_onboarding_review_reference",
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
        buyer = (
            self.company_id.partner_id
            if self.is_self_billing
            else self.partner_id.commercial_partner_id
        )
        destination = self._l10n_ae_export_destination(buyer)
        if destination:
            return destination
        if self.l10n_ae_buyer_not_implemented and (
            self.l10n_ae_deemed_supply or self.l10n_ae_exports or self.is_self_billing
        ):
            raise UserError(
                self.env._(
                    "Review the not-yet-implemented buyer routing: it cannot be "
                    "combined with deemed supply, exports or self-billing."
                )
            )
        if not self.l10n_ae_deemed_supply:
            if self.l10n_ae_buyer_not_implemented:
                if self.partner_id.commercial_partner_id.country_id.code != "AE":
                    raise UserError(
                        self.env._(
                            "Confirm a domestic UAE business buyer before using "
                            "the not-yet-implemented buyer destination."
                        )
                    )
                if self.partner_id.commercial_partner_id.routing_endpoint:
                    raise UserError(
                        self.env._(
                            "Review the existing Participant ID before selecting a "
                            "not-yet-implemented buyer. The existing identifier "
                            "cannot be replaced by a predefined destination."
                        )
                    )
                if (
                    not self.l10n_ae_buyer_onboarding_review_date
                    or not (
                        self.l10n_ae_buyer_onboarding_review_reference or ""
                    ).strip()
                ):
                    raise UserError(
                        self.env._(
                            "Record the buyer status review date and source before "
                            "using the not-yet-implemented buyer destination."
                        )
                    )
                return {
                    "scheme": "0235",
                    "endpoint": "9900000098",
                    "reason": "buyer_not_implemented",
                }
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

    def _l10n_ae_export_destination(self, buyer):
        if self.l10n_ae_exports and self.l10n_ae_document_category == "commercial":
            raise UserError(
                self.env._(
                    "Exports do not apply to a commercial invoice or credit note "
                    "under UAE Electronic Invoicing Guidelines section 10.4. "
                    "Review the document category and transaction classification."
                )
            )
        if self.l10n_ae_export_buyer_no_peppol:
            if not self.l10n_ae_exports:
                raise UserError(
                    self.env._(
                        "Select Exports before confirming a non-Peppol export buyer."
                    )
                )
            if self.l10n_ae_buyer_not_implemented:
                raise UserError(
                    self.env._(
                        "Review the conflicting buyer status declarations before "
                        "selecting a predefined destination."
                    )
                )
            if self.l10n_ae_deemed_supply:
                raise UserError(
                    self.env._(
                        "Review the combined export and deemed-supply routing policy "
                        "before selecting a predefined destination."
                    )
                )
            if buyer.routing_endpoint:
                raise UserError(
                    self.env._(
                        "Review the existing Participant ID before confirming "
                        "a non-Peppol export buyer. It cannot be replaced by "
                        "a predefined destination."
                    )
                )
            if (
                not self.l10n_ae_buyer_onboarding_review_date
                or not (self.l10n_ae_buyer_onboarding_review_reference or "").strip()
            ):
                raise UserError(
                    self.env._(
                        "Record the export buyer status review date and source "
                        "before using the predefined export destination."
                    )
                )
            return {
                "scheme": "0235",
                "endpoint": "9900000099",
                "reason": "export_buyer_no_peppol",
            }
        if (
            self.l10n_ae_exports
            and not buyer.routing_endpoint
            and not self.l10n_ae_buyer_not_implemented
        ):
            raise UserError(
                self.env._(
                    "Review the export buyer's Peppol status. Missing endpoint "
                    "data does not establish that the recipient is unregistered."
                )
            )
        return None

    def _l10n_ae_snapshot(self):
        self.ensure_one()
        company = self.company_id
        partner = self.partner_id.commercial_partner_id
        scale = Decimal(1).scaleb(-self.currency_id.decimal_places)

        def amount(value):
            return str(Decimal(str(value)).quantize(scale))

        def party(record):
            foreign_identifier = (
                record._get_preferred_legal_entity_identifier_vals()
                if record.country_id.code != "AE"
                else {}
            )
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
                "legal_registration": foreign_identifier.get("value")
                or record._l10n_ae_legal_registration_number()
                or None,
                "legal_registration_type": foreign_identifier.get("key")
                or record.l10n_ae_registration_type
                or None,
                "legal_registration_authority": record.l10n_ae_registration_authority
                or None,
                "additional_identifiers": dict(record.additional_identifiers or {}),
            }

        company_party = {**party(company.partner_id), "tin": company.l10n_ae_tin}
        counterparty = party(partner)
        agreement = self.l10n_ae_selfbilling_agreement_id
        destination = self._l10n_ae_destination_override()
        snapshot = {
            "contract_version": "kmee-controlled-1",
            "company": company_party,
            "counterparty": counterparty,
            "issuer_role": "buyer" if self.is_self_billing else "supplier",
            "supplier": counterparty if self.is_self_billing else company_party,
            "buyer": company_party if self.is_self_billing else counterparty,
            "destination": destination,
            "exchange_mode": "reporting_only" if destination else "exchange",
            "buyer_onboarding_review": {
                "status": (
                    "not_registered_in_peppol"
                    if self.l10n_ae_export_buyer_no_peppol
                    else "not_implemented"
                ),
                "date": fields.Date.to_string(
                    self.l10n_ae_buyer_onboarding_review_date
                ),
                "source": self.l10n_ae_buyer_onboarding_review_reference,
            }
            if self.l10n_ae_buyer_not_implemented or self.l10n_ae_export_buyer_no_peppol
            else None,
            "alternative_delivery": {
                "required": True,
                "reason": "buyer_not_implemented",
            }
            if self.l10n_ae_buyer_not_implemented
            and self.l10n_ae_document_category == "tax"
            and self.move_type == "out_invoice"
            else None,
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
            "principal_id": self.l10n_ae_principal_id or None,
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
        if self.l10n_ae_invoicing_period_start or self.l10n_ae_invoicing_period_end:
            snapshot["invoicing_period"] = {
                "start_date": fields.Date.to_string(self.l10n_ae_invoicing_period_start)
                or None,
                "end_date": fields.Date.to_string(self.l10n_ae_invoicing_period_end)
                or None,
            }
        return snapshot

    def _l10n_ae_prepare_xml(self):
        """Format addons provide submitted bytes at the preparation boundary."""
        return False

    def _l10n_ae_conventional_presentation(self, snapshot):
        """Preserve the native presentation locally, before later master edits."""
        if not (snapshot.get("alternative_delivery") or {}).get("required"):
            return {}
        move = self.with_company(self.company_id)
        report = self.env["account.move.send"]._get_default_pdf_report_id(move)
        html, _report_type = (
            report.with_company(move.company_id)
            .with_context(debug=False)
            ._render_qweb_html(report, move.ids, data={"report_type": "pdf"})
        )
        return {
            "conventional_html_content": html.decode(),
            "conventional_html_hash": hashlib.sha256(html).hexdigest(),
            "conventional_report_id": report.id,
            "conventional_filename": move._get_invoice_report_filename(report=report),
        }

    def action_l10n_ae_generate_conventional_pdf(self):
        """Download the same prepared conventional invoice, without delivering it."""
        self.ensure_one()
        self.check_access("read")
        transmission = self.l10n_ae_transmission_id.with_context(bin_size=False)
        if not transmission:
            raise UserError(
                self.env._("Prepare the UAE invoice before generating its PDF.")
            )
        transmission.check_access("read")
        if transmission.move_id != self or transmission.company_id != self.company_id:
            raise UserError(
                self.env._(
                    "The preserved document belongs to another invoice or company."
                )
            )
        transmission.sudo()._ensure_conventional_pdf()
        return {
            "type": "ir.actions.act_url",
            "target": "download",
            "url": (
                f"/web/content/l10n_ae.edi.transmission/{transmission.id}/"
                "conventional_pdf_content?download=true&filename_field=conventional_filename"
            ),
        }

    def action_print_pdf(self):
        self.ensure_one()
        if self.l10n_ae_transmission_id.conventional_html_content:
            return self.action_l10n_ae_generate_conventional_pdf()
        return super().action_print_pdf()

    def _get_invoice_legal_documents(self, filetype, allow_fallback=False):
        self.ensure_one()
        transmission = self.l10n_ae_transmission_id.with_context(bin_size=False)
        if filetype == "pdf" and transmission.conventional_html_content:
            self.action_l10n_ae_generate_conventional_pdf()
            return [
                {
                    "filename": transmission.conventional_filename,
                    "filetype": "application/pdf",
                    "content": transmission.conventional_pdf_content.content,
                }
            ]
        return super()._get_invoice_legal_documents(
            filetype, allow_fallback=allow_fallback
        )

    def action_l10n_ae_start_local_correction(self):
        self.ensure_one()
        self.check_access("write")
        if not self.env.user.has_group("account.group_account_manager"):
            raise AccessError(
                self.env._("Only accounting managers can correct UAE issues.")
            )
        return {
            "type": "ir.actions.act_window",
            "res_model": "l10n_ae.edi.correction.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_move_id": self.id,
                "default_source_transmission_id": self.l10n_ae_transmission_id.id,
            },
        }

    def _start_l10n_ae_local_correction(self, source, reason):
        self.ensure_one()
        self.check_access("write")
        if not self.env.user.has_group("account.group_account_manager"):
            raise AccessError(
                self.env._("Only accounting managers can correct UAE issues.")
            )
        source.ensure_one()
        source.check_access("read")
        if not reason or not reason.strip():
            raise UserError(
                self.env._("Explain the local correction before continuing.")
            )
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM account_move WHERE id = %s FOR UPDATE", [self.id]
        )
        self.invalidate_recordset()
        source.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_transmission WHERE id = %s FOR UPDATE",
            [source.id],
        )
        source.invalidate_recordset()
        if source.move_id != self or source.company_id != self.company_id:
            raise UserError(
                self.env._(
                    "The local correction source belongs to another invoice or company."
                )
            )
        action = {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }
        if source.local_correction_id:
            return action
        if (
            self.l10n_ae_transmission_id != source
            or self.state != "posted"
            or source.environment != "sandbox"
            or source.submission_state != "queued"
            or source.attempt_ids
            or source.remote_id
            or source.event_ids
            or source.validation_receipt
        ):
            raise UserError(
                self.env._(
                    "Correct only a queued sandbox issue with no transport "
                    "attempt or remote facts."
                )
            )
        with self.env.cr.savepoint():
            correction = (
                self.env["l10n_ae.edi.correction"]
                .sudo()
                ._begin(self, source, reason.strip())
            )
            source.sudo()._set_local_correction(correction)
            super().write(
                {
                    "l10n_ae_transmission_id": False,
                    "l10n_ae_correction_id": correction.id,
                }
            )
            self.button_draft()
        return action

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
                correction = self.l10n_ae_correction_id
                source = correction.source_transmission_id
                if correction and (
                    correction.replacement_transmission_id
                    or source.binding_id != binding
                    or source.company_id != self.company_id
                    or source.process != self._l10n_ae_snapshot()["process"]
                    or source.origin_system != self.l10n_ae_origin_system
                    or source.origin_document_id != self.l10n_ae_origin_document_id
                ):
                    raise UserError(
                        self.env._(
                            "Preserve the local correction's company, process, "
                            "origin and sandbox binding."
                        )
                    )
                uuid_source = "supplied" if self.l10n_ae_fiscal_uuid else None
                if (
                    not self.l10n_ae_fiscal_uuid
                    and binding.fiscal_uuid_policy == "controlled_local"
                ):
                    # Fiscal identity assignment is metadata: native invoice
                    # synchronization would recompute stored debit/credit fields
                    # and touch accounting-line timestamps without a ledger edit.
                    self.with_context(
                        skip_invoice_sync=True, skip_is_manually_modified=True
                    ).l10n_ae_fiscal_uuid = str(uuid.uuid4())
                    uuid_source = "controlled_local"
                snapshot = self._l10n_ae_snapshot()
                snapshot["fiscal_uuid_assignment"] = {
                    "policy": binding.fiscal_uuid_policy,
                    "source": uuid_source,
                    "environment": binding.environment,
                    "simulation": True,
                }
                if correction and self.l10n_ae_fiscal_uuid:
                    previous = json.loads(source.payload)
                    if previous.get("fiscal_uuid") == self.l10n_ae_fiscal_uuid:
                        snapshot["fiscal_uuid_assignment"] = previous[
                            "fiscal_uuid_assignment"
                        ]
                xml_content = self._l10n_ae_prepare_xml()
                presentation = self._l10n_ae_conventional_presentation(snapshot)
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
                            **presentation,
                            "move_id": self.id,
                            "company_id": self.company_id.id,
                            "binding_id": binding.id,
                            "provider": binding.provider,
                            "environment": binding.environment,
                            "process": snapshot["process"],
                            "revision": source.revision + 1 if correction else 1,
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
                super(
                    AccountMove,
                    self.with_context(
                        skip_invoice_sync=True, skip_is_manually_modified=True
                    ),
                ).write({"l10n_ae_transmission_id": transmission.id})
                if correction:
                    correction.sudo()._complete(transmission)
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

    def _check_l10n_ae_principal_id(self):
        self.ensure_one()

        if self.l10n_ae_principal_id and not self.l10n_ae_disclosed_agent:
            raise UserError(
                self.env._(
                    "Review Principal ID and the disclosed-agent classification "
                    "before preparing the document."
                )
            )
        if self.l10n_ae_disclosed_agent:
            principal = self.l10n_ae_principal_id or ""
            if not principal.strip():
                raise UserError(
                    self.env._(
                        "IBR-137-AE: disclosed-agent billing requires the principal's "
                        "TRN as Principal ID."
                    )
                )
            supplier = (
                self.partner_id.commercial_partner_id
                if self.is_self_billing
                else self.company_id.partner_id
            )
            if principal == supplier.vat:
                raise UserError(
                    self.env._(
                        "IBR-176-AE: Principal ID must differ from supplier VAT/TRN."
                    )
                )
            if not re.fullmatch(r"[0-9]{15}", principal):
                raise UserError(
                    self.env._(
                        "Principal ID requires 15 ASCII digits for the principal TRN."
                    )
                )

    def _check_l10n_ae_invoicing_period(self):
        self.ensure_one()
        if (
            self.l10n_ae_invoicing_period_start
            and self.l10n_ae_invoicing_period_end
            and self.l10n_ae_invoicing_period_end < self.l10n_ae_invoicing_period_start
        ):
            raise UserError(
                self.env._(
                    "IBR-029: the invoicing period end must be on or after its start."
                )
            )
        if self.l10n_ae_summary and not (
            self.l10n_ae_invoicing_period_start or self.l10n_ae_invoicing_period_end
        ):
            raise UserError(
                self.env._(
                    "IBR-138-AE: a Summary invoice requires a reviewed "
                    "invoicing period before preparation."
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
            move._check_l10n_ae_invoicing_period()
            if move.l10n_ae_free_zone and not move.l10n_ae_beneficiary_id:
                raise UserError(
                    self.env._(
                        "IBR-007-AE: a Free Zone transaction requires a beneficiary "
                        "identifier."
                    )
                )
            move._check_l10n_ae_principal_id()
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
