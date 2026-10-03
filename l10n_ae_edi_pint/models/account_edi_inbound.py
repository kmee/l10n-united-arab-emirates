# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import models
from odoo.exceptions import UserError


class AccountEdiUblUaeInbound(models.AbstractModel):
    _name = "account.edi.ubl.kmee_ae.inbound"
    _inherit = "account.edi.xml.ubl_21"
    _description = "UAE PINT Import with Reviewed Master Data"

    def _import_retrieve_partner_vals(self, tree, role):
        result = super()._import_retrieve_partner_vals(tree, role)
        party_path = f"./{{*}}{role}Party/{{*}}Party"
        # A CompanyID under PartyLegalEntity is a license, never a VAT TRN.
        vat_nodes = tree.xpath(
            f"./*[local-name()='{role}Party']/*[local-name()='Party']"
            "/*[local-name()='PartyTaxScheme'][*[local-name()='TaxScheme']"
            "/*[local-name()='ID']='VAT']/*[local-name()='CompanyID']"
        )
        result["vat"] = vat_nodes[0].text if len(vat_nodes) == 1 else False
        legal = tree.find(party_path + "/{*}PartyLegalEntity/{*}CompanyID")
        if legal is not None:
            result["ae_legal_identity"] = (
                legal.text,
                legal.get("schemeAgencyID"),
                legal.get("schemeAgencyName"),
            )
        endpoint = tree.find(f"./{{*}}{role}Party/{{*}}Party/{{*}}EndpointID")
        if endpoint is not None:
            result["routing_identifier"] = f"{endpoint.get('schemeID')}:{endpoint.text}"
        return result

    def _import_partner(self, company_id, name, phone, email, vat, **kwargs):
        # pylint: disable=method-required-super
        domain = [
            ("parent_id", "=", False),
            ("company_id", "in", [False, company_id.id]),
        ]
        routing = kwargs.get("routing_identifier")
        if routing:
            scheme, _, endpoint = routing.partition(":")
            domain += [
                ("routing_scheme", "=", scheme),
                ("routing_endpoint", "=", endpoint),
            ]
        if vat:
            partner = self.env["res.partner"].search(
                domain + [("vat", "=", vat)], limit=2
            )
        elif routing and (legal := kwargs.get("ae_legal_identity")) and all(legal):
            number, registration_type, authority = legal
            partner = (
                self.env["res.partner"]
                .search(
                    domain
                    + [
                        ("l10n_ae_registration_type", "=", registration_type),
                        ("l10n_ae_registration_authority", "=", authority),
                    ]
                )
                .filtered(
                    lambda record: record._l10n_ae_legal_registration_number() == number
                )
            )
        else:
            partner = self.env["res.partner"]
        if len(partner) != 1:
            raise UserError(
                self.env._("The received supplier requires master data review.")
            )
        return partner, []

    def _import_partner_bank(self, invoice, bank_details):
        # pylint: disable=method-required-super
        return []

    def _import_attachments(self, invoice, tree):
        # pylint: disable=method-required-super
        return self.env["ir.attachment"]

    def _get_tax_nodes(self, tree):
        # pylint: disable=method-required-super
        # UAE E/O categories can omit Percent. Keep category evidence rather than
        # treating a missing percentage as an absent tax classification.
        return tree.findall("./{*}Item/{*}ClassifiedTaxCategory")

    def _retrieve_taxes(self, record, line_values, tax_type, tax_exigibility=None):
        # pylint: disable=method-required-super
        taxes = self.env["account.tax"]
        for node in line_values.pop("tax_nodes"):
            category = node.findtext("./{*}ID")
            percentage = node.findtext("./{*}Percent")
            if percentage is None and category not in ("E", "O"):
                raise UserError(
                    self.env._("The received tax percentage requires review.")
                )
            candidates = (
                self.env["account.tax"]
                .search(
                    [
                        ("company_id", "=", record.company_id.id),
                        ("amount_type", "=", "percent"),
                        ("amount", "=", float(percentage or 0)),
                        ("type_tax_use", "=", tax_type),
                        ("price_include", "=", False),
                    ]
                )
                .filtered(
                    lambda tax, code=category: (
                        (tax.ubl_cii_tax_category_code or "S") == code
                    )
                )
            )
            default = record.company_id.account_purchase_tax_id & candidates
            if len(default) == 1:
                candidates = default
            if len(candidates) != 1:
                raise UserError(
                    self.env._("The received tax category requires master data review.")
                )
            taxes |= candidates
        if not taxes:
            raise UserError(
                self.env._("The received tax category requires master data review.")
            )
        return taxes.ids, []
