# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json
from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_ae_advance_reference_summary = fields.Text(
        string="Taxed Advance References",
        compute="_compute_l10n_ae_advance_reference_summary",
    )

    def _l10n_ae_lock_advance_sources(self, additional_sources=None):
        moves = self.filtered(
            lambda move: (
                move.company_id.country_code == "AE"
                and move.move_type == "out_invoice"
                and not move.l10n_ae_received_document
                and not move.l10n_ae_transmission_id
            )
        )
        sources = self.env["account.move"]
        if moves and additional_sources:
            sources |= additional_sources
        for move in moves:
            linked = (
                move.invoice_line_ids.filtered(
                    lambda line: line.is_downpayment and line.quantity < 0
                )
                ._get_downpayment_lines()
                .filtered(
                    lambda line: (
                        line.move_id.move_type == "out_invoice" and line.quantity > 0
                    )
                )
                .move_id
            )
            if linked.filtered(
                lambda source, move=move: source.company_id != move.company_id
            ):
                raise UserError(
                    self.env._(
                        "The taxed advance and deduction must belong "
                        "to the same legal company."
                    )
                )
            sources |= linked
        if sources:
            locked = moves | sources
            locked.check_access("write")
            locked.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM account_move WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                [locked.ids],
            )
            locked.invalidate_recordset()
            # Publish the consumption change through the original's row version.
            # A stale repeatable-read request then retries its entire action.
            # Original fiscal number/UUID/snapshot/XML remain unchanged.
            # A source-row revision coordinates repeatable-read consumption.
            # It must not run invoice-line synchronization for this metadata.
            sources.with_context(
                skip_invoice_sync=True, skip_is_manually_modified=True
            ).write({"write_date": self.env.cr.now()})
            sources.flush_recordset(["write_uid", "write_date"])

    def _post(self, soft=True):
        candidates = self.filtered(
            lambda move: (
                not soft
                or not move.date
                or move.date <= fields.Date.context_today(move)
            )
        )
        candidates._l10n_ae_lock_advance_sources()
        # Keep the existing advance-consumption accounting guard. Unlike missing
        # fiscal metadata, an unposted/over-consumed advance invalidates the
        # deduction itself; automatic preparation must not swallow this refusal.
        for move in candidates.filtered(
            lambda move: move.company_id.l10n_ae_edi_auto_prepare
        ):
            move._l10n_ae_advance_references()
        return super()._post(soft=soft)

    def action_l10n_ae_prepare(self):
        self.ensure_one()
        self._l10n_ae_lock_advance_sources()
        return super().action_l10n_ae_prepare()

    def button_draft(self):
        sources = self.filtered(
            lambda move: (
                move.company_id.country_code == "AE"
                and move.move_type == "out_invoice"
                and move.state == "posted"
                and move.invoice_line_ids.filtered(
                    lambda line: line.is_downpayment and line.quantity > 0
                )
            )
        )
        if sources:
            sources.check_access("write")
            sources.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM account_move WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                [sources.ids],
            )
            sources.invalidate_recordset()
            for source in sources:
                frozen_deduction = self.env["l10n_ae.edi.transmission"].search_count(
                    [
                        ("company_id", "=", source.company_id.id),
                        ("l10n_ae_advance_source_ids", "in", source.ids),
                        # Match prior consumption: a replaced issue remains
                        # immutable history, not an effective deduction. An
                        # editing correction still reserves its frozen amount.
                        "|",
                        ("local_correction_id", "=", False),
                        "&",
                        (
                            "local_correction_id.replacement_transmission_id",
                            "=",
                            False,
                        ),
                        ("move_id.state", "=", "draft"),
                    ],
                    limit=1,
                )
                posted_deduction = self.env["account.move.line"].search_count(
                    [
                        ("company_id", "=", source.company_id.id),
                        (
                            "sale_line_ids",
                            "in",
                            source.invoice_line_ids.sale_line_ids.ids,
                        ),
                        ("is_downpayment", "=", True),
                        ("quantity", "<", 0),
                        ("move_id.state", "=", "posted"),
                        ("move_id.move_type", "=", "out_invoice"),
                        ("move_id", "!=", source.id),
                    ],
                    limit=1,
                )
                if frozen_deduction or posted_deduction:
                    raise UserError(
                        self.env._(
                            "This advance has already been deducted in "
                            "accounting or UAE evidence. "
                            "Preserve it and review the appropriate adjustment."
                        )
                    )
        return super().button_draft()

    def _l10n_ae_advance_references(self):
        self.ensure_one()
        if self.l10n_ae_transmission_id:
            return json.loads(self.l10n_ae_transmission_id.payload).get(
                "taxed_advances", []
            )
        if (
            self.company_id.country_code != "AE"
            or self.move_type != "out_invoice"
            or self.l10n_ae_received_document
        ):
            return []
        references = {}
        for line in self.invoice_line_ids.filtered(
            lambda item: item.is_downpayment and item.quantity < 0
        ):
            sources = (
                line._get_downpayment_lines()
                .filtered(
                    lambda item: (
                        item.move_id.move_type == "out_invoice" and item.quantity > 0
                    )
                )
                .move_id
            )
            if len(sources) != 1:
                raise UserError(
                    self.env._(
                        "Review the original taxed advance linked to this deduction."
                    )
                )
            source = sources
            if source.company_id != self.company_id:
                raise UserError(
                    self.env._(
                        "The taxed advance and deduction must belong "
                        "to the same legal company."
                    )
                )
            if source.state != "posted":
                raise UserError(
                    self.env._(
                        "The original taxed advance must be posted "
                        "before this deduction."
                    )
                )
            if source.commercial_partner_id != self.commercial_partner_id:
                raise UserError(
                    self.env._(
                        "The taxed advance must belong to the same commercial customer."
                    )
                )
            identity = (
                json.loads(source.l10n_ae_transmission_id.payload)
                if source.l10n_ae_transmission_id
                else {
                    "invoice_number": source.name,
                    "issue_date": fields.Date.to_string(source.invoice_date),
                }
            )
            if (
                identity.get("currency", source.currency_id.name)
                != self.currency_id.name
            ):
                raise UserError(
                    self.env._(
                        "The taxed advance and deduction must use "
                        "the same document currency."
                    )
                )
            if (
                identity.get("document_category", source.l10n_ae_document_category)
                != self.l10n_ae_document_category
            ):
                raise UserError(
                    self.env._(
                        "The taxed advance and deduction must use "
                        "the same document category."
                    )
                )
            reference = references.setdefault(
                source.id,
                {
                    "source_move_id": source.id,
                    "number": identity["invoice_number"],
                    "issue_date": identity["issue_date"],
                    "consumed_net": Decimal(0),
                    "consumed_vat": Decimal(0),
                },
            )
            reference["consumed_net"] -= Decimal(str(line.price_subtotal))
            reference["consumed_vat"] -= Decimal(str(line.price_total)) - Decimal(
                str(line.price_subtotal)
            )
        scale = Decimal(1).scaleb(-self.currency_id.decimal_places)
        for reference in references.values():
            source = self.env["account.move"].browse(reference["source_move_id"])
            source_totals = (
                json.loads(source.l10n_ae_transmission_id.payload)["totals"]
                if source.l10n_ae_transmission_id
                else {"net": source.amount_untaxed, "vat": source.amount_tax}
            )
            if reference["consumed_net"] > Decimal(
                str(source_totals["net"])
            ) or reference["consumed_vat"] > Decimal(str(source_totals["vat"])):
                raise UserError(
                    self.env._(
                        "The deduction exceeds the original taxed advance. "
                        "Review the invoice lines."
                    )
                )
            consumed = self._l10n_ae_prior_advance_consumption(source)
            if consumed["consumed_net"] + reference["consumed_net"] > Decimal(
                str(source_totals["net"])
            ) or consumed["consumed_vat"] + reference["consumed_vat"] > Decimal(
                str(source_totals["vat"])
            ):
                raise UserError(
                    self.env._(
                        "This advance has already been deducted. "
                        "Review its remaining net and VAT before another deduction."
                    )
                )
            for name in ("consumed_net", "consumed_vat"):
                reference[name] = str(reference[name].quantize(scale))
        return list(references.values())

    def _l10n_ae_prior_advance_consumption(self, source):
        self.ensure_one()
        prior = self.env["l10n_ae.edi.transmission"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("l10n_ae_advance_source_ids", "in", source.ids),
                ("move_id", "!=", self.id),
                "|",
                ("local_correction_id", "=", False),
                "&",
                ("local_correction_id.replacement_transmission_id", "=", False),
                ("move_id.state", "=", "draft"),
            ]
        )
        consumed = {"consumed_net": Decimal(0), "consumed_vat": Decimal(0)}
        for transmission in prior:
            for used in json.loads(transmission.payload).get("taxed_advances", []):
                if used["source_move_id"] == source.id:
                    for name in consumed:
                        consumed[name] += Decimal(used[name])
        posted_lines = self.env["account.move.line"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("sale_line_ids", "in", source.invoice_line_ids.sale_line_ids.ids),
                ("is_downpayment", "=", True),
                ("quantity", "<", 0),
                ("move_id.state", "=", "posted"),
                ("move_id.move_type", "=", "out_invoice"),
                ("move_id", "!=", self.id),
                ("move_id.l10n_ae_transmission_id", "=", False),
            ]
        )
        for used_line in posted_lines:
            if (
                used_line.move_id.currency_id != self.currency_id
                or used_line.move_id.l10n_ae_document_category
                != self.l10n_ae_document_category
            ):
                raise UserError(
                    self.env._(
                        "Review the previous posted advance deduction's "
                        "currency and document category."
                    )
                )
            consumed["consumed_net"] -= Decimal(str(used_line.price_subtotal))
            consumed["consumed_vat"] -= Decimal(str(used_line.price_total)) - Decimal(
                str(used_line.price_subtotal)
            )
        return consumed

    def _l10n_ae_check_pint_data(self):
        self.check_access("read")
        # Advance eligibility must be checked before full XML preflight. A
        # mismatched advance currency cannot be diagnosed as an exchange-rate
        # rounding problem just because the formatter runs earlier at Check.
        for move in self:
            move._l10n_ae_advance_references()
        return super()._l10n_ae_check_pint_data()

    def _l10n_ae_snapshot(self):
        snapshot = super()._l10n_ae_snapshot()
        snapshot["taxed_advances"] = self._l10n_ae_advance_references()
        return snapshot

    @api.depends(
        "l10n_ae_transmission_id",
        "invoice_line_ids.sale_line_ids",
        "invoice_line_ids.quantity",
        "invoice_line_ids.price_subtotal",
        "invoice_line_ids.price_total",
    )
    def _compute_l10n_ae_advance_reference_summary(self):
        for move in self:
            try:
                references = move._l10n_ae_advance_references()
                move.l10n_ae_advance_reference_summary = "\n".join(
                    f"{ref['number']} | {ref['issue_date']} | "
                    f"{self.env._('Net')}: {ref['consumed_net']} | "
                    f"{self.env._('VAT')}: {ref['consumed_vat']} "
                    f"{move.currency_id.name}"
                    for ref in references
                )
            except UserError as error:
                move.l10n_ae_advance_reference_summary = str(error)
