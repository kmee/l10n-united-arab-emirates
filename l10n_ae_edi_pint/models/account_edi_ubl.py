# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from decimal import ROUND_HALF_UP, Decimal
from math import inf, isfinite, nextafter

from lxml import etree

from odoo import fields, models

from odoo.addons.account_edi_ubl_cii.models.account_edi_common import FloatFmt
from odoo.addons.account_edi_ubl_cii.tools.ubl_21_common import CustomerParty, TaxTotal

from ..tools.errors import UaePintExportReview


class AccountEdiUblUae(models.AbstractModel):
    _name = "account.edi.ubl.kmee_ae"
    _inherit = "account.edi.ubl_pint"
    _description = "UAE PINT Billing"

    def _ubl_add_issue_date_node(self, vals):
        result = super()._ubl_add_issue_date_node(vals)
        invoice = vals["invoice"]
        if invoice.taxable_supply_date and (
            invoice.taxable_supply_date != invoice.invoice_date
        ):
            vals["document_node"]["cbc:TaxPointDate"] = {
                "_text": invoice.taxable_supply_date
            }
        return result

    def _ubl_add_notes_nodes(self, vals):
        result = super()._ubl_add_notes_nodes(vals)
        invoice = vals["invoice"]
        if invoice.l10n_ae_billing_frequency_code == "OTH":
            # Public PINT already combines native terms/withholding into one
            # invoice-level Note (IBR-SR-51). Preserve that text and append
            # the reviewed cadence explanation required for Other (IBR-160-AE).
            note = vals["document_node"]["cbc:Note"]
            explanation = (invoice.l10n_ae_billing_frequency_note or "").strip()
            note["_text"] = "\n".join(
                text for text in (note.get("_text"), explanation) if text
            )
        return result

    def _ubl_default_tax_category_grouping_key(
        self, base_line, tax_data, vals, currency
    ):
        key = super()._ubl_default_tax_category_grouping_key(
            base_line, tax_data, vals, currency
        )
        if key and tax_data and key["tax_category_code"] == "N":
            # IBR-111-AE requires a positive nominal rate even though the
            # disclosed VAT is zero (IBR-108-AE). Keep native amounts intact.
            key["percent"] = tax_data["tax"].l10n_ae_edi_additional_vat_rate
        return key

    def _export_invoice_filename(self, invoice):
        # The native Send placeholder still uses the legacy filename interface.
        return invoice._get_invoice_report_filename(extension="xml")

    def _export_invoice(self, invoice):
        if any(
            not ((line.product_id.name if line.product_id else line.name) or "").strip()
            for line in invoice.invoice_line_ids
            if line.display_type in (False, "product")
        ):
            # Match the native item's name: product name when present, otherwise
            # the free-text line name. IBT-153 is mandatory; the pinned public
            # PINT constraint assumes cbc:Name exists. Return its error contract.
            return b"", {
                self.env._(
                    "IBT-153: each invoice line requires a nonblank item name. "
                    "Review the line description before preparing the invoice."
                )
            }
        result = super()._export_invoice(invoice)
        if isinstance(result, dict):
            # OCB's pinned API returns export values instead of XML/errors.
            self._validate_taxes(invoice.invoice_line_ids.tax_ids)
            content = self._etree_to_string(self._vals_to_etree(result))
            errors = {value for value in result["constraints"].values() if value}
        else:
            content, errors = result
        # Apply the published predicate to the actual core-generated item
        # categories. Fixed/withholding taxes and header charges must not be
        # mistaken for a qualifying ClassifiedTaxCategory on an invoice line.
        document = etree.fromstring(content)
        document_type = document.findtext("{*}InvoiceTypeCode") or document.findtext(
            "{*}CreditNoteTypeCode"
        )
        categories = document.findall(
            "{*}InvoiceLine/{*}Item/{*}ClassifiedTaxCategory"
        ) + document.findall("{*}CreditNoteLine/{*}Item/{*}ClassifiedTaxCategory")
        for category in categories:
            if (category.findtext("{*}ID") or "").strip() == "N":
                nominal_rate = Decimal(category.findtext("{*}Percent") or "0")
                if not nominal_rate.is_finite() or nominal_rate <= 0:
                    errors.add(
                        self.env._(
                            "IBR-111-AE: category N requires a reviewed positive "
                            "nominal VAT rate. Review the additional VAT metadata; "
                            "the native tax calculation will not be changed."
                        )
                    )
        for subtotal in document.findall("{*}TaxTotal/{*}TaxSubtotal"):
            category = subtotal.find("{*}TaxCategory")
            if category is not None and (
                (category.findtext("{*}ID") or "").strip() == "N"
                and (category.findtext("{*}TaxScheme/{*}ID") or "").strip().upper()
                == "VAT"
            ):
                vat_amount = Decimal(subtotal.findtext("{*}TaxAmount") or "0")
                if not vat_amount.is_finite() or vat_amount != 0:
                    errors.add(
                        self.env._(
                            "IBR-108-AE: category N requires zero disclosed VAT. "
                            "Review the fiscal representation and accounting "
                            "policy; existing VAT and ledger amounts will not "
                            "be zeroed automatically."
                        )
                    )
        transaction_type = document.findtext("{*}ProfileExecutionID") or ""
        if (
            len(transaction_type) == 8
            and transaction_type[6] == "1"
            and any(
                document.find("{*}Delivery/{*}DeliveryLocation/{*}Address/{*}" + name)
                is None
                for name in ("StreetName", "CityName", "CountrySubentity")
            )
        ):
            # Match IBR-142-AE on the actual public-core delivery XML. Its
            # partner selection differs for billing and self-billing.
            errors.add(
                self.env._(
                    "IBR-142-AE: e-commerce requires a delivery street, city and "
                    "state/emirate. Review the Shipping Address before preparing "
                    "the document; keep the buyer's legal address separate."
                )
            )
        if (
            len(transaction_type) == 8
            and transaction_type[2] == "1"
            and any(
                (node.text or "").strip() != "N"
                for node in (
                    document.findall(".//{*}TaxCategory/{*}ID")
                    + document.findall(".//{*}ClassifiedTaxCategory/{*}ID")
                )
            )
        ):
            errors.add(
                self.env._(
                    "IBR-116-AE: a profit margin document requires reviewed VAT "
                    "category N on every item and tax breakdown. Selecting the "
                    "scenario does not change the tax configuration or accounting."
                )
            )
        if document_type in {"380", "381"} and not any(
            (category.findtext("{*}ID") or "").strip() not in {"E", "O"}
            for category in categories
        ):
            errors.add(
                self.env._(
                    "IBR-151-AE: a tax invoice (380) or tax credit note (381) "
                    "requires at least one line VAT category other than E or O. "
                    "Review the document category before preparation; "
                    "it will not be changed automatically."
                )
            )
        return content, errors

    def _init_invoice_export_values(self, invoice):
        vals = super()._init_invoice_export_values(invoice)
        # Core copies vals for each line. Initialize once so all copies share
        # the document's local constraint accumulator.
        vals["l10n_ae_constraints"] = {}
        # Pinned public 20.0 mixes new and legacy currency keys in UBL/PINT.
        # Keep this adapter local; do not patch the deployed core.
        vals["currency_id"] = vals["currency"]
        vals["company_currency"] = vals["company"].currency_id
        # OCB 20.0 still reads a removed flag; absence means normal tax nodes.
        vals.setdefault("no_party_tax_scheme", False)
        return vals

    def _export_document_node_constraints(self, vals):
        return {
            **super()._export_document_node_constraints(vals),
            **vals["l10n_ae_constraints"],
        }

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

    def _ubl_get_partner_address_node(self, vals, partner):
        node = super()._ubl_get_partner_address_node(vals, partner)
        if partner.country_code == "AE":
            node["cbc:CountrySubentity"]["_text"] = partner._l10n_ae_country_subentity()
        return node

    def _ubl_add_tax_currency_code_node(self, vals):
        result = super()._ubl_add_tax_currency_code_node(vals)
        vals["document_node"]["cbc:TaxCurrencyCode"] = {
            "_text": "AED" if vals["currency"].name != "AED" else None
        }
        return result

    def _ubl_add_payment_means_nodes(self, vals):
        result = super()._ubl_add_payment_means_nodes(vals)
        account_builder = (
            self._ubl_get_payment_means_payee_financial_account_node_from_partner_bank
        )
        try:
            instruction = vals["invoice"]._l10n_ae_payment_means()
        except UaePintExportReview as error:
            vals["l10n_ae_constraints"]["uae-payment-means"] = str(error)
            return result
        if instruction:
            vals["document_node"]["cac:PaymentMeans"] = [
                {
                    "cbc:PaymentMeansCode": {
                        "_text": instruction["code"],
                        "name": instruction["name"],
                    },
                    "cbc:PaymentID": {"_text": instruction["payment_reference"]},
                }
            ]
            if instruction["code"] == "30":
                vals["document_node"]["cac:PaymentMeans"][0][
                    "cac:PayeeFinancialAccount"
                ] = account_builder(vals, vals["invoice"].partner_bank_id)
        return result

    def _ubl_add_invoice_period_nodes(self, vals):
        result = super()._ubl_add_invoice_period_nodes(vals)
        invoice = vals["invoice"]
        period_start, period_end, _origin = invoice._l10n_ae_invoicing_period_dates()
        if period_start or period_end:
            vals["document_node"]["cac:InvoicePeriod"].update(
                {
                    "cbc:StartDate": {
                        "_text": fields.Date.to_string(period_start) or None
                    },
                    "cbc:EndDate": {"_text": fields.Date.to_string(period_end) or None},
                }
            )
        if invoice.l10n_ae_billing_frequency_code:
            vals["document_node"]["cac:InvoicePeriod"]["cbc:DescriptionCode"] = {
                "_text": invoice.l10n_ae_billing_frequency_code
            }
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
        return Decimal(vals["invoice"]._l10n_ae_tax_exchange()["rate"])

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
        if partner.country_code == "AE" or (
            partner.routing_scheme == "0235"
            and partner.routing_endpoint
            and partner.l10n_ae_registration_type
            and partner._l10n_ae_legal_registration_number()
        ):
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
        # IBR-188-AE restricts this document's scheme to HS. Optional native
        # localizations can contribute CPV/UNSPSC/KPD here; keep their product
        # data and generic exporters intact, filtering only this UAE node list.
        nodes[:] = [
            node
            for node in nodes
            if "cbc:ItemClassificationCode" not in node
            or node["cbc:ItemClassificationCode"].get("listID") == "HS"
        ]
        product = vals["line_vals"]["base_line"]["product_id"]
        if not product:
            return nodes
        classification = product._l10n_ae_item_classification()
        nodes.append({"cbc:CommodityCode": {"_text": classification["type"]}})
        if classification["hs_code"]:
            # The reviewed UAE value also owns the frozen fiscal snapshot.
            # Replace native HS nodes in this exporter without changing Intrastat.
            nodes[:] = [
                node
                for node in nodes
                if "cbc:ItemClassificationCode" not in node
                or node["cbc:ItemClassificationCode"].get("listID") != "HS"
            ]
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

    def _ubl_add_line_item_classified_tax_category_nodes(
        self, vals, in_foreign_currency=True
    ):
        result = super()._ubl_add_line_item_classified_tax_category_nodes(
            vals, in_foreign_currency=in_foreign_currency
        )
        item = vals["item_node"]
        if not any(
            category["cbc:ID"]["_text"] == "AE"
            for category in item["cac:ClassifiedTaxCategory"]
        ):
            return result
        base_line = vals["line_vals"]["base_line"]
        line = base_line["record"]
        facts = line._l10n_ae_item_classification()
        nature = facts.get("rcm_nature_code")
        if not nature:
            vals["l10n_ae_constraints"]["ibr-166-ae"] = self.env._(
                "IBR-166-AE: select the reviewed UAE reverse-charge nature "
                "before preparing this AE item."
            )
            return result
        # Inspect the category already computed by the core; no second tax
        # calculation, tax-name inference or NatureCode per tax-category row.
        classifications = item["cac:CommodityClassification"]
        classification = next(
            (node for node in classifications if "cbc:CommodityCode" in node), None
        )
        if classification is None:
            classification = {}
            classifications.append(classification)
        classification["cbc:NatureCode"] = {"_text": nature}
        if facts.get("rcm_standard_id"):
            item["cac:StandardItemIdentification"] = {
                "cbc:ID": {"_text": facts["rcm_standard_id"], "schemeID": "0160"}
            }
        return result

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
        basis = self._l10n_ae_computed_decimal(
            self.env["account.tax"]._get_gross_total_without_tax(
                raw_base, vals["company"]
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
        # Replace its percentage discount before the optional global line rebuild.
        original = vals["l10n_ae_discount_node"]
        if original is not None:
            original.clear()
            original.update(node)
        else:
            vals["line_node"]["cac:AllowanceCharge"].append(node)

    def _l10n_ae_preserve_global_line_extension(self, vals):
        if vals["company"].tax_calculation_rounding_method != "round_globally":
            return
        # UAE IBR-147-AE allows raw line extensions; IBR-CO-10 rounds their
        # sum at the document boundary. Core's cent-rounding before summation
        # otherwise loses source money on fractional multi-line invoices.
        # Use the same export-only native net basis as the price/discount,
        # before PINT's intermediate six-place rounding. Consume the final
        # serialized allowance/charge nodes, including the exact AE discount.
        raw_base = self._l10n_ae_raw_fiscal_base_line(vals, undiscounted=True)
        amount = self._l10n_ae_computed_decimal(
            self.env["account.tax"]._get_gross_total_without_tax(
                raw_base, vals["company"]
            )
        )
        line = vals["line_node"]
        for adjustment in line["cac:AllowanceCharge"]:
            sign = 1 if adjustment["cbc:ChargeIndicator"]["_text"] == "true" else -1
            amount += sign * Decimal(str(adjustment["cbc:Amount"]["_text"]))
        line["cbc:LineExtensionAmount"]["_text"] = FloatFmt(
            amount, max_dp=max(2, -amount.as_tuple().exponent)
        )

    def _ubl_add_legal_monetary_total_line_extension_amount_node(
        self, vals, in_foreign_currency=True
    ):
        result = super()._ubl_add_legal_monetary_total_line_extension_amount_node(
            vals, in_foreign_currency
        )
        if (
            in_foreign_currency
            and vals["company"].tax_calculation_rounding_method == "round_globally"
        ):
            currency = vals["currency_id"]
            node = vals["legal_monetary_total_node"]["cbc:LineExtensionAmount"]
            # Sum the actual emitted values as decimals before the sole source
            # header rounding; retain the native accounting-currency path.
            amount = sum(
                (
                    Decimal(str(line["cbc:LineExtensionAmount"]["_text"]))
                    for key in (
                        "cac:InvoiceLine",
                        "cac:CreditNoteLine",
                        "cac:DebitNoteLine",
                    )
                    for line in vals["document_node"].get(key, [])
                ),
                Decimal(0),
            )
            node["_text"] = FloatFmt(
                currency.round(float(amount)), min_dp=currency.decimal_places
            )
        return result

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

    @staticmethod
    def _l10n_ae_computed_decimal(value):
        # Native tax results are binary64 intermediates, not exact decimal
        # inputs. Choose the shortest decimal that maps to this float or one
        # adjacent representable float. This removes arithmetic tails without
        # an absolute decimal-place cutoff (small/high-precision values survive).
        # Never apply this boundary to entered prices, quantities or quote text.
        if not isfinite(value):
            raise ValueError("Non-finite native fiscal amount")
        raw = Decimal(str(value))
        if not value:
            return raw
        neighbours = (nextafter(value, -inf), value, nextafter(value, inf))
        for digits in range(1, 18):
            scale = Decimal(1).scaleb(raw.adjusted() - digits + 1)
            candidate = raw.quantize(scale)
            represented = float(candidate)
            # A finite endpoint's nextafter neighbour can be infinity. Never
            # admit an overflowing decimal candidate through that neighbour.
            if isfinite(represented) and represented in neighbours:
                return candidate
        return raw

    def _l10n_ae_add_line_aed_amounts(self, vals):
        details = self._l10n_ae_raw_fiscal_base_line(vals)["tax_details"]
        rate = self._l10n_ae_exchange_rate(vals)
        included = self._l10n_ae_computed_decimal(
            details["raw_total_included_currency"]
        )
        excluded = self._l10n_ae_computed_decimal(
            details["raw_total_excluded_currency"]
        )
        gross = included * rate
        vat = (included - excluded) * rate
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
        self._l10n_ae_preserve_global_line_extension(vals)
        self._l10n_ae_add_line_aed_amounts(vals)
        return result

    def _ubl_add_credit_note_line_node(self, vals):
        result = super()._ubl_add_credit_note_line_node(vals)
        self._l10n_ae_preserve_line_discount(vals)
        self._l10n_ae_preserve_global_line_extension(vals)
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
        native_aed_tax_totals = [
            node
            for node in document["cac:TaxTotal"]
            if node["cbc:TaxAmount"]["currencyID"] == "AED"
        ]
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
                "cbc:CalculationRate": {
                    "_text": invoice._l10n_ae_tax_exchange()["rate"]
                },
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
                # Native tax details already allocate the posted AED cents.
                # Validate the quote itself, rather than comparing two different
                # rounding orders and blocking otherwise valid multi-line moves.
                native_rate = Decimal(1) / Decimal(str(invoice.invoice_currency_rate))
                native_rate = native_rate.quantize(
                    Decimal("0.000001"), rounding=ROUND_HALF_UP
                )
                if rate != native_rate:
                    vals["l10n_ae_constraints"]["uae-aed-accounting"] = self.env._(
                        "The reviewed AED exchange rate does not match accounting. "
                        "Reviewed rate: %(reviewed)s AED per %(currency)s; "
                        "posted invoice rate: %(native)s AED per %(currency)s. "
                        "Review the quote or correct the accounting document "
                        "through its authorized accounting workflow before "
                        "preparing the e-invoice.",
                        reviewed=str(rate),
                        native=str(native_rate),
                        currency=currency,
                    )
                document["cac:TaxTotal"].extend(native_aed_tax_totals)
                gross = Decimal(
                    str(-invoice.direction_sign * invoice.amount_total_signed)
                )
            if (
                not native_aed_tax_totals
                or invoice.company_id.currency_id.name != "AED"
            ):
                document["cac:TaxTotal"].append(
                    {
                        "cbc:TaxAmount": {
                            "_text": format(
                                vat.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                                "f",
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
                        node = {
                            "cbc:NatureCode": {},
                            "cbc:CommodityCode": {},
                            **node,
                        }
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
