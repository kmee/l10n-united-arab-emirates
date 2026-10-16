# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
import math
from datetime import date

from psycopg2.errors import UniqueViolation

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, ConcurrencyError, UserError

from .account_move import UAE_TRANSACTION_FLAG_FIELDS

BILLING_HEADER_FIELDS = {
    "move_type",
    "partner_id",
    "journal_id",
    "currency_id",
    "invoice_date",
    "taxable_supply_date",
    "date",
    "invoice_date_due",
    "invoice_payment_term_id",
    "partner_bank_id",
    "partner_shipping_id",
    "ref",
    "narration",
    "invoice_origin",
    "payment_reference",
    "invoice_incoterm_id",
    "reversed_entry_id",
    "l10n_ae_binding_id",
    "l10n_ae_document_category",
    "l10n_ae_beneficiary_id",
    "l10n_ae_principal_id",
    "l10n_ae_credit_reason",
    "l10n_ae_selfbilling_agreement_id",
    "l10n_ae_tax_exchange_rate",
    "l10n_ae_tax_exchange_date",
    "l10n_ae_tax_exchange_source",
    "l10n_ae_invoicing_period_start",
    "l10n_ae_invoicing_period_end",
    "l10n_ae_billing_frequency_code",
    "l10n_ae_billing_frequency_note",
    "l10n_ae_buyer_not_implemented",
    "l10n_ae_export_buyer_no_peppol",
    "l10n_ae_buyer_onboarding_review_date",
    "l10n_ae_buyer_onboarding_review_reference",
    *UAE_TRANSACTION_FLAG_FIELDS,
}
BILLING_LINE_FIELDS = {
    "product_id",
    "name",
    "quantity",
    "price_unit",
    "discount",
    "account_id",
    "product_uom_id",
}
BILLING_CREDIT_FIELDS = {
    "invoice_id",
    "number",
    "issue_date",
    "allocated_net",
    "allocated_tax",
    "review_evidence",
}


