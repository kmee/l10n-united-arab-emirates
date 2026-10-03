# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import os

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
        string="Supplier Bill",
        readonly=True,
        check_company=True,
        ondelete="restrict",
    )

    _received_identity = models.UniqueIndex(
        "(binding_id, remote_id, xml_hash)",
        "These received document bytes already exist.",
    )

    def _check_operator(self):
        if not self.env.su and not self.env.user.has_group(
            "account.group_account_manager"
        ):
            raise AccessError(
                self.env._(
                    "Only an accounting manager can receive or process UAE documents."
                )
            )

    @api.model
    def receive_controlled_document(self, binding_id, remote_id, content):
        """Persist controlled authenticated bytes without an accounting commit."""
        self._check_operator()
        binding = self.env["l10n_ae.edi.binding"].browse(binding_id).exists()
        binding.check_access("read")
        if not binding or not binding.active or binding.environment != "sandbox":
            raise UserError(self.env._("Select an active controlled sandbox binding."))
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
        return (
            self.sudo()
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
                    **(conflict if other else {}),
                }
            )
            .with_env(self.env)
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
        try:
            with self.env.cr.savepoint():
                move = self.with_company(self.company_id)._process_received_xml()
        except (UserError, UnsafeInvoiceXml, ValidationUnavailable) as error:
            super(UaeEdiInbox, self.sudo()).write(
                {"state": "review", "diagnostic": str(error)}
            )
            return False
        super(UaeEdiInbox, self.sudo()).write(
            {"state": "processed", "diagnostic": False, "move_id": move.id}
        )
        return True

    def action_open_move(self):
        self.ensure_one()
        self.check_access("read")
        if not self.move_id:
            raise UserError(self.env._("No supplier bill has been created yet."))
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
        directory = os.environ.get("KMEE_UAE_NORMATIVE_DIR")
        if not directory:
            raise UserError(
                self.env._("Configure the offline UAE normative resource directory.")
            )
        report = OfflinePintValidator(directory).validate(content)
        self.env["account.move"]._l10n_ae_check_pint_report(report)
        if (
            tree.findtext("./{*}CustomizationID") != "urn:peppol:pint:billing-1@ae-1"
            or tree.findtext("./{*}InvoiceTypeCode") != "380"
            or tree.findtext("./{*}ProfileExecutionID") != "00000000"
            or tree.findtext("./{*}DocumentCurrencyCode") != "AED"
            or tree.findall("./{*}AllowanceCharge")
            or float(tree.findtext("./{*}LegalMonetaryTotal/{*}PrepaidAmount") or 0)
            != 0
        ):
            raise UserError(
                self.env._("This received UAE scenario requires review before import.")
            )
        endpoint = tree.find("./{*}AccountingCustomerParty/{*}Party/{*}EndpointID")
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
                return prior
            raise UserError(
                self.env._("Received fiscal UUID reused with different document bytes.")
            )
        journal = self.env["account.journal"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("type", "=", "purchase"),
                ("is_self_billing", "=", False),
            ],
            limit=2,
        )
        if len(journal) != 1:
            raise UserError(
                self.env._(
                    "Select an unambiguous purchase journal before receiving UAE "
                    "invoices."
                )
            )
        move = self.env["account.move"].create(
            {
                "company_id": self.company_id.id,
                "journal_id": journal.id,
                "move_type": "in_invoice",
                "l10n_ae_origin_system": "controlled-inbound",
                "l10n_ae_origin_document_id": f"{self.binding_id.id}:{self.remote_id}",
                "l10n_ae_fiscal_uuid": tree.findtext("./{*}UUID"),
            }
        )
        move._l10n_ae_mark_received()
        self.env["account.edi.ubl.kmee_ae.inbound"]._import_invoice_ubl_cii(
            move, {"xml_tree": tree, "attachment": False}, new=True
        )
        expected = [
            ("TaxExclusiveAmount", move.amount_untaxed),
            ("TaxInclusiveAmount", move.amount_total),
        ]
        for name, value in expected:
            amount = tree.findtext("./{*}LegalMonetaryTotal/{*}" + name)
            if amount is None or move.currency_id.compare_amounts(float(amount), value):
                raise UserError(
                    self.env._("Received totals do not match the draft bill.")
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
