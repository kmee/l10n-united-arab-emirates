# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from decimal import ROUND_HALF_UP, Decimal

from odoo import fields, models
from odoo.exceptions import UserError

from odoo.addons.account_edi_ubl_cii.models.account_edi_common import FloatFmt
from odoo.addons.account_edi_ubl_cii.tools.ubl_21_common import CustomerParty, TaxTotal


class AccountEdiUblUae(models.AbstractModel):
    _name = "account.edi.ubl.kmee_ae"
    _inherit = "account.edi.ubl_pint"
    _description = "UAE PINT Billing"

    def _export_invoice_filename(self, invoice):
        # The native Send placeholder still uses the legacy filename interface.
        return invoice._get_invoice_report_filename(extension="xml")

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

    def _ubl_add_invoice_period_nodes(self, vals):
        result = super()._ubl_add_invoice_period_nodes(vals)
        invoice = vals["invoice"]
        if (
            invoice.l10n_ae_invoicing_period_start
            or invoice.l10n_ae_invoicing_period_end
        ):
            vals["document_node"]["cac:InvoicePeriod"].update(
                {
                    "cbc:StartDate": {
                        "_text": fields.Date.to_string(
                            invoice.l10n_ae_invoicing_period_start
                        )
                        or None
                    },
                    "cbc:EndDate": {
                        "_text": fields.Date.to_string(
                            invoice.l10n_ae_invoicing_period_end
                        )
                        or None
                    },
                }
            )
        return result

    def _ubl_get_line_item_node_classified_tax_category_node(self, vals, tax_category):
        node = super()._ubl_get_line_item_node_classified_tax_category_node(
            vals, tax_category
        )
        if tax_category["tax_category_code"] == "E":
            # UAE E lines require the legal exemption reference, without a rate.
            node["cbc:Percent"]["_text"] = None
            node["cbc:TaxExemptionReasonCode"]["_text"] = tax_category.get(
                "tax_exemption_reason_code"
            )
            node["cbc:TaxExemptionReason"]["_text"] = tax_category.get(
                "tax_exemption_reason"
            )
        return node

    def _ubl_get_tax_category_node(self, vals, tax_category):
        node = super()._ubl_get_tax_category_node(vals, tax_category)
        if tax_category["tax_category_code"] == "E":
            # IBR-119/121-AE distinguish absent rates from an explicit zero rate.
            node["cbc:Percent"]["_text"] = None
        return node

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

    def _ubl_add_accounting_customer_party_endpoint_id_node(self, vals):
        result = super()._ubl_add_accounting_customer_party_endpoint_id_node(vals)
        destination = vals["invoice"]._l10n_ae_destination_override()
        if destination:
            vals["party_node"]["cbc:EndpointID"] = {
                "schemeID": destination["scheme"],
                "_text": destination["endpoint"],
            }
        return result

    def _ubl_add_accounting_customer_party_tax_scheme_nodes(self, vals):
        result = super()._ubl_add_accounting_customer_party_tax_scheme_nodes(vals)
        partner = vals["party_vals"]["partner"].commercial_partner_id
        if (
            vals["invoice"].l10n_ae_exports
            and partner.country_code != "AE"
            and not partner.vat
        ):
            # Odoo may store a tax identifier derived from the legal registration.
            # It does not establish the buyer VAT identity. Preserve master data;
            # explicitly supplied VAT remains subject to formal validation.
            vals["party_node"]["cac:PartyTaxScheme"] = []
        return result

    def _ubl_add_accounting_customer_party_legal_entity_nodes(self, vals):
        result = super()._ubl_add_accounting_customer_party_legal_entity_nodes(vals)
        partner = vals["party_vals"]["partner"].commercial_partner_id
        destination = vals["invoice"]._l10n_ae_destination_override()
        if (
            partner.country_code != "AE"
            and destination
            and destination["reason"] == "export_buyer_no_peppol"
        ):
            identifier = partner._get_preferred_legal_entity_identifier_vals()
            for node in vals["party_node"]["cac:PartyLegalEntity"]:
                if node["cbc:CompanyID"].get("_text"):
                    # The original national identifier type is not a UAE license.
                    # IBR-180 requires a type for the predefined 0235 destination.
                    node["cbc:CompanyID"].update(
                        schemeAgencyID=identifier.get("key"),
                        schemeAgencyName=partner.l10n_ae_registration_authority or None,
                    )
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
            base_line = vals["line_vals"]["base_line"]
            # Native Sales may leave the custom line description empty while
            # displaying the product's name. Preserve that actual item label.
            vals["item_node"]["cbc:Description"] = {
                "_text": base_line.get("name") or base_line["product_id"].display_name
            }
        return result

    def _ubl_add_line_item_commodity_classification_nodes(self, vals):
        # Extend the public Odoo20 hook, as the LGPL-3 Odoo PR262200 does.
        nodes = super()._ubl_add_line_item_commodity_classification_nodes(vals)
        product = vals["line_vals"]["base_line"]["product_id"]
        if not product:
            return nodes
        classification = product._l10n_ae_item_classification()
        nodes.append({"cbc:CommodityCode": {"_text": classification["type"]}})
        if classification["hs_code"]:
            nodes.append(
                {
                    "cbc:ItemClassificationCode": {
                        "_text": classification["hs_code"],
                        "listID": "HS",
                    }
                }
            )
        if classification["sac_code"]:
            vals["item_node"]["cac:AdditionalItemIdentification"] = {
                "cbc:ID": {
                    "_text": classification["sac_code"],
                    "schemeID": "SAC",
                    "schemeVersionID": classification["sac_scheme_version"],
                }
            }
        return nodes

    def _ubl_add_line_price_node(self, vals, in_foreign_currency=True):
        # The pinned core infers the price from the rounded accounting base.
        # Keep its tax-inclusive/discount logic, using raw fiscal details only
        # for pricing; the original base still supplies the accounting totals.
        raw_base = self._l10n_ae_raw_fiscal_base_line(vals, undiscounted=True)
        price_vals = {
            **vals,
            "line_vals": {**vals["line_vals"], "base_line": raw_base},
        }
        result = super()._ubl_add_line_price_node(price_vals, in_foreign_currency)
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
        nodes = vals["line_node"]["cac:AllowanceCharge"]
        before = len(nodes)
        self._ubl_add_line_allowance_charge_nodes_for_discount(vals)
        vals["l10n_ae_discount_node"] = nodes[-1] if len(nodes) > before else None
        return result

    def _l10n_ae_preserve_line_discount(self, vals):
        discount = vals["line_vals"]["base_line"]["discount"]
        if not discount:
            return
        raw_base = self._l10n_ae_raw_fiscal_base_line(vals, undiscounted=True)
        currency = raw_base["currency_id"]
        # Reuse native tax arithmetic for the undiscounted net basis.
        # IBR-131-AE permits an exact percentage allowance; cent rounding here
        # can violate IBR-147-AE against the preserved high-precision price.
        basis = Decimal(
            str(
                self.env["account.tax"]._get_gross_total_without_tax(
                    raw_base, vals["company"]
                )
            )
        )
        amount = basis * Decimal(str(discount)) / Decimal(100)
        node = self._ubl_get_line_allowance_charge_discount_node(
            vals,
            {
                "currency": currency,
                "percent": discount,
                "is_charge": amount < 0,
                "amount": float(amount),
                "base_amount": float(basis),
            },
        )
        # The public builder's constraints require numeric FloatFmt values.
        # Give each native value its supplied precision instead of currency cents.
        for key, value in [("cbc:Amount", amount), ("cbc:BaseAmount", basis)]:
            node[key]["_text"] = FloatFmt(
                abs(value), max_dp=max(2, -value.as_tuple().exponent)
            )
        # Core has already computed the line extension from its rounded amounts.
        # Preserve that accounting total, then replace only its percentage discount.
        original = vals["l10n_ae_discount_node"]
        if original is not None:
            original.clear()
            original.update(node)
        else:
            vals["line_node"]["cac:AllowanceCharge"].append(node)

    def _l10n_ae_raw_fiscal_base_line(self, vals, undiscounted=False):
        # Recompute on an export-only copy with native unrounded tax arithmetic.
        # This does not change the company's policy or any accounting record.
        raw_base = dict(vals["line_vals"]["base_line"])
        if undiscounted and raw_base["discount"] == 100:
            # The core's zero-discount-factor fallback uses price_unit directly,
            # which still includes VAT. Recover the original net price through
            # native tax calculation on this copy; never change the actual line.
            raw_base["discount"] = 0
        self.env["account.tax"]._add_tax_details_in_base_line(
            raw_base, vals["company"], rounding_method="round_globally"
        )
        return raw_base

    def _l10n_ae_add_line_aed_amounts(self, vals):
        details = self._l10n_ae_raw_fiscal_base_line(vals)["tax_details"]
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
        self._l10n_ae_preserve_line_discount(vals)
        self._l10n_ae_add_line_aed_amounts(vals)
        return result

    def _ubl_add_credit_note_line_node(self, vals):
        result = super()._ubl_add_credit_note_line_node(vals)
        self._l10n_ae_preserve_line_discount(vals)
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
                fiscal_vat = vat.quantize(quantum, rounding=ROUND_HALF_UP)
                fiscal_gross = gross.quantize(quantum, rounding=ROUND_HALF_UP)
                accounting_vat = Decimal(
                    str(-invoice.direction_sign * invoice.amount_tax_signed)
                ).quantize(quantum, rounding=ROUND_HALF_UP)
                accounting_gross = Decimal(
                    str(-invoice.direction_sign * invoice.amount_total_signed)
                ).quantize(quantum, rounding=ROUND_HALF_UP)
                if (fiscal_vat, fiscal_gross) != (accounting_vat, accounting_gross):
                    raise UserError(
                        self.env._(
                            "The fiscal AED amounts do not match accounting. "
                            "Fiscal VAT: %(fiscal_vat)s AED; accounting VAT: "
                            "%(accounting_vat)s AED. Fiscal gross: %(fiscal_gross)s "
                            "AED; accounting gross: %(accounting_gross)s AED. "
                            "Review the exchange rate and rounding before "
                            "preparing the e-invoice.",
                            fiscal_vat=str(fiscal_vat),
                            accounting_vat=str(accounting_vat),
                            fiscal_gross=str(fiscal_gross),
                            accounting_gross=str(accounting_gross),
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
                # The public core template has generic classifications, but not
                # the UAE commodity type or SAC. Extend only this document's
                # item template in UBL schema order; never mutate core globals.
                item_template = {}
                for tag, node in template[name]["cac:Item"].items():
                    if tag == "cac:CommodityClassification":
                        item_template["cac:AdditionalItemIdentification"] = {
                            "cbc:ID": {}
                        }
                        node = {"cbc:CommodityCode": {}, **node}
                    item_template[tag] = node
                template[name] = {
                    **template[name],
                    "cac:Item": item_template,
                    "cac:ItemPriceExtension": {
                        "cbc:Amount": {},
                        "cac:TaxTotal": TaxTotal,
                    },
                }
        vals["document_node"]["_template"] = template
        vals["document_node"]["cbc:ProfileExecutionID"] = {
            "_text": invoice._l10n_ae_transaction_flags()
        }
        if invoice.l10n_ae_disclosed_agent:
            document["cac:SellerSupplierParty"] = {
                "cac:Party": {
                    "cac:PartyIdentification": [
                        {"cbc:ID": {"_text": invoice.l10n_ae_principal_id}}
                    ]
                }
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
