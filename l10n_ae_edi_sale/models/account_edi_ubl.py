# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import models


class AccountEdiUblUae(models.AbstractModel):
    _inherit = "account.edi.ubl.kmee_ae"

    def _ubl_add_billing_reference_nodes(self, vals):
        result = super()._ubl_add_billing_reference_nodes(vals)
        if self._is_document(vals, "invoice"):
            references = vals["invoice"]._l10n_ae_advance_references()
            if references:
                vals["document_node"]["cac:BillingReference"] = [
                    {
                        "cac:InvoiceDocumentReference": {
                            "cbc:ID": {"_text": ref["number"]},
                            "cbc:IssueDate": {"_text": ref["issue_date"]},
                        }
                    }
                    for ref in references
                ]
        return result

    def _fill_document_values(self, vals):
        result = super()._fill_document_values(vals)
        if self._is_document(vals, "invoice"):
            # The pinned core omits this hook for normal invoices too.
            self._ubl_add_billing_reference_nodes(vals)
        return result
