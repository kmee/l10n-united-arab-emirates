# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import models

from odoo.addons.account_edi_ubl_cii.tools.ubl_21_common import CustomerParty


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

    def _fill_document_values(self, vals):
        result = super()._fill_document_values(vals)
        invoice = vals["invoice"]
        flags = (
            invoice.l10n_ae_free_zone,
            invoice.l10n_ae_deemed_supply,
            invoice.l10n_ae_margin_scheme,
            invoice.l10n_ae_summary,
            invoice.l10n_ae_continuous_supply,
            invoice.l10n_ae_disclosed_agent,
            invoice.l10n_ae_ecommerce,
            invoice.l10n_ae_exports,
        )
        vals["document_node"]["cbc:ProfileExecutionID"] = {
            "_text": "".join("1" if flag else "0" for flag in flags)
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
