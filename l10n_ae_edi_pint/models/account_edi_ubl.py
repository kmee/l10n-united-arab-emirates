# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from decimal import ROUND_HALF_UP, Decimal

from odoo import fields, models
from odoo.exceptions import UserError

from odoo.addons.account_edi_ubl_cii.tools.ubl_21_common import CustomerParty, TaxTotal


class AccountEdiUblUae(models.AbstractModel):
    _name = "account.edi.ubl.kmee_ae"
    _inherit = "account.edi.ubl_pint"
    _description = "UAE PINT Billing"

    def _export_invoice(self, invoice):
        result = super()._export_invoice(invoice)
        if isinstance(result, dict):
            # OCB's pinned API returns export values instead of XML/errors.
            self._validate_taxes(invoice.invoice_line_ids.tax_ids)
            content = self._etree_to_string(self._vals_to_etree(result))
            errors = {value for value in result["constraints"].values() if value}
            return content, errors
        return result

    def _init_invoice_export_values(self, invoice):
        vals = super()._init_invoice_export_values(invoice)
        # Pinned public 20.0 mixes new and legacy currency keys in UBL/PINT.
        # Keep this adapter local; do not patch the deployed core.
        vals["currency_id"] = vals["currency"]
        vals["company_currency"] = vals["company"].currency_id
        # OCB 20.0 still reads a removed flag; absence means normal tax nodes.
        vals.setdefault("no_party_tax_scheme", False)
        return vals

    def _ubl_add_customization_id_node(self, vals):
        result = super()._ubl_add_customization_id_node(vals)
        process = "selfbilling" if vals["invoice"].is_self_billing else "billing"
        vals["document_node"]["cbc:CustomizationID"]["_text"] = (
            f"urn:peppol:pint:{process}-1@ae-1"
        )
        return result

    def _ubl_add_profile_id_node(self, vals):
        result = super()._ubl_add_profile_id_node(vals)
        process = "selfbilling" if vals["invoice"].is_self_billing else "billing"
        vals["document_node"]["cbc:ProfileID"]["_text"] = f"urn:peppol:bis:{process}"
        return result

    def _ubl_add_tax_currency_code_node(self, vals):
        result = super()._ubl_add_tax_currency_code_node(vals)
        vals["document_node"]["cbc:TaxCurrencyCode"] = {
            "_text": "AED" if vals["currency"].name != "AED" else None
        }
        return result

    def _l10n_ae_exchange_rate(self, vals):
        if vals["currency"].name == "AED":
            return Decimal(1)
        return Decimal(vals["invoice"].l10n_ae_tax_exchange_rate)

    def _l10n_ae_decimal_text(self, value):
        text = format(value, "f")
        integer, _point, fraction = text.partition(".")
        fraction = fraction.rstrip("0")
        return integer + "." + fraction.ljust(2, "0")

    def _ubl_add_invoice_type_code_node(self, vals):
        result = super()._ubl_add_invoice_type_code_node(vals)
        if vals["invoice"].l10n_ae_document_category == "commercial":
            vals["document_node"]["cbc:InvoiceTypeCode"]["_text"] = "480"
        return result

    def _ubl_add_credit_note_type_code_node(self, vals):
        result = super()._ubl_add_credit_note_type_code_node(vals)
        if vals["invoice"].l10n_ae_document_category == "commercial":
            vals["document_node"]["cbc:CreditNoteTypeCode"]["_text"] = "81"
        return result

    def _ubl_add_billing_reference_nodes(self, vals):
        result = super()._ubl_add_billing_reference_nodes(vals)
        invoice = vals["invoice"]
        if (
            self._is_document(vals, "credit_note", "self_credit_note")
            and invoice.l10n_ae_credit_reference_ids
        ):
            vals["document_node"]["cac:BillingReference"] = [
                {
                    "cac:InvoiceDocumentReference": {
                        "cbc:ID": {"_text": ref.number},
                        "cbc:IssueDate": {
                            "_text": fields.Date.to_string(ref.issue_date)
                        },
                    }
                }
                for ref in invoice.l10n_ae_credit_reference_ids
            ]
            return result
        if (
            self._is_document(vals, "credit_note", "self_credit_note")
            and invoice.reversed_entry_id
        ):
            original = invoice.reversed_entry_id
            vals["document_node"]["cac:BillingReference"] = [
                {
                    "cac:InvoiceDocumentReference": {
                        "cbc:ID": {"_text": original.name},
                        "cbc:IssueDate": {
                            "_text": fields.Date.to_string(original.invoice_date)
                        },
                    }
                }
            ]
        return result

    def _ubl_add_party_tax_scheme_nodes(self, vals):
        result = super()._ubl_add_party_tax_scheme_nodes(vals)
        partner = vals["party_vals"]["partner"].commercial_partner_id
        if partner.country_code == "AE" and not partner.vat:
            # The generic PINT fallback may present the routing TIN as a VAT ID.
            # UAE TIN and VAT TRN are separate identities, including non-registrants.
            vals["party_node"]["cac:PartyTaxScheme"] = []
        return result

    def _ubl_add_party_legal_entity_nodes(self, vals):
        result = super()._ubl_add_party_legal_entity_nodes(vals)
        partner = vals["party_vals"]["partner"].commercial_partner_id
        if partner.country_code == "AE":
            for node in vals["party_node"]["cac:PartyLegalEntity"]:
                # VAT/endpoint identity is not a trade-license identifier.
                node["cbc:CompanyID"] = {
                    "_text": partner._l10n_ae_legal_registration_number() or None,
                    "schemeAgencyID": partner.l10n_ae_registration_type or None,
                    "schemeAgencyName": partner.l10n_ae_registration_authority or None,
                }
        return result

    def _ubl_add_line_item_name_description_nodes(self, vals):
        result = super()._ubl_add_line_item_name_description_nodes(vals)
        if not vals["item_node"].get("cbc:Description"):
            vals["item_node"]["cbc:Description"] = {
                "_text": vals["line_vals"]["base_line"].get("name")
            }
        return result

    def _ubl_add_line_price_node(self, vals, in_foreign_currency=True):
        result = super()._ubl_add_line_price_node(vals, in_foreign_currency)
        price = vals["line_node"]["cac:Price"]
        quantity = vals["line_node"].get("cbc:InvoicedQuantity") or vals[
            "line_node"
        ].get("cbc:CreditedQuantity")
        price["cbc:BaseQuantity"] = {"_text": 1, "unitCode": quantity.get("unitCode")}
        # Odoo expresses invoice discounts at line level. The price-level
        # gross/net relation therefore has a zero allowance, even with no discount.
        price["cac:AllowanceCharge"] = {
            "cbc:ChargeIndicator": {"_text": "false"},
            "cbc:Amount": {
                "_text": "0",
                "currencyID": price["cbc:PriceAmount"]["currencyID"],
            },
            "cbc:BaseAmount": dict(price["cbc:PriceAmount"]),
        }
        return result

    def _ubl_add_line_allowance_charge_nodes(self, vals):
        result = super()._ubl_add_line_allowance_charge_nodes(vals)
        self._ubl_add_line_allowance_charge_nodes_for_discount(vals)
        return result

    def _l10n_ae_add_line_aed_amounts(self, vals):
        details = vals["line_vals"]["base_line"]["tax_details"]
        rate = self._l10n_ae_exchange_rate(vals)
        gross = Decimal(str(details["raw_total_included_currency"])) * rate
        vat = (
            Decimal(str(details["raw_total_included_currency"]))
            - Decimal(str(details["raw_total_excluded_currency"]))
        ) * rate
        extension = vals["line_node"]["cac:ItemPriceExtension"] = {
            "cbc:Amount": {
                "_text": self._l10n_ae_decimal_text(gross),
                "currencyID": "AED",
            }
        }
        categories = vals["line_node"]["cac:Item"]["cac:ClassifiedTaxCategory"]
        if not categories or any(node["cbc:ID"]["_text"] != "E" for node in categories):
            extension["cac:TaxTotal"] = {
                "cbc:TaxAmount": {
                    "_text": self._l10n_ae_decimal_text(vat),
                    "currencyID": "AED",
                }
            }

    def _ubl_add_invoice_line_node(self, vals):
        result = super()._ubl_add_invoice_line_node(vals)
        self._l10n_ae_add_line_aed_amounts(vals)
        return result

    def _ubl_add_credit_note_line_node(self, vals):
        result = super()._ubl_add_credit_note_line_node(vals)
        self._l10n_ae_add_line_aed_amounts(vals)
        return result

    def _fill_document_values(self, vals):
        result = super()._fill_document_values(vals)
        invoice = vals["invoice"]
        if self._is_document(vals, "credit_note", "self_credit_note"):
            # The pinned public builder does not call its reference hook.
            self._ubl_add_billing_reference_nodes(vals)
        vals["document_node"]["cbc:UUID"] = {
            "_text": invoice.l10n_ae_fiscal_uuid or None
        }
        currency = vals["currency"].name
        document = vals["document_node"]
        document["cac:TaxTotal"] = [
            node
            for node in document["cac:TaxTotal"]
            if node["cbc:TaxAmount"]["currencyID"] == currency
        ]
        if currency != "AED":
            rate = self._l10n_ae_exchange_rate(vals)
            document["cac:TaxExchangeRate"] = {
                "cbc:SourceCurrencyCode": {"_text": currency},
                "cbc:TargetCurrencyCode": {"_text": "AED"},
                "cbc:CalculationRate": {"_text": invoice.l10n_ae_tax_exchange_rate},
            }
            vat = (
                sum(
                    Decimal(str(node["cbc:TaxAmount"]["_text"]))
                    for node in document["cac:TaxTotal"]
                )
                * rate
            )
            gross = (
                Decimal(
                    str(
                        document["cac:LegalMonetaryTotal"]["cbc:TaxInclusiveAmount"][
                            "_text"
                        ]
                    )
                )
                * rate
            )
            if invoice.company_id.currency_id.name == "AED":
                quantum = Decimal("0.01")
                expected = (
                    (gross, invoice.amount_total_signed),
                    (vat, invoice.amount_tax_signed),
                )
                for fiscal_amount, signed_accounting_amount in expected:
                    accounting_amount = Decimal(
                        str(-invoice.direction_sign * signed_accounting_amount)
                    )
                    if fiscal_amount.quantize(
                        quantum, rounding=ROUND_HALF_UP
                    ) != accounting_amount.quantize(quantum, rounding=ROUND_HALF_UP):
                        raise UserError(
                            self.env._(
                                "The fiscal AED amounts do not match accounting. "
                                "Review the exchange rate and rounding before "
                                "preparing the e-invoice."
                            )
                        )
            document["cac:TaxTotal"].append(
                {
                    "cbc:TaxAmount": {
                        "_text": format(
                            vat.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"
                        ),
                        "currencyID": "AED",
                    }
                }
            )
            document.setdefault("cac:AdditionalDocumentReference", []).append(
                {
                    "cbc:ID": {"_text": "AED"},
                    "cbc:DocumentTypeCode": {"_text": "aedtotal-incl-vat"},
                    "cbc:DocumentDescription": {
                        "_text": format(
                            gross.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"
                        )
                    },
                }
            )
        if (
            self._is_document(vals, "credit_note", "self_credit_note")
            and invoice.l10n_ae_credit_reason
        ):
            vals["document_node"]["cac:DiscrepancyResponse"] = [
                {"cbc:ResponseCode": {"_text": invoice.l10n_ae_credit_reason}}
            ]
        template = dict(vals["document_node"]["_template"])
        for name in ("cac:InvoiceLine", "cac:CreditNoteLine"):
            if name in template:
                template[name] = {
                    **template[name],
                    "cac:ItemPriceExtension": {
                        "cbc:Amount": {},
                        "cac:TaxTotal": TaxTotal,
                    },
                }
        vals["document_node"]["_template"] = template
        vals["document_node"]["cbc:ProfileExecutionID"] = {
            "_text": invoice._l10n_ae_transaction_flags()
        }
        if invoice.l10n_ae_free_zone:
            vals["document_node"]["cac:BuyerCustomerParty"] = {
                "cac:Party": {
                    "cac:PartyIdentification": [
                        {"cbc:ID": {"_text": invoice.l10n_ae_beneficiary_id}}
                    ]
                }
            }
            # The public template omits this standard UBL party. Insert it in
            # its schema position, without mutating the global core template.
            template = {}
            for name, node in vals["document_node"]["_template"].items():
                if name == "cac:SellerSupplierParty":
                    template["cac:BuyerCustomerParty"] = CustomerParty
                template[name] = node
            vals["document_node"]["_template"] = template
        return result
