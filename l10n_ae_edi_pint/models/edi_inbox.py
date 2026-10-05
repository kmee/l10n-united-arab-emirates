# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import os
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.binary import BinaryBytes

from ..tools.validation import (
    MAX_XML_BYTES,
    OfflinePintValidator,
    UnsafeInvoiceXml,
    ValidationUnavailable,
    parse_invoice_xml,
)


class UaeEdiInbox(models.Model):
    _name = "l10n_ae.edi.inbox"
    _description = "UAE Received Document Evidence"
    _check_company_auto = True
    _rec_name = "remote_id"

    binding_id = fields.Many2one(
        "l10n_ae.edi.binding",
        string="ASP Binding",
        required=True,
        check_company=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one("res.company", required=True, index=True)
    remote_id = fields.Char(
        string="ASP Document Reference", required=True, readonly=True, index=True
    )
    original_xml = fields.Binary(
        string="Original XML", required=True, readonly=True, attachment=False
    )
    xml_hash = fields.Char(string="XML SHA-256", required=True, readonly=True)
    received_at = fields.Datetime(required=True, readonly=True)
    state = fields.Selection(
        [
            ("received", "Received"),
            ("review", "Requires Review"),
            ("processed", "Processed"),
        ],
        required=True,
        default="received",
        readonly=True,
    )
    diagnostic = fields.Text(readonly=True)
    identity_conflict = fields.Boolean(string="Remote ID Conflict", readonly=True)
    move_id = fields.Many2one(
        "account.move",
        string="Accounting Document",
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )
    processing_history_ids = fields.One2many(
        "l10n_ae.edi.inbox.review",
        "inbox_id",
        readonly=True,
    )
    credit_reference_snapshot = fields.Json(readonly=True)
    credit_reference_summary = fields.Text(
        string="Received Invoice References",
        compute="_compute_credit_reference_summary",
    )

    @api.depends("credit_reference_snapshot")
    def _compute_credit_reference_summary(self):
        for inbox in self:
            inbox.credit_reference_summary = (
                "\n".join(
                    reference["number"]
                    + (
                        f" ({reference['issue_date']})"
                        if reference["issue_date"]
                        else ""
                    )
                    for reference in inbox.credit_reference_snapshot or []
                )
                or False
            )

    _received_identity = models.UniqueIndex(
        "(binding_id, remote_id, xml_hash)",
        "These received document bytes already exist.",
    )

    def _check_operator(self, process=True):
        principal = self.env.user.exists()
        if (
            not principal
            or not principal.active
            or not (
                principal.has_group("account.group_account_manager")
                or not process
                and principal.has_group("l10n_ae_edi_pint.group_document_receiver")
            )
        ):
            raise AccessError(
                self.env._(
                    "An active authorized UAE document receiving account is required."
                )
            )

    def _check_receiving_company(self, company):
        if (
            company not in self.env.user.company_ids
            or company not in self.env.companies
        ):
            raise AccessError(
                self.env._("The received document company is not authorized.")
            )

    @api.model
    def receive_controlled_document(self, binding_id, remote_id, content):
        """Persist controlled authenticated bytes without an accounting commit."""
        self._check_operator(process=False)
        binding = self.env["l10n_ae.edi.binding"].browse(binding_id).exists()
        binding.check_access("read")
        if not binding or not binding.active or binding.environment != "sandbox":
            raise UserError(self.env._("Select an active controlled sandbox binding."))
        self._check_receiving_company(binding.company_id)
        binding._check_transport_allowed()
        if not isinstance(remote_id, str) or not 1 <= len(remote_id) <= 256:
            raise UserError(self.env._("Invalid received document identifier."))
        if not isinstance(content, bytes) or not 0 < len(content) <= MAX_XML_BYTES:
            raise UserError(self.env._("Invalid received XML size or encoding."))
        digest = hashlib.sha256(content).hexdigest()
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_binding WHERE id = %s FOR UPDATE", [binding.id]
        )
        existing = self.search(
            [
                ("binding_id", "=", binding.id),
                ("remote_id", "=", remote_id),
                ("xml_hash", "=", digest),
            ],
            limit=1,
        )
        if existing:
            return existing
        other = self.search(
            [("binding_id", "=", binding.id), ("remote_id", "=", remote_id)]
        )
        conflict = {
            "state": "review",
            "identity_conflict": True,
            "diagnostic": self.env._(
                "Received identity reused with different XML bytes."
            ),
        }
        if other:
            super(UaeEdiInbox, other.sudo()).write(conflict)
        # Caller defaults must not fabricate receipt state or accounting links.
        # pylint: disable=context-overridden
        receiver = self.sudo().with_context(
            {"allowed_company_ids": [binding.company_id.id]}
        )
        return (
            super(UaeEdiInbox, receiver)
            .create(
                {
                    "binding_id": binding.id,
                    "company_id": binding.company_id.id,
                    "remote_id": remote_id,
                    "original_xml": BinaryBytes(
                        content, filename="received-pint-ae.xml"
                    ),
                    "xml_hash": digest,
                    "received_at": fields.Datetime.now(),
                    "state": "received",
                    "diagnostic": False,
                    "identity_conflict": False,
                    "move_id": False,
                    **(conflict if other else {}),
                }
            )
            .with_env(self.env)
        )

    @api.model_create_multi
    def create(self, vals_list):
        # pylint: disable=method-required-super
        raise UserError(
            self.env._("Received UAE evidence is created by the authorized receiver.")
        )

    def write(self, vals):
        # pylint: disable=method-required-super
        raise UserError(self.env._("Received UAE evidence is protected."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_received_evidence(self):
        raise UserError(self.env._("Received UAE evidence cannot be deleted."))

    def action_process(self):
        self.ensure_one()
        self._check_operator()
        self.check_access("read")
        self._check_receiving_company(self.company_id)
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_binding WHERE id = %s FOR UPDATE",
            [self.binding_id.id],
        )
        self.env.cr.execute(
            "SELECT id FROM l10n_ae_edi_inbox WHERE id = %s FOR UPDATE", [self.id]
        )
        self.invalidate_recordset()
        if self.identity_conflict:
            return False
        if self.move_id:
            return True
        report = False
        try:
            with self.env.cr.savepoint():
                content = self.original_xml.content
                if hashlib.sha256(content).hexdigest() != self.xml_hash:
                    raise UserError(
                        self.env._("Received XML failed its integrity check.")
                    )
                tree = parse_invoice_xml(content)
                directory = os.environ.get("KMEE_UAE_NORMATIVE_DIR")
                if not directory:
                    raise UserError(
                        self.env._(
                            "Configure the offline UAE normative resource directory."
                        )
                    )
                report = OfflinePintValidator(directory).validate(content)
                # Preserve the specific cardinality diagnostic alongside the report.
                self._received_beneficiary_id(tree)
                self.env["account.move"]._l10n_ae_check_pint_report(report)
                move = self.with_company(self.company_id)._process_received_xml()
        except (UserError, UnsafeInvoiceXml, ValidationUnavailable) as error:
            super(UaeEdiInbox, self.sudo()).write(
                {"state": "review", "diagnostic": str(error)}
            )
            self._record_processing_history(
                "review", diagnostic=str(error), validation_report=report
            )
            return False
        super(UaeEdiInbox, self.sudo()).write(
            {"state": "processed", "diagnostic": False, "move_id": move.id}
        )
        self._record_processing_history(
            "processed", move=move, validation_report=report
        )
        return True

    def _record_processing_history(
        self, outcome, diagnostic=False, move=False, validation_report=False
    ):
        self.ensure_one()
        return self.env["l10n_ae.edi.inbox.review"]._record(
            self,
            outcome,
            diagnostic=diagnostic,
            move=move,
            validation_report=validation_report,
        )

    def action_open_move(self):
        self.ensure_one()
        self.check_access("read")
        if not self.move_id:
            raise UserError(self.env._("No accounting document has been linked yet."))
        self.move_id.check_access("read")
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": self.move_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def _process_received_xml(self):
        content = self.original_xml.content
        if hashlib.sha256(content).hexdigest() != self.xml_hash:
            raise UserError(self.env._("Received XML failed its integrity check."))
        tree = parse_invoice_xml(content)
        beneficiary_id = self._received_beneficiary_id(tree)
        is_credit = tree.tag == (
            "{urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2}CreditNote"
        )
        type_code = tree.findtext(
            "./{*}CreditNoteTypeCode" if is_credit else "./{*}InvoiceTypeCode"
        )
        customization = tree.findtext("./{*}CustomizationID")
        selfbilling = customization == "urn:peppol:pint:selfbilling-1@ae-1"
        supported = (
            selfbilling
            and type_code == ("261" if is_credit else "389")
            or customization == "urn:peppol:pint:billing-1@ae-1"
            and type_code in (("381", "81") if is_credit else ("380", "480"))
        )
        transaction_flags = tree.findtext("./{*}ProfileExecutionID")
        if (
            not supported
            or transaction_flags not in ("00000000", "10000000")
            or tree.findall("./{*}AllowanceCharge")
            or not is_credit
            and float(tree.findtext("./{*}LegalMonetaryTotal/{*}PrepaidAmount") or 0)
            != 0
        ):
            raise UserError(
                self.env._("This received UAE scenario requires review before import.")
            )
        currency_code = tree.findtext("./{*}DocumentCurrencyCode")
        currency = self.env["res.currency"].search(
            [("name", "=", currency_code), ("active", "=", True)], limit=1
        )
        if not currency:
            raise UserError(
                self.env._("The received document currency requires review.")
            )
        fiscal_values = self._received_fiscal_values(tree, currency_code)
        recipient_role = "AccountingSupplier" if selfbilling else "AccountingCustomer"
        endpoint = tree.find(f"./{{*}}{recipient_role}Party/{{*}}Party/{{*}}EndpointID")
        if (
            endpoint is None
            or endpoint.get("schemeID") != "0235"
            or endpoint.text != self.company_id.l10n_ae_tin
            or not self.company_id.l10n_ae_tin_confirmed
        ):
            raise UserError(
                self.env._(
                    "The received XML recipient does not match this ASP binding."
                )
            )
        self.env.cr.execute(
            "SELECT id FROM res_company WHERE id = %s FOR UPDATE", [self.company_id.id]
        )
        prior = self.env["account.move"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("l10n_ae_received_document", "=", True),
                ("l10n_ae_fiscal_uuid", "=", tree.findtext("./{*}UUID")),
            ],
            limit=2,
        )
        if prior:
            same_bytes = self.search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("move_id", "in", prior.ids),
                    ("xml_hash", "=", self.xml_hash),
                ],
                limit=1,
            )
            if len(prior) == 1 and same_bytes:
                super(UaeEdiInbox, self.sudo()).write(
                    {"credit_reference_snapshot": same_bytes.credit_reference_snapshot}
                )
                return prior
            raise UserError(
                self.env._("Received fiscal UUID reused with different document bytes.")
            )
        journal = self.env["account.journal"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("type", "=", "sale" if selfbilling else "purchase"),
                ("is_self_billing", "=", False),
            ],
            limit=2,
        )
        if len(journal) != 1:
            raise UserError(
                self.env._(
                    "Select an unambiguous accounting journal for the received "
                    "UAE document's economic role."
                )
            )
        agreement = self.env["l10n_ae.selfbilling.agreement"]
        if selfbilling:
            partner, agreement = self._resolve_received_selfbilling_agreement(tree)
        values = {
            "company_id": self.company_id.id,
            "journal_id": journal.id,
            "move_type": ("out_refund" if is_credit else "out_invoice")
            if selfbilling
            else "in_refund"
            if is_credit
            else "in_invoice",
            "l10n_ae_received_process": "selfbilling" if selfbilling else "billing",
            "l10n_ae_selfbilling_agreement_id": agreement.id,
            "l10n_ae_received_agreement_snapshot": {
                "reference": agreement.name,
                "local_role": agreement.local_role,
                "start_date": fields.Date.to_string(agreement.start_date),
                "end_date": fields.Date.to_string(agreement.end_date),
                "scope": agreement.scope,
                "onboarding_reference": agreement.onboarding_reference,
            }
            if agreement
            else False,
            "l10n_ae_document_category": "commercial"
            if type_code in ("480", "81")
            else "tax",
            "l10n_ae_credit_reason": tree.findtext(
                "./{*}DiscrepancyResponse/{*}ResponseCode"
            )
            if is_credit
            else False,
            "l10n_ae_origin_system": "controlled-inbound",
            "l10n_ae_origin_document_id": f"{self.binding_id.id}:{self.remote_id}",
            "l10n_ae_fiscal_uuid": tree.findtext("./{*}UUID"),
            "l10n_ae_free_zone": transaction_flags == "10000000",
            "l10n_ae_beneficiary_id": beneficiary_id,
            "l10n_ae_received_currency_id": currency.id,
            **fiscal_values,
            "l10n_ae_received_prepaid_amount": float(
                tree.findtext("./{*}LegalMonetaryTotal/{*}PrepaidAmount") or 0
            ),
            "l10n_ae_received_payable_amount": float(
                tree.findtext("./{*}LegalMonetaryTotal/{*}PayableAmount")
            ),
        }
        existing = self.env["account.move"]
        if selfbilling:
            existing = self._find_existing_selfbilling_move(
                tree, partner, values["move_type"]
            )
        if existing:
            metadata = {
                name: value
                for name, value in values.items()
                if name.startswith("l10n_ae_")
            }
            metadata.pop("l10n_ae_origin_system")
            metadata.pop("l10n_ae_origin_document_id")
            existing.write(metadata)
            move = existing
        else:
            move = self.env["account.move"].create(values)
        move._l10n_ae_mark_received()
        if not existing:
            self.env["account.edi.ubl.kmee_ae.inbound"]._import_invoice_ubl_cii(
                move, {"xml_tree": tree, "attachment": False}, new=True
            )
        reference_snapshot = False
        if is_credit:
            originals, reference_snapshot = self._resolve_received_credit_references(
                tree, move, selfbilling
            )
            move.reversed_entry_id = originals if len(originals) == 1 else False
        expected = [
            ("TaxExclusiveAmount", move.amount_untaxed),
            ("TaxInclusiveAmount", move.amount_total),
        ]
        for name, value in expected:
            amount = tree.findtext("./{*}LegalMonetaryTotal/{*}" + name)
            if amount is None or move.currency_id.compare_amounts(float(amount), value):
                raise UserError(
                    self.env._(
                        "Received totals do not match the draft accounting document."
                    )
                )
        super(UaeEdiInbox, self.sudo()).write(
            {"credit_reference_snapshot": reference_snapshot}
        )
        self.env["ir.attachment"].create(
            {
                "name": "received-pint-ae.xml",
                "res_model": "account.move",
                "res_id": move.id,
                "raw": content,
                "mimetype": "application/xml",
            }
        )
        return move

    def _received_beneficiary_id(self, tree):
        nodes = tree.findall("./{*}BuyerCustomerParty")
        if not nodes:
            return False
        for child in ("Party", "PartyIdentification", "ID"):
            if len(nodes) != 1:
                raise UserError(
                    self.env._("The received beneficiary structure requires review.")
                )
            nodes = nodes[0].findall("./{*}" + child)
        if len(nodes) != 1:
            raise UserError(
                self.env._("The received beneficiary structure requires review.")
            )
        return nodes[0].text or False

    def _received_fiscal_values(self, tree, currency_code):
        def amount(xpath):
            return self._received_fiscal_amount(xpath, tree)

        try:
            quantum = Decimal("0.01")
            rate_text = tree.findtext("./{*}TaxExchangeRate/{*}CalculationRate")
            rate = Decimal(1)
            vat = amount("./{*}TaxTotal/{*}TaxAmount[@currencyID='AED']")
            gross = amount("./{*}LegalMonetaryTotal/{*}TaxInclusiveAmount")
            if currency_code != "AED":
                if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?", rate_text or ""):
                    raise ValueError("invalid_fiscal_rate")
                rate = Decimal(rate_text)
                if rate <= 0:
                    raise ValueError("nonpositive_fiscal_rate")
                gross_path = (
                    "./{*}AdditionalDocumentReference"
                    "[{*}DocumentTypeCode='aedtotal-incl-vat']/{*}DocumentDescription"
                )
                declared_gross = amount(gross_path)
                if not re.fullmatch(
                    r"-?[0-9]+(?:\.[0-9]{1,2})?", tree.findtext(gross_path) or ""
                ):
                    raise ValueError("invalid_fiscal_gross")
                document_vat = amount(
                    f"./{{*}}TaxTotal/{{*}}TaxAmount[@currencyID='{currency_code}']"
                )
                if vat != (document_vat * rate).quantize(
                    quantum, rounding=ROUND_HALF_UP
                ):
                    raise ValueError("fiscal_vat_mismatch")
                if declared_gross != (gross * rate).quantize(
                    quantum, rounding=ROUND_HALF_UP
                ):
                    raise ValueError("fiscal_gross_mismatch")
                gross = declared_gross
            self._check_received_fiscal_lines(tree, rate)
        except (InvalidOperation, ValueError) as error:
            raise UserError(
                self.env._(
                    "Received fiscal AED amounts or exchange rate require review."
                )
            ) from error
        return {
            "l10n_ae_received_fiscal_currency_id": self.env.ref("base.AED").id,
            "l10n_ae_received_tax_exchange_rate": rate_text
            if currency_code != "AED"
            else False,
            "l10n_ae_received_tax_amount": float(vat),
            "l10n_ae_received_tax_inclusive_amount": float(gross),
        }

    @staticmethod
    def _received_fiscal_amount(xpath, parent):
        nodes = parent.findall(xpath)
        if len(nodes) != 1:
            raise ValueError("ambiguous_fiscal_amount")
        value = Decimal(nodes[0].text or "")
        if not value.is_finite():
            raise ValueError("nonfinite_fiscal_amount")
        return value

    def _check_received_fiscal_lines(self, tree, rate):
        amount = self._received_fiscal_amount
        precision = Decimal("0.000001")
        lines = tree.findall("./{*}InvoiceLine") + tree.findall("./{*}CreditNoteLine")
        for line in lines:
            categories = line.findall("./{*}Item/{*}ClassifiedTaxCategory")
            if len(categories) != 1:
                raise ValueError("ambiguous_line_tax_category")
            code = categories[0].findtext("./{*}ID")
            quantity_path = (
                "./{*}InvoicedQuantity"
                if line.find("./{*}InvoicedQuantity") is not None
                else "./{*}CreditedQuantity"
            )
            base_quantity = (
                amount("./{*}Price/{*}BaseQuantity", line)
                if line.find("./{*}Price/{*}BaseQuantity") is not None
                else Decimal(1)
            )
            if base_quantity <= 0:
                raise ValueError("nonpositive_price_base_quantity")
            net = (
                amount("./{*}Price/{*}PriceAmount", line)
                * amount(quantity_path, line)
                / base_quantity
            )
            for adjustment in line.findall("./{*}AllowanceCharge"):
                adjustment_amount = amount("./{*}Amount", adjustment)
                net += (
                    adjustment_amount
                    if adjustment.findtext("./{*}ChargeIndicator") in {"true", "1"}
                    else -adjustment_amount
                )
            tax = (
                net * Decimal(categories[0].findtext("./{*}Percent") or "0") / 100
                if code == "S"
                else Decimal(0)
            )
            gross_node = line.find("./{*}ItemPriceExtension/{*}Amount")
            if gross_node is None or gross_node.get("currencyID") != "AED":
                raise ValueError("invalid_line_fiscal_currency")
            if amount("./{*}ItemPriceExtension/{*}Amount", line).quantize(
                precision
            ) != ((net + tax) * rate).quantize(precision):
                raise ValueError("line_fiscal_gross_mismatch")
            if code != "E":
                vat_node = line.find("./{*}ItemPriceExtension/{*}TaxTotal/{*}TaxAmount")
                if vat_node is None or vat_node.get("currencyID") != "AED":
                    raise ValueError("invalid_line_fiscal_vat_currency")
                if amount(
                    "./{*}ItemPriceExtension/{*}TaxTotal/{*}TaxAmount", line
                ).quantize(precision) != (tax * rate).quantize(precision):
                    raise ValueError("line_fiscal_vat_mismatch")

    def _resolve_received_selfbilling_agreement(self, tree):
        importer = self.env["account.edi.ubl.kmee_ae.inbound"]
        partner, _logs = importer._import_partner(
            self.company_id,
            **importer._import_retrieve_partner_vals(tree, "AccountingCustomer"),
        )
        local_vat = self.company_id.partner_id.vat
        supplier_vat = tree.xpath(
            "./*[local-name()='AccountingSupplierParty']/*[local-name()='Party']"
            "/*[local-name()='PartyTaxScheme'][*[local-name()='TaxScheme']"
            "/*[local-name()='ID']='VAT']/*[local-name()='CompanyID']/text()"
        )
        if (
            not re.fullmatch(r"[0-9]{15}", local_vat or "")
            or supplier_vat != [local_vat]
            or not re.fullmatch(r"[0-9]{15}", partner.vat or "")
            or partner.routing_scheme != "0235"
            or partner.routing_endpoint in {"9900000097", "9900000098", "9900000099"}
            or not re.fullmatch(r"[0-9]{10}", partner.routing_endpoint or "")
        ):
            raise UserError(
                self.env._(
                    "Review the received self-billing VAT and onboarding identities."
                )
            )
        issue_date = fields.Date.to_date(tree.findtext("./{*}IssueDate"))
        agreement = self.env["l10n_ae.selfbilling.agreement"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("partner_id", "=", partner.id),
                ("local_role", "=", "supplier"),
                ("active", "=", True),
                ("start_date", "<=", issue_date),
                "|",
                ("end_date", "=", False),
                ("end_date", ">=", issue_date),
            ],
            limit=2,
        )
        if len(agreement) != 1:
            raise UserError(
                self.env._("The received self-billing agreement requires review.")
            )
        return partner, agreement

    def _resolve_received_credit_references(self, tree, move, selfbilling):
        references = tree.findall("./{*}BillingReference/{*}InvoiceDocumentReference")
        if not references:
            raise UserError(
                self.env._("This received credit requires reference review.")
            )
        domain = [
            ("company_id", "=", self.company_id.id),
            ("partner_id", "=", move.partner_id.id),
            ("move_type", "=", "out_invoice" if selfbilling else "in_invoice"),
            ("currency_id", "=", move.currency_id.id),
            ("l10n_ae_document_category", "=", move.l10n_ae_document_category),
            ("state", "!=", "cancel"),
        ]
        if selfbilling:
            domain.append(("l10n_ae_received_process", "=", "selfbilling"))
        else:
            domain += [
                ("l10n_ae_received_process", "!=", "selfbilling"),
                ("is_self_billing", "=", False),
            ]
        originals = self.env["account.move"]
        snapshot = []
        for reference in references:
            number = reference.findtext("./{*}ID")
            date = reference.findtext("./{*}IssueDate") or False
            matching = (
                ["|", ("ref", "=", number), ("name", "=", number)]
                if selfbilling
                else [("ref", "=", number)]
            )
            if date:
                matching.append(("invoice_date", "=", date))
            original = self.env["account.move"].search(domain + matching, limit=2)
            if len(original) != 1:
                raise UserError(
                    self.env._(
                        "The received credit's original self-billed sale "
                        "requires review."
                    )
                    if selfbilling
                    else self.env._(
                        "The received credit's original supplier bill requires review."
                    )
                )
            if original in originals:
                raise UserError(
                    self.env._("Repeated received invoice references require review.")
                )
            originals |= original
            snapshot.append(
                {"number": number, "issue_date": date, "move_id": original.id}
            )
        return originals, snapshot

    def _find_existing_selfbilling_move(self, tree, partner, move_type):
        number = tree.findtext("./{*}ID")
        candidate = self.env["account.move"].search(
            [
                ("company_id", "=", self.company_id.id),
                "|",
                ("l10n_ae_fiscal_uuid", "=", tree.findtext("./{*}UUID")),
                "&",
                ("move_type", "=", move_type),
                "&",
                ("partner_id", "=", partner.id),
                "|",
                ("ref", "=", number),
                ("name", "=", number),
            ],
            limit=2,
        )
        if not candidate:
            return candidate
        if (
            len(candidate) != 1
            or candidate.move_type != move_type
            or candidate.partner_id != partner
            or candidate.state != "draft"
            or candidate.l10n_ae_transmission_id
            or candidate.l10n_ae_received_document
            or candidate.l10n_ae_document_category != "tax"
            or candidate.currency_id.name != tree.findtext("./{*}DocumentCurrencyCode")
            or candidate.invoice_date
            != fields.Date.to_date(tree.findtext("./{*}IssueDate"))
        ):
            raise UserError(
                self.env._(
                    "The existing billing document requires reconciliation review."
                )
            )
        expected = parse_invoice_xml(candidate.preview_l10n_ae_pint().encode())
        if candidate._l10n_ae_transaction_flags() != tree.findtext(
            "./{*}ProfileExecutionID"
        ) or (
            candidate.l10n_ae_beneficiary_id or False
        ) != self._received_beneficiary_id(tree):
            raise UserError(
                self.env._(
                    "The existing billing document's scenario and beneficiary "
                    "require review."
                )
            )
        if self._fiscal_lines_signature(expected) != self._fiscal_lines_signature(tree):
            raise UserError(
                self.env._(
                    "The existing billing document's fiscal lines require review."
                )
            )
        return candidate

    @staticmethod
    def _fiscal_lines_signature(tree):
        def signature(node):
            text = (node.text or "").strip()
            localname = node.tag.rsplit("}", 1)[-1]
            if text and localname.endswith(
                ("Amount", "Quantity", "Percent", "Numeric")
            ):
                text = str(Decimal(text).normalize())
            return (
                node.tag,
                tuple(sorted(node.attrib.items())),
                text,
                tuple(signature(child) for child in node if isinstance(child.tag, str)),
            )

        result = []
        for line in tree.findall("./{*}InvoiceLine") + tree.findall(
            "./{*}CreditNoteLine"
        ):
            parts = [
                signature(child)
                for child in line
                if isinstance(child.tag, str)
                and child.tag.rsplit("}", 1)[-1]
                in {
                    "InvoicedQuantity",
                    "CreditedQuantity",
                    "LineExtensionAmount",
                    "TaxTotal",
                    "ItemPriceExtension",
                    "Price",
                    "AllowanceCharge",
                }
            ]
            parts += [
                signature(child)
                for child in line.findall("./{*}Item/{*}ClassifiedTaxCategory")
            ]
            parts += [
                signature(child)
                for child in line.findall("./{*}Item/{*}CommodityClassification")
            ]
            result.append(tuple(parts))
        return tuple(result)