class UaeEdiBilling(models.AbstractModel):
    _name = "l10n_ae.edi.billing"
    _description = "UAE Billing Integration"

    @api.model
    def submit_document(
        self,
        company_id,
        environment,
        process,
        origin_system,
        origin_document_id,
        invoice,
    ):
        """Create/post/prepare atomically; controlled dispatch is a separate worker."""
        identity = self._billing_identity(
            company_id, environment, process, origin_system, origin_document_id
        )
        principal = self.env.user.with_user(self.env.uid).sudo(False)
        if not principal.has_group("account.group_account_invoice"):
            raise AccessError(
                self.env._("Native invoice create/write permissions are required.")
            )
        if environment != "sandbox":
            raise UserError(
                self.env._("Only controlled sandbox submission is available.")
            )
        # Semantic input cannot smuggle ORM defaults or accounting bypass flags.
        # pylint: disable=context-overridden
        billing = self.sudo(False).with_context(
            {"allowed_company_ids": [company_id], "lang": self.env.lang}
        )
        moves = billing.env["account.move"].with_company(company_id)
        moves.check_access("create")
        values = self._billing_invoice_values(moves, invoice, process)
        request_hash = hashlib.sha256(
            json.dumps(
                self._billing_fingerprint_value(invoice),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        receipts = billing.env["l10n_ae.edi.billing.request"]
        receipt = receipts.search(
            [(field, "=", value) for field, value in identity.items()], limit=1
        )
        if receipt:
            if receipt.request_hash != request_hash:
                raise UserError(
                    self.env._(
                        "This billing origin conflicts with the original "
                        "request content."
                    )
                )
            receipt.move_id.check_access("write")
            return billing._billing_submission_result(identity, receipt)
        if self._billing_issue(identity, None) or moves.search_count(
            [
                ("company_id", "=", company_id),
                ("l10n_ae_origin_system", "=", origin_system),
                ("l10n_ae_origin_document_id", "=", origin_document_id),
            ]
        ):
            raise UserError(
                self.env._(
                    "This billing origin already belongs to an "
                    "independently existing invoice."
                )
            )
        try:
            with self.env.cr.savepoint():
                move = moves.create(
                    {
                        **values,
                        "state": "draft",
                        "company_id": company_id,
                        "l10n_ae_origin_system": origin_system,
                        "l10n_ae_origin_document_id": origin_document_id,
                    }
                )
                move.check_access("write")
                move.action_post()
                billing.prepare_document(**identity, move_id=move.id)
                receipt = receipts._record_prepared_request(
                    identity, request_hash, move
                )
                return billing._billing_submission_result(identity, receipt)
        except UniqueViolation as error:
            transmissions = billing.env["l10n_ae.edi.transmission"]
            identity_constraints = {
                moves._l10n_ae_origin_identity_unique.full_name(moves),
                receipts._origin_identity_unique.full_name(receipts),
                transmissions._business_revision_unique.full_name(transmissions),
            }
            if error.diag.constraint_name not in identity_constraints:
                raise
            # A competing commit can be outside this repeatable-read snapshot.
            # Let Odoo retry the entire request, including native accounting.
            raise ConcurrencyError(
                self.env._("Retry the concurrent original billing request.")
            ) from error

    def _billing_fingerprint_value(self, value):
        if isinstance(value, dict):
            return {
                key: self._billing_fingerprint_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._billing_fingerprint_value(item) for item in value]
        if type(value) is float and value.is_integer():
            return int(value)
        return value

    def _billing_checked_values(self, model, values, allowed):
        if not isinstance(values, dict) or set(values) - allowed:
            raise UserError(self.env._("Supply only documented billing input fields."))
        result = {}
        for name, value in values.items():
            field = model._fields[name]
            valid = False
            if field.type == "many2one":
                valid = type(value) is int and 1 <= value <= 2147483647
            elif field.type in {"float", "monetary", "integer"}:
                try:
                    valid = type(value) in {int, float} and math.isfinite(value)
                except OverflowError:
                    valid = False
            elif field.type == "boolean":
                valid = type(value) is bool
            elif field.type == "date":
                try:
                    valid = (
                        isinstance(value, str)
                        and date.fromisoformat(value).isoformat() == value
                    )
                except ValueError:
                    valid = False
            elif field.type == "selection":
                valid = (
                    isinstance(value, str) and value in field.get_values(model.env)
                ) or (
                    name == "l10n_ae_billing_frequency_code"
                    and (value is False or value is None)
                )
            elif field.type in {"char", "text", "html"}:
                valid = isinstance(value, str)
            if not valid:
                raise UserError(self.env._("Invalid typed billing field: %s", name))
            result[name] = value
        return result

    def _billing_invoice_values(self, moves, invoice, process):
        required = {
            "move_type",
            "partner_id",
            "journal_id",
            "currency_id",
            "invoice_date",
            "l10n_ae_binding_id",
            "lines",
        }
        if (
            not isinstance(invoice, dict)
            or not required <= set(invoice)
            or set(invoice) - (BILLING_HEADER_FIELDS | {"lines", "credit_references"})
        ):
            raise UserError(
                self.env._("Supply the documented invoice identity and billing lines.")
            )
        allowed_types = (
            {"out_invoice", "out_refund"}
            if process == "billing"
            else {"in_invoice", "in_refund"}
        )
        if (
            not isinstance(invoice["move_type"], str)
            or invoice["move_type"] not in allowed_types
        ):
            raise UserError(
                self.env._(
                    "The accounting invoice orientation does not match "
                    "the billing process."
                )
            )
        values = self._billing_checked_values(
            moves,
            {
                key: value
                for key, value in invoice.items()
                if key in BILLING_HEADER_FIELDS
            },
            BILLING_HEADER_FIELDS,
        )
        lines = invoice["lines"]
        if not isinstance(lines, list) or not lines:
            raise UserError(
                self.env._("Supply a nonempty list of billing invoice lines.")
            )
        commands = []
        for line in lines:
            if (
                not isinstance(line, dict)
                or not {"name", "quantity", "price_unit", "tax_ids"} <= set(line)
                or set(line) - (BILLING_LINE_FIELDS | {"tax_ids"})
            ):
                raise UserError(
                    self.env._(
                        "Supply documented native billing line fields "
                        "and explicit taxes."
                    )
                )
            line_values = self._billing_checked_values(
                self.env["account.move.line"],
                {key: value for key, value in line.items() if key != "tax_ids"},
                BILLING_LINE_FIELDS,
            )
            taxes = line["tax_ids"]
            if not isinstance(taxes, list) or any(
                type(value) is not int or not 1 <= value <= 2147483647
                for value in taxes
            ):
                raise UserError(
                    self.env._("Supply existing tax IDs, without ORM commands.")
                )
            line_values["tax_ids"] = [Command.set(taxes)]
            commands.append(Command.create(line_values))
        values["invoice_line_ids"] = commands
        if "credit_references" in invoice:
            references = invoice["credit_references"]
            if not isinstance(references, list):
                raise UserError(
                    self.env._("Supply structured credit reference objects.")
                )
            values["l10n_ae_credit_reference_ids"] = [
                Command.create(
                    self._billing_checked_values(
                        self.env["l10n_ae.credit.reference"], ref, BILLING_CREDIT_FIELDS
                    )
                )
                for ref in references
            ]
        return values

    def _billing_submission_result(self, identity, receipt):
        move = receipt.move_id
        move.check_access("read")
        return {
            **self.get_document(**identity),
            "request_sha256": receipt.request_hash,
            "accounting": {
                "move_id": move.id,
                "number": move.name,
                "state": move.state,
                "move_type": move.move_type,
                "currency_id": move.currency_id.id,
                "amount_untaxed": move.amount_untaxed,
                "amount_tax": move.amount_tax,
                "amount_total": move.amount_total,
            },
        }

    @api.model
    def prepare_document(
        self,
        company_id,
        environment,
        process,
        origin_system,
        origin_document_id,
        move_id,
    ):
        """Prepare an existing posted sandbox document through its native action."""
        identity = self._billing_identity(
            company_id, environment, process, origin_system, origin_document_id
        )
        principal = self.env.user.with_user(self.env.uid).sudo(False)
        if not principal.has_group("account.group_account_invoice"):
            raise AccessError(
                self.env._("Native invoice write permissions are required.")
            )
        if type(move_id) is not int or not 1 <= move_id <= 2147483647:
            raise UserError(self.env._("A positive integer invoice ID is required."))
        if environment != "sandbox":
            raise UserError(
                self.env._("Only controlled sandbox preparation is available.")
            )
        # Search through the caller's ordinary access rules and bind the lookup
        # to the already-authorized company. A missing or foreign move has one
        # response, so this API does not reveal whether another company's ID exists.
        move = (
            self.env["account.move"]
            .sudo(False)
            .search([("id", "=", move_id), ("company_id", "=", company_id)], limit=1)
        )
        if not move:
            raise AccessError(
                self.env._("The invoice is unavailable to the authorized company.")
            )
        move.check_access("write")
        if move.state != "posted":
            raise UserError(self.env._("Post the invoice before billing preparation."))
        if (
            move.l10n_ae_origin_system != origin_system
            or move.l10n_ae_origin_document_id != origin_document_id
        ):
            raise UserError(self.env._("The invoice's billing origin does not match."))
        previous = self._billing_issue(identity, None)
        if previous and previous.move_id.id != move_id:
            raise UserError(
                self.env._("This billing origin already belongs to another invoice.")
            )
        with self.env.cr.savepoint():
            move.action_l10n_ae_prepare()
            issue = move.l10n_ae_transmission_id
            if (
                any(
                    issue[field] != value
                    for field, value in identity.items()
                    if field != "company_id"
                )
                or issue.company_id.id != company_id
            ):
                raise UserError(
                    self.env._(
                        "The prepared issue does not match the requested "
                        "billing identity."
                    )
                )
            return self.get_document(**identity)

    def _billing_identity(
        self, company_id, environment, process, origin_system, origin_document_id
    ):
        if type(company_id) is not int or not 1 <= company_id <= 2147483647:
            raise UserError(self.env._("A positive integer company ID is required."))
        principal = self.env.user.with_user(self.env.uid).sudo(False).exists()
        if (
            not principal
            or not principal.active
            or not (
                principal.has_group("account.group_account_invoice")
                or principal.has_group("account.group_account_readonly")
                or principal.has_group("l10n_ae_edi.group_billing_reader")
            )
            or company_id not in principal.company_ids.ids
            or company_id not in self.env.companies.ids
        ):
            raise AccessError(
                self.env._("An authorized active billing account is required.")
            )
        if environment not in ("sandbox", "production") or process not in (
            "billing",
            "selfbilling",
        ):
            raise UserError(self.env._("Select a valid UAE environment and process."))
        for value, maximum in ((origin_system, 128), (origin_document_id, 256)):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > maximum
                or value != value.strip()
                or any(
                    ord(character) < 32 or ord(character) == 127 for character in value
                )
            ):
                raise UserError(
                    self.env._("Supply a bounded, unmodified billing origin identity.")
                )
        return {
            "company_id": company_id,
            "environment": environment,
            "process": process,
            "origin_system": origin_system,
            "origin_document_id": origin_document_id,
        }

    def _billing_issue(self, identity, revision, required=False):
        if (revision is None and required) or (
            revision is not None
            and (type(revision) is not int or not 1 <= revision <= 2147483647)
        ):
            raise UserError(
                self.env._("A positive integer issue revision is required.")
            )
        domain = [(field, "=", value) for field, value in identity.items()]
        if revision is not None:
            domain.append(("revision", "=", revision))
        # Reapply native access even if the caller supplied a sudo recordset.
        return (
            self.env["l10n_ae.edi.transmission"]
            .sudo(False)
            .search(domain, order="revision desc", limit=1)
        )

    def _billing_document(self, issue):
        if hashlib.sha256(issue.payload.encode()).hexdigest() != issue.payload_hash:
            raise UserError(
                self.env._("Frozen UAE billing snapshot integrity check failed.")
            )
        snapshot = json.loads(issue.payload)
        return {
            "move_id": issue.move_id.id,
            "revision": issue.revision,
            "fiscal_uuid": snapshot.get("fiscal_uuid"),
            "fiscal_uuid_assignment": snapshot.get("fiscal_uuid_assignment"),
            "remote_id": issue.remote_id or None,
            "snapshot_sha256": issue.payload_hash,
            "xml_sha256": issue.xml_hash or None,
            "states": {
                "submission": issue.submission_state,
                "validation_c2": issue.validation_c2_state,
                "delivery_c3": issue.delivery_c3_state,
                "reporting_c2": issue.reporting_c2_state,
                "reporting_c3": issue.reporting_c3_state,
                "confirmations": issue.confirmation_state,
            },
            "review": {
                "dispatch_required": issue.dispatch_review_required,
                "event_required": issue.event_review_required,
                "local_correction": bool(issue.local_correction_id),
            },
        }

    @api.model
    @api.readonly
    def get_document(
        self,
        company_id,
        environment,
        process,
        origin_system,
        origin_document_id,
        revision=None,
    ):
        """Read a frozen issue by exact origin; no send or accounting side effects."""
        identity = self._billing_identity(
            company_id, environment, process, origin_system, origin_document_id
        )
        issue = self._billing_issue(identity, revision)
        return {
            "contract_version": "kmee-uae-billing-1",
            "identity": identity,
            "status": "found" if issue else "not_found",
            "document": self._billing_document(issue) if issue else None,
        }

    @api.model
    @api.readonly
    def get_document_artifact(
        self,
        company_id,
        environment,
        process,
        origin_system,
        origin_document_id,
        revision,
        artifact="pint_xml",
    ):
        """Return the exact protected XML bytes of one explicit issue revision."""
        identity = self._billing_identity(
            company_id, environment, process, origin_system, origin_document_id
        )
        if artifact != "pint_xml":
            raise UserError(
                self.env._("Only the preserved PINT XML artifact is available.")
            )
        issue = self._billing_issue(identity, revision, required=True)
        if not issue or not issue.xml_content or not issue.xml_hash:
            raise UserError(
                self.env._("The preserved PINT XML revision is unavailable.")
            )
        self._billing_document(issue)
        if hashlib.sha256(issue.xml_content.encode()).hexdigest() != issue.xml_hash:
            raise UserError(
                self.env._("Frozen UAE billing XML integrity check failed.")
            )
        return {
            "contract_version": "kmee-uae-billing-1",
            "identity": identity,
            "revision": issue.revision,
            "artifact": "pint_xml",
            "filename": f"pint-ae-{issue.move_id.id}-r{issue.revision}.xml",
            "content_type": "application/xml",
            "sha256": issue.xml_hash,
            "content": issue.xml_content,
        }

    @api.model
    @api.readonly
    def list_document_events(
        self,
        company_id,
        environment,
        process,
        origin_system,
        origin_document_id,
        revision,
        after_id=0,
        limit=100,
    ):
        """Page immutable facts within one authorized document revision."""
        identity = self._billing_identity(
            company_id, environment, process, origin_system, origin_document_id
        )
        if (
            type(after_id) is not int
            or not 0 <= after_id <= 2147483647
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise UserError(
                self.env._("Supply a valid event cursor and a limit from 1 to 100.")
            )
        issue = self._billing_issue(identity, revision, required=True)
        if not issue:
            raise UserError(
                self.env._("The frozen UAE document revision is unavailable.")
            )
        self._billing_document(issue)
        events = self.env["l10n_ae.edi.event"].sudo(False)
        domain = [("transmission_id", "=", issue.id)]
        if after_id and not events.search_count(domain + [("id", "=", after_id)]):
            raise UserError(
                self.env._(
                    "The event cursor does not belong to this document revision."
                )
            )
        window = events.search(
            domain + [("id", ">", after_id)], order="id", limit=limit + 1
        )
        page = window[:limit]
        return {
            "contract_version": "kmee-uae-billing-1",
            "identity": identity,
            "revision": issue.revision,
            "events": [
                {
                    "id": event.id,
                    "event_id": event.event_id,
                    "dimension": event.dimension,
                    "state": event.fact_state,
                    "sequence": event.sequence,
                    "occurred_at": fields.Datetime.to_string(event.occurred_at),
                    "received_at": fields.Datetime.to_string(event.received_at),
                    "sha256": event.body_hash,
                    "outcome": event.outcome,
                }
                for event in page
            ],
            "next_after_id": page[-1].id if page else after_id,
            "has_more": len(window) > limit,
        }
