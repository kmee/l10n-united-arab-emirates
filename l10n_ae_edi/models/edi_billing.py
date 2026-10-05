# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError


class UaeEdiBilling(models.AbstractModel):
    _name = "l10n_ae.edi.billing"
    _description = "UAE Billing Integration"

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
        move = self.env["account.move"].sudo(False).browse(move_id).exists()
        if not move:
            raise UserError(self.env._("The existing invoice was not found."))
        move.check_access("write")
        if move.company_id.id != company_id:
            raise AccessError(
                self.env._("The invoice is outside the authorized company.")
            )
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
