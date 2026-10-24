# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import date_utils


class UaeEdiRetention(models.Model):
    _name = "l10n_ae.edi.retention"
    _description = "UAE Reviewed Retention Obligation"
    _check_company_auto = True

    name = fields.Char(required=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        readonly=True,
        index=True,
        default=lambda self: self.env.company,
    )
    transmission_id = fields.Many2one(
        "l10n_ae.edi.transmission",
        string="Issued Original",
        check_company=True,
        ondelete="restrict",
        index=True,
    )
    record_class = fields.Selection(
        [
            ("invoice", "Invoice"),
            ("credit_note", "Credit Note"),
            ("evidence", "Evidence"),
        ]
    )
    tax_purpose = fields.Selection(
        [
            ("vat", "VAT"),
            ("corporate_tax", "Corporate Tax"),
            ("tax_procedures", "Tax Procedures"),
            ("other", "Other Reviewed Purpose"),
        ]
    )
    anchor_kind = fields.Selection(
        [
            ("tax_period_end", "Tax Period End"),
            ("calendar_year_end", "Calendar Year End"),
            ("source_event", "Reviewed Source Event"),
        ]
    )
    period_start = fields.Date()
    anchor_date = fields.Date()
    retention_years = fields.Integer()
    extended_until = fields.Date(
        help="Explicit supported extension; cannot shorten the base period."
    )
    source_reference = fields.Text()
    policy_version = fields.Char()
    review_note = fields.Text()
    state = fields.Selection(
        [("draft", "Requires Review"), ("approved", "Reviewed")],
        default="draft",
        required=True,
        readonly=True,
        copy=False,
    )
    approved_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False, ondelete="restrict"
    )
    approved_at = fields.Datetime(readonly=True, copy=False)
    retain_until = fields.Date(readonly=True, copy=False)
    hold_ids = fields.One2many(
        "l10n_ae.edi.retention.hold", "retention_id", readonly=True
    )

    @api.model
    def _check_current_editor(self):
        principal = self.env["res.users"].browse(self.env.uid)
        if not principal.active or not (
            principal.has_group("account.group_account_manager")
            or principal.has_group("base.group_system")
        ):
            raise AccessError(
                self.env._(
                    "Only an active accounting manager can review UAE retention."
                )
            )

    @api.model
    def _check_current_document(self, document):
        document.ensure_one()
        # Native relation access hides archived companies from an ordinary
        # res.users browse. env.user reads only this actor's own assignment
        # metadata; the original itself is still checked under the actual UID.
        principal = self.env.user
        if (
            not principal.active
            or document.company_id.id
            not in principal.with_context(active_test=False).company_ids.ids
        ):
            raise AccessError(
                self.env._(
                    "The current account cannot access this UAE retention company."
                )
            )
        document.with_user(self.env.uid).check_access("read")

    @api.model
    def _document_from_values(self, vals):
        document = (
            self.env["l10n_ae.edi.transmission"]
            .browse(vals.get("transmission_id"))
            .exists()
        )
        if len(document) != 1:
            raise ValidationError(
                self.env._("A retention obligation needs exactly one UAE original.")
            )
        return document

    def _document(self):
        self.ensure_one()
        return self._document_from_values({"transmission_id": self.transmission_id.id})

    @api.model_create_multi
    def create(self, vals_list):
        self._check_current_editor()
        self.with_user(self.env.uid).check_access("create")
        protected = {"state", "approved_by_id", "approved_at", "retain_until"}
        prepared = []
        for values in vals_list:
            vals = dict(values)
            if protected.intersection(vals):
                raise UserError(
                    self.env._(
                        "Use the UAE retention review action to record approval."
                    )
                )
            document = self._document_from_values(vals)
            self._check_current_document(document)
            if vals.get("company_id", document.company_id.id) != document.company_id.id:
                raise ValidationError(
                    self.env._(
                        "Retention and original must belong to the same company."
                    )
                )
            # Native defaults are applied after this guard. Fix approval facts so
            # neither caller context nor saved model defaults can fabricate them.
            vals.update(
                company_id=document.company_id.id,
                state="draft",
                approved_by_id=False,
                approved_at=False,
                retain_until=False,
            )
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        self._check_current_editor()
        self.with_user(self.env.uid).check_access("write")
        if {
            "state",
            "approved_by_id",
            "approved_at",
            "retain_until",
            "company_id",
        }.intersection(vals):
            raise UserError(
                self.env._(
                    "Use the UAE retention review action; approval facts are protected."
                )
            )
        for record in self:
            record._check_current_document(record._document())
            if record.state == "approved":
                raise UserError(
                    self.env._("Reviewed UAE retention obligations are immutable.")
                )
            values = record._target_values() | vals
            document = record._document_from_values(values)
            record._check_current_document(document)
            if document.company_id != record.company_id:
                raise ValidationError(
                    self.env._(
                        "Retention and original must belong to the same company."
                    )
                )
        return super().write(vals)

    def _target_values(self):
        self.ensure_one()
        return {"transmission_id": self.transmission_id.id}

    def _reviewed_end_date(self):
        self.ensure_one()
        if not all(
            (self.record_class, self.tax_purpose, self.anchor_kind, self.anchor_date)
        ):
            raise ValidationError(
                self.env._("Review the record class, tax purpose and retention anchor.")
            )
        if not all(
            (value or "").strip()
            for value in (self.source_reference, self.policy_version, self.review_note)
        ):
            raise ValidationError(
                self.env._(
                    "Retention review requires its source, version and review note."
                )
            )
        if not 1 <= self.retention_years <= 100:
            raise ValidationError(
                self.env._("Review a retention interval of 1 to 100 calendar years.")
            )
        if self.anchor_kind == "calendar_year_end" and (
            self.anchor_date.month,
            self.anchor_date.day,
        ) != (12, 31):
            raise ValidationError(
                self.env._("A calendar-year retention anchor must be 31 December.")
            )
        if self.anchor_kind == "tax_period_end" and (
            not self.period_start or self.period_start > self.anchor_date
        ):
            raise ValidationError(
                self.env._("Review the tax-period start and end for retention.")
            )
        try:
            end = date_utils.add(self.anchor_date, years=self.retention_years)
        except (ValueError, OverflowError) as exc:
            raise ValidationError(
                self.env._("Review a retention anniversary within the supported range.")
            ) from exc
        if self.extended_until and self.extended_until < end:
            raise ValidationError(
                self.env._("A supported extension cannot shorten the retention period.")
            )
        return max(end, self.extended_until) if self.extended_until else end

    def action_approve(self):
        self._check_current_editor()
        self.with_user(self.env.uid).check_access("write")
        for record in self:
            record._check_current_document(record._document())
        # Validate the whole batch before recording any approval facts.
        ends = {
            record.id: record._reviewed_end_date()
            for record in self
            if record.state != "approved"
        }
        for record in self:
            if record.id in ends:
                super(UaeEdiRetention, record).write(
                    {
                        "state": "approved",
                        "approved_by_id": self.env.uid,
                        "approved_at": fields.Datetime.now(),
                        "retain_until": ends[record.id],
                    }
                )
        return True

    def action_open_hold(self):
        self.ensure_one()
        self._check_current_editor()
        self.with_user(self.env.uid).check_access("read")
        self._check_current_document(self._document())
        if self.state != "approved":
            raise ValidationError(
                self.env._("Review the retention obligation before placing a hold.")
            )
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Place Retention Hold"),
            "res_model": "l10n_ae.edi.retention.hold.wizard",
            "view_mode": "form",
            "view_id": self.env.ref("l10n_ae_edi.edi_retention_hold_wizard_form").id,
            "target": "new",
            "context": {"default_retention_id": self.id},
        }

    def action_assess_original(self, as_of=None):
        self.ensure_one()
        self.with_user(self.env.uid).check_access("read")
        status = self._document().get_retention_status(as_of)
        labels = {
            "unclassified": self.env._("Unclassified"),
            "review_required": self.env._("Requires Review"),
            "retained": self.env._("Retained"),
            "held": self.env._("Held"),
            "reviewable": self.env._("Disposition Review Permitted"),
        }
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("Original Retention Assessment"),
                "message": self.env._(
                    "Status: %(state)s. Assessed on: %(date)s. "
                    "Latest reviewed deadline: %(deadline)s. "
                    "Obligations: %(obligations)s. Active holds: %(holds)s. "
                    "Disposition review does not authorize deletion.",
                    state=labels[status["state"]],
                    date=status["as_of"],
                    deadline=status["retain_until"] or self.env._("Not Reviewed"),
                    obligations=status["obligation_count"],
                    holds=status["active_hold_count"],
                ),
                "type": "info",
                "sticky": True,
            },
        }

    @api.ondelete(at_uninstall=False)
    def _unlink_except_reviewed_obligation(self):
        self._check_current_editor()
        self.with_user(self.env.uid).check_access("unlink")
        for record in self:
            record._check_current_document(record._document())
        if any(record.state == "approved" for record in self):
            raise UserError(
                self.env._("Reviewed UAE retention obligations cannot be deleted.")
            )

    @api.model
    def _evidence_appendix(self, document, target_field):
        self._check_current_document(document)
        obligations = (
            self.sudo()
            .search([(target_field, "=", document.id)], order="id")
            .with_user(self.env.uid)
        )
        obligations.check_access("read")
        holds = (
            self.env["l10n_ae.edi.retention.hold"]
            .sudo()
            .search([("retention_id", "in", obligations.ids)], order="id")
            .with_user(self.env.uid)
        )
        holds.check_access("read")
        if any(record.company_id != document.company_id for record in obligations):
            raise UserError(
                self.env._("Retention evidence has an inconsistent company.")
            )
        if any(record.company_id != document.company_id for record in holds):
            raise UserError(self.env._("Hold evidence has an inconsistent company."))

        def metadata(records, names):
            rows = []
            for record in records:
                row = {"id": record.id}
                for name in names:
                    value = record[name]
                    kind = record._fields[name].type
                    if kind == "many2one":
                        value = value.id or False
                    elif kind == "date":
                        value = fields.Date.to_string(value) if value else False
                    elif kind == "datetime":
                        value = fields.Datetime.to_string(value) if value else False
                    row[name] = value
                rows.append(row)
            return rows

        names = [
            "name",
            "company_id",
            "transmission_id",
            "record_class",
            "tax_purpose",
            "anchor_kind",
            "period_start",
            "anchor_date",
            "retention_years",
            "extended_until",
            "source_reference",
            "policy_version",
            "review_note",
            "state",
            "approved_by_id",
            "approved_at",
            "retain_until",
        ]
        if "inbox_id" in self._fields:
            names.append("inbox_id")
        return {
            "format": "kmee-uae-retention-1",
            "source": {
                "model": document._name,
                "id": document.id,
                "company_id": document.company_id.id,
            },
            "obligations": metadata(obligations, names),
            "holds": metadata(
                holds,
                [
                    "company_id",
                    "retention_id",
                    "reason",
                    "source_reference",
                    "state",
                    "placed_by_id",
                    "placed_at",
                    "released_by_id",
                    "released_at",
                    "release_reason",
                ],
            ),
        }

    @api.model
    def _assess(self, document, as_of, target_field):
        self._check_current_document(document)
        try:
            assessment_date = (
                fields.Date.to_date(as_of)
                if as_of
                else fields.Date.context_today(document)
            )
        except (ValueError, TypeError) as exc:
            raise ValidationError(
                self.env._("Use a valid date for retention assessment.")
            ) from exc
        domain = [
            (target_field, "=", document.id),
            ("company_id", "=", document.company_id.id),
        ]
        # Discover only linked IDs, then enforce the current actor's rights on every
        # row. A restricted rule must not silently hide a longer obligation or hold.
        ids = self.sudo().search(domain).ids
        obligations = self.with_user(self.env.uid).browse(ids)
        obligations.check_access("read")
        hold_model = self.env["l10n_ae.edi.retention.hold"]
        hold_ids = hold_model.sudo().search([("retention_id", "in", ids)]).ids
        holds = hold_model.with_user(self.env.uid).browse(hold_ids)
        holds.check_access("read")
        active_holds = len(holds.filtered(lambda record: record.state == "active"))
        ends = [
            record.retain_until for record in obligations if record.state == "approved"
        ]
        retain_until = max(ends) if ends else False
        if active_holds:
            state = "held"
        elif not obligations:
            state = "unclassified"
        elif any(record.state != "approved" for record in obligations):
            state = "review_required"
        elif assessment_date <= retain_until:
            state = "retained"
        else:
            state = "reviewable"
        return {
            "state": state,
            "as_of": fields.Date.to_string(assessment_date),
            "retain_until": fields.Date.to_string(retain_until)
            if retain_until
            else False,
            "obligation_count": len(obligations),
            "active_hold_count": active_holds,
            "disposition_reviewable": state == "reviewable",
        }


class UaeEdiRetentionHold(models.Model):
    _name = "l10n_ae.edi.retention.hold"
    _description = "UAE Retention Hold History"
    _rec_name = "reason"
    _check_company_auto = True

    retention_id = fields.Many2one(
        "l10n_ae.edi.retention", required=True, ondelete="restrict"
    )
    company_id = fields.Many2one(
        related="retention_id.company_id", store=True, readonly=True, index=True
    )
    reason = fields.Text(required=True, readonly=True)
    source_reference = fields.Text(required=True, readonly=True)
    state = fields.Selection(
        [("active", "Active"), ("released", "Released")],
        default="active",
        required=True,
        readonly=True,
        copy=False,
    )
    placed_by_id = fields.Many2one(
        "res.users", required=True, readonly=True, ondelete="restrict", copy=False
    )
    placed_at = fields.Datetime(required=True, readonly=True, copy=False)
    released_by_id = fields.Many2one(
        "res.users", readonly=True, ondelete="restrict", copy=False
    )
    released_at = fields.Datetime(readonly=True, copy=False)
    release_reason = fields.Text(readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        policy_model = self.env["l10n_ae.edi.retention"]
        policy_model._check_current_editor()
        self.with_user(self.env.uid).check_access("create")
        protected = {
            "state",
            "company_id",
            "placed_by_id",
            "placed_at",
            "released_by_id",
            "released_at",
            "release_reason",
        }
        prepared = []
        for values in vals_list:
            vals = dict(values)
            if protected.intersection(vals):
                raise UserError(
                    self.env._(
                        "The native action records hold identity and release history."
                    )
                )
            policy = (
                policy_model.with_user(self.env.uid)
                .browse(vals.get("retention_id"))
                .exists()
            )
            if len(policy) != 1:
                raise ValidationError(
                    self.env._("A hold needs a reviewed retention obligation.")
                )
            policy.check_access("read")
            policy._check_current_document(policy._document())
            if policy.state != "approved":
                raise ValidationError(
                    self.env._("Review the retention obligation before placing a hold.")
                )
            if not all(
                (vals.get(key) or "").strip() for key in ("reason", "source_reference")
            ):
                raise ValidationError(
                    self.env._("A hold requires its reason and source reference.")
                )
            vals.update(
                state="active",
                placed_by_id=self.env.uid,
                placed_at=fields.Datetime.now(),
                released_by_id=False,
                released_at=False,
                release_reason=False,
            )
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        # pylint: disable=method-required-super
        self.env["l10n_ae.edi.retention"]._check_current_editor()
        self.with_user(self.env.uid).check_access("write")
        raise UserError(
            self.env._(
                "UAE hold history is immutable; use its reviewed release action."
            )
        )

    def action_release(self, reason):
        self.ensure_one()
        self.env["l10n_ae.edi.retention"]._check_current_editor()
        self.with_user(self.env.uid).check_access("write")
        self.retention_id._check_current_document(self.retention_id._document())
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError(
                self.env._("Provide the reviewed reason for releasing this hold.")
            )
        if self.state == "released":
            raise UserError(
                self.env._(
                    "This UAE hold has already been released; its history is immutable."
                )
            )
        return super().write(
            {
                "state": "released",
                "release_reason": reason.strip(),
                "released_by_id": self.env.uid,
                "released_at": fields.Datetime.now(),
            }
        )

    def action_open_release(self):
        self.ensure_one()
        self.env["l10n_ae.edi.retention"]._check_current_editor()
        self.with_user(self.env.uid).check_access("write")
        self.retention_id._check_current_document(self.retention_id._document())
        if self.state != "active":
            raise UserError(
                self.env._(
                    "This UAE hold has already been released; its history is immutable."
                )
            )
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Release Retention Hold"),
            "res_model": "l10n_ae.edi.retention.release.wizard",
            "view_mode": "form",
            "view_id": self.env.ref("l10n_ae_edi.edi_retention_release_wizard_form").id,
            "target": "new",
            "context": {"default_hold_id": self.id},
        }

    @api.ondelete(at_uninstall=False)
    def _unlink_except_hold_history(self):
        raise UserError(self.env._("UAE hold history cannot be deleted."))
