# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import base64
import json
from contextlib import contextmanager
from datetime import date

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user
from odoo.tools.binary import BinaryBytes

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_ae_edi.tests.common import (
    CONTROLLED_CAPABILITIES,
    review_controlled_binding,
)


def select_reviewed_calendar_fixture(incident):
    """Explicitly review the synthetic fixture through the public manager action."""
    if incident.calendar_version_id:
        return incident.calendar_version_id
    assert incident.state == "draft"
    manager = incident.env.user
    assert manager.active and manager.has_group("account.group_account_manager")
    calendar = incident.env["l10n_ae.edi.calendar"].create(
        {
            "name": "Synthetic calendar fixture for incident " + str(incident.id),
            "company_id": incident.company_id.id,
            "version": f"{incident.calendar_version}-fixture-{incident.id}",
            "calendar_start": incident.calendar_start,
            "calendar_end": incident.calendar_end,
            "calendar_holidays": incident.calendar_holidays or [],
            "source_reference": incident.calendar_source,
            "source_snapshot": json.dumps(
                {
                    "scope": "Synthetic fixture only; no official holiday assertion",
                    "source": incident.calendar_source,
                    "inline_version": incident.calendar_version,
                    "holidays": incident.calendar_holidays or [],
                },
                sort_keys=True,
            ),
            "review_note": incident.review_note,
        }
    )
    assert calendar.action_approve()
    assert calendar.approved_by_id == manager and calendar.approved_at
    incident.write({"calendar_version_id": calendar.id})
    return calendar


def open_recovery_fixture(incident):
    if incident.incident_kind == "registration_change":
        incident.record_fta_confirmation(
            {
                "evidence_key": (
                    "synthetic-recovery-registration-confirmation-"
                    + str(incident.company_id.id)
                ),
                "evidence_origin": "simulated",
                "received_at": "2026-12-01 09:00:00",
                "external_reference": "Synthetic FTA confirmation",
                "source_reference": "Supplied synthetic recovery fixture",
                "review_note": ("Reviewed declaration; no independent verification"),
                "document_filename": "synthetic-recovery-confirmation.txt",
                "document": base64.b64encode(
                    b"Synthetic FTA recovery confirmation\n"
                ).decode(),
            }
        )
    select_reviewed_calendar_fixture(incident)
    incident.action_open()


@tagged("post_install", "-at_install")
class TestUaeIncident(AccountTestInvoicingCommon):
    @contextmanager
    def _assert_user_error(self, message):
        """Require this business diagnostic, not an inherited ACL/state refusal."""
        with self.assertRaisesRegex(UserError, message) as raised:
            yield
        self.assertIs(type(raised.exception), UserError)

    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.partner_id.state_id = cls.env.ref("base.state_ae_du")

    @freeze_time("2026-12-01 12:00:00")
    def test_draft_incident_cannot_open_without_approved_calendar_version(self):
        manager = new_test_user(
            self.env,
            login="synthetic-required-calendar-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        incident = (
            self.env["l10n_ae.edi.incident"]
            .with_user(manager)
            .create(
                {
                    "name": "Synthetic inline calendar without reviewed version",
                    "affected_roles": "issuer",
                    "occurrence_at": "2026-11-30 21:30:00",
                    "detected_at": "2026-12-01 08:00:00",
                    "reason": "Synthetic failure",
                    "responsible_id": manager.id,
                    "calendar_start": "2026-12-01",
                    "calendar_end": "2026-12-10",
                    "calendar_holidays": ["2026-12-02", "2026-12-03"],
                    "calendar_source": "Typed synthetic calendar source",
                    "calendar_version": "typed-synthetic-v1",
                    "source_reference": "Reviewed MD243 art12 and date exclusion",
                    "review_note": "Dubai Dec1; Dec4 and Dec7 are working days1 and2",
                }
            )
        )
        before = incident.read(
            [
                "state",
                "write_date",
                "calendar_version_id",
                "calendar_source_sha256",
                "notification_due_date",
                "opened_by_id",
                "opened_at",
                "activity_ids",
            ]
        )
        with (
            self.cr.savepoint(),
            self.assertRaisesRegex(ValidationError, "reviewed calendar version"),
        ):
            incident.action_open()
        self.assertEqual(
            incident.read(
                [
                    "state",
                    "write_date",
                    "calendar_version_id",
                    "calendar_source_sha256",
                    "notification_due_date",
                    "opened_by_id",
                    "opened_at",
                    "activity_ids",
                ]
            ),
            before,
        )
        calendar = (
            self.env["l10n_ae.edi.calendar"]
            .with_user(manager)
            .create(
                {
                    "name": "Synthetic reviewed calendar",
                    "version": "synthetic-reviewed-v1",
                    "calendar_start": "2026-12-01",
                    "calendar_end": "2026-12-10",
                    "calendar_holidays": ["2026-12-02", "2026-12-03"],
                    "source_reference": "Synthetic source; no official holiday claim",
                    "source_snapshot": "Synthetic Dec2/3 exclusions; weekend Sat/Sun.",
                    "review_note": "Arithmetic fixture only",
                }
            )
        )
        self.assertTrue(calendar.action_approve())
        incident.write({"calendar_version_id": calendar.id})
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.notification_due_date, date(2026, 12, 7))
        self.assertEqual(len(incident.activity_ids), 1)
        opened_before = incident.read()
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.read(), opened_before)

    def test_calendar_rejects_non_list_holiday_inputs(self):
        manager = new_test_user(
            self.env,
            login="synthetic-calendar-json-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        calendars = self.env["l10n_ae.edi.calendar"].with_user(manager)
        values = {
            "name": "Synthetic invalid holiday input",
            "version": "synthetic-invalid",
            "calendar_start": "2026-12-01",
            "calendar_end": "2026-12-10",
            "source_reference": "Synthetic source",
            "source_snapshot": "Synthetic snapshot",
            "review_note": "Synthetic input qualification; no official dates",
        }
        for holidays in ({}, 0, "", {"dates": []}):
            with (
                self.subTest(holidays=holidays),
                self.cr.savepoint(),
                self.assertRaisesRegex(ValidationError, "list"),
            ):
                calendar = calendars.create(dict(values, calendar_holidays=holidays))
                calendar.action_approve()
        calendar = calendars.create(dict(values, calendar_holidays=[]))
        for holidays in ({}, 0, "", {"dates": []}):
            with (
                self.subTest(write_holidays=holidays),
                self.cr.savepoint(),
                self.assertRaisesRegex(ValidationError, "list"),
            ):
                calendar.write({"calendar_holidays": holidays})
                calendar.action_approve()
        self.assertEqual(calendar.state, "draft")
        self.assertTrue(calendar.action_approve())

    def test_calendar_selection_rechecks_actor_and_company_under_sudo(self):
        company_a = self.env.company
        company_b = self.setup_other_company(name="Synthetic calendar company B")[
            "company"
        ]
        company_b.country_id = self.env.ref("base.ae")
        manager = new_test_user(
            self.env,
            login="synthetic-calendar-two-company-manager",
            groups="account.group_account_manager",
            company_ids=(company_a | company_b).ids,
            company_id=company_a.id,
        )
        reader = new_test_user(
            self.env,
            login="synthetic-calendar-company-reader",
            groups="account.group_account_readonly",
            company_ids=company_a.ids,
            company_id=company_a.id,
        )

        def approve_calendar(company, allowed_company_ids, suffix):
            calendar = (
                self.env["l10n_ae.edi.calendar"]
                .with_user(manager)
                .with_context(allowed_company_ids=allowed_company_ids)
                .create(
                    {
                        "name": "Synthetic calendar " + suffix,
                        "company_id": company.id,
                        "version": "synthetic-calendar-" + suffix,
                        "calendar_start": "2026-12-01",
                        "calendar_end": "2026-12-10",
                        "calendar_holidays": ["2026-12-02"],
                        "source_reference": "Synthetic source",
                        "source_snapshot": "Synthetic source snapshot " + suffix,
                        "review_note": "Authorization qualification only",
                    }
                )
            )
            self.assertTrue(calendar.action_approve())
            return calendar

        calendar_a = approve_calendar(company_a, company_a.ids, "a")
        calendar_b = approve_calendar(company_b, company_b.ids, "b")
        incidents = self.env["l10n_ae.edi.incident"].with_user(manager)
        allowed_a = {"allowed_company_ids": company_a.ids}
        target = incidents.with_context(**allowed_a).create(
            {"name": "Synthetic A selection target", "company_id": company_a.id}
        )
        same_company = incidents.with_context(**allowed_a).create(
            {"name": "Synthetic A positive control", "company_id": company_a.id}
        )

        # An ordinary same-company selection succeeds and copies reviewed facts.
        same_company.write({"calendar_version_id": calendar_a.id})
        self.assertEqual(same_company.calendar_version_id, calendar_a)
        self.assertEqual(same_company.calendar_start, calendar_a.calendar_start)
        self.assertEqual(same_company.calendar_end, calendar_a.calendar_end)
        self.assertEqual(same_company.calendar_holidays, ["2026-12-02"])
        self.assertEqual(same_company.calendar_source, calendar_a.source_reference)
        self.assertEqual(same_company.calendar_version, calendar_a.version)
        self.assertEqual(same_company.calendar_source_sha256, calendar_a.source_sha256)

        fields_to_compare = [
            "state",
            "company_id",
            "calendar_version_id",
            "calendar_start",
            "calendar_end",
            "calendar_holidays",
            "calendar_source",
            "calendar_version",
            "calendar_source_sha256",
            "notification_due_date",
            "opened_by_id",
            "opened_at",
            "activity_ids",
            "write_date",
        ]
        before = target.read(fields_to_compare)

        # The manager belongs to B, but B is not active in this A-only context.
        for elevated in (False, True):
            with (
                self.subTest(active_companies="A", manager_sudo=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                target.with_context(**allowed_a).sudo(elevated).write(
                    {"calendar_version_id": calendar_b.id}
                )
            self.assertEqual(target.read(fields_to_compare), before)

        # B is readable when active; attaching it to the A incident still fails.
        allowed_both = {"allowed_company_ids": (company_a | company_b).ids}
        for elevated in (False, True):
            with (
                self.subTest(active_companies="A+B", manager_sudo=elevated),
                self.cr.savepoint(),
                self.assertRaisesRegex(
                    ValidationError, "Select a reviewed calendar in this company"
                ),
            ):
                target.with_context(**allowed_both).sudo(elevated).write(
                    {"calendar_version_id": calendar_b.id}
                )
            self.assertEqual(target.read(fields_to_compare), before)

        # Sudo cannot turn a readonly same-company user into an incident manager.
        with self.cr.savepoint(), self.assertRaises(AccessError):
            target.with_user(reader).with_context(**allowed_a).sudo().write(
                {"calendar_version_id": calendar_a.id}
            )
        self.assertEqual(target.read(fields_to_compare), before)

    def test_calendar_context_default_cannot_select_unapproved_version(self):
        manager = new_test_user(
            self.env,
            login="synthetic-calendar-default-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        calendar = (
            self.env["l10n_ae.edi.calendar"]
            .with_user(manager)
            .create(
                {
                    "name": "Synthetic unreviewed calendar",
                    "version": "synthetic-draft",
                }
            )
        )
        incidents = self.env["l10n_ae.edi.incident"].with_user(manager)
        for defaults in (False, True):
            with self.subTest(context_default=defaults):
                target = (
                    incidents.with_context(default_calendar_version_id=calendar.id)
                    if defaults
                    else incidents
                )
                values = {"name": "Synthetic unapproved selection"}
                if not defaults:
                    values["calendar_version_id"] = calendar.id
                with (
                    self.cr.savepoint(),
                    self.assertRaisesRegex(ValidationError, "reviewed calendar"),
                ):
                    target.create(values)
        self.assertFalse(
            incidents.search([("name", "=", "Synthetic unapproved selection")])
        )

    @freeze_time("2026-12-01 12:00:00")
    def test_incident_form_selects_approved_calendar_and_freezes_basis(self):
        manager = new_test_user(
            self.env,
            login="synthetic-approved-calendar-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        calendar_model = self.env["l10n_ae.edi.calendar"].with_user(manager)
        with Form(calendar_model, view="l10n_ae_edi.edi_calendar_form") as form:
            form.name = "Synthetic calendar; arithmetic fixture only"
            form.version = "synthetic-2026-v1"
            form.calendar_start = date(2026, 12, 1)
            form.calendar_end = date(2026, 12, 10)
            form.calendar_holiday_dates = "2026-12-02\n2026-12-03"
            form.source_reference = "Synthetic source; no official holiday claim"
            form.source_snapshot = "Synthetic Dec2/3 holidays, Saturday/Sunday weekend."
            form.review_note = "Arithmetic qualification only; not for production"
        calendar = form.record
        self.assertEqual(calendar.state, "draft")
        self.assertFalse(calendar.approved_by_id)
        self.assertFalse(calendar.approved_at)
        self.assertTrue(calendar.action_approve())
        self.assertEqual(calendar.approved_by_id, manager)
        self.assertEqual(calendar.approved_at, fields.Datetime.now())
        approved_before = calendar.read()
        self.assertTrue(calendar.action_approve())
        self.assertEqual(calendar.read(), approved_before)
        with self.cr.savepoint(), self.assertRaises(UserError):
            calendar.write({"calendar_holiday_dates": "2026-12-04"})

        with Form(
            self.env["l10n_ae.edi.incident"].with_user(manager),
            view="l10n_ae_edi.edi_incident_form",
        ) as form:
            form.name = "Synthetic failure using approved calendar"
            form.affected_roles = "issuer"
            form.occurrence_at = fields.Datetime.to_datetime("2026-11-30 21:30:00")
            form.detected_at = fields.Datetime.to_datetime("2026-12-01 08:00:00")
            form.reason = "Synthetic controlled failure"
            form.responsible_id = manager
            form.calendar_version_id = calendar
            form.source_reference = "Reviewed MD243 art12 and date exclusion"
            form.review_note = "Dubai Dec1; Dec4 and Dec7 are working days1 and2"
        incident = form.record
        self.assertEqual(incident.calendar_start, date(2026, 12, 1))
        self.assertEqual(incident.calendar_end, date(2026, 12, 10))
        self.assertEqual(incident.calendar_holidays, ["2026-12-02", "2026-12-03"])
        self.assertEqual(incident.calendar_source, calendar.source_reference)
        self.assertEqual(incident.calendar_version, "synthetic-2026-v1")
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.notification_due_date, date(2026, 12, 7))
        self.assertEqual(incident.opened_by_id, manager)
        self.assertEqual(len(incident.activity_ids), 1)
        self.assertEqual(incident.activity_ids.date_deadline, date(2026, 12, 7))
        before = incident.read()
        activity_before = incident.activity_ids.read()
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.read(), before)
        self.assertEqual(incident.activity_ids.read(), activity_before)
        reviewed = Form(incident, view="l10n_ae_edi.edi_incident_form")
        self.assertEqual(reviewed.calendar_version_id, calendar)
        with self.assertRaises(AssertionError):
            reviewed.calendar_version_id = False

    @freeze_time("2026-12-08 12:00:00")
    def test_new_registration_cannot_open_from_typed_receipt_without_original(self):
        manager = new_test_user(
            self.env,
            login="synthetic-confirmation-intake-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        incident = (
            self.env["l10n_ae.edi.incident"]
            .with_user(manager)
            .create(
                {
                    "name": "Synthetic new registration requiring original",
                    "incident_kind": "registration_change",
                    "fta_confirmation_received_at": "2026-11-30 21:30:00",
                    "fta_confirmation_reference": "Synthetic FTA registration receipt",
                    "reason": "Synthetic address amendment",
                    "responsible_id": manager.id,
                    "calendar_start": "2026-12-01",
                    "calendar_end": "2026-12-10",
                    "calendar_holidays": ["2026-12-02", "2026-12-03"],
                    "calendar_source": "Reviewed synthetic federal calendar",
                    "calendar_version": "synthetic-2026",
                    "source_reference": "Reviewed MD243 Art5.3 and date exclusion",
                    "review_note": "Dubai Dec1; Dec4,7,8,9,10 are days1 through5",
                }
            )
        )
        select_reviewed_calendar_fixture(incident)
        before = incident.read(
            [
                "state",
                "write_date",
                "notification_due_date",
                "activity_ids",
                "opened_at",
                "fta_confirmation_received_at",
                "fta_confirmation_reference",
            ]
        )
        with (
            self.cr.savepoint(),
            self.assertRaisesRegex(ValidationError, "original FTA confirmation"),
        ):
            incident.action_open()
        self.assertEqual(
            incident.read(
                [
                    "state",
                    "write_date",
                    "notification_due_date",
                    "activity_ids",
                    "opened_at",
                    "fta_confirmation_received_at",
                    "fta_confirmation_reference",
                ]
            ),
            before,
        )
        incident.record_fta_confirmation(
            {
                "evidence_key": "synthetic-intake-original",
                "evidence_origin": "simulated",
                "received_at": "2026-11-30 21:30:00",
                "external_reference": "Synthetic FTA registration receipt",
                "source_reference": "Supplied synthetic original",
                "review_note": "Reviewed declaration; no independent verification",
                "document_filename": "synthetic-intake-confirmation.txt",
                "document": base64.b64encode(
                    b"Synthetic FTA registration confirmation\n"
                ).decode(),
            }
        )
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.notification_due_date, date(2026, 12, 10))
        self.assertEqual(len(incident.activity_ids), 1)
        self.assertEqual(len(incident.fta_confirmation_ids), 1)

    @freeze_time("2026-12-08 12:00:00")
    def test_confirmation_capture_refuses_forged_facts_and_unauthorized_original_reads(
        self,
    ):
        company_a = self.env.company
        company_b = self.setup_other_company(
            name="Synthetic FTA confirmation company B"
        )["company"]
        company_b.country_id = self.env.ref("base.ae")
        manager = new_test_user(
            self.env,
            login="synthetic-confirmation-access-manager",
            groups="account.group_account_manager",
            company_ids=(company_a | company_b).ids,
            company_id=company_a.id,
        )
        reader = new_test_user(
            self.env,
            login="synthetic-confirmation-access-reader",
            groups="account.group_account_readonly",
            company_ids=company_a.ids,
            company_id=company_a.id,
        )
        model = self.env["l10n_ae.edi.incident"].with_user(manager)
        case = model.with_context(allowed_company_ids=company_a.ids).create(
            {
                "name": "Synthetic original confirmation access qualification",
                "incident_kind": "registration_change",
            }
        )
        foreign = model.with_context(allowed_company_ids=company_b.ids).create(
            {
                "name": "Synthetic other-company original confirmation",
                "incident_kind": "registration_change",
            }
        )
        failure = model.with_context(allowed_company_ids=company_a.ids).create(
            {
                "name": "Synthetic wrong incident kind for FTA confirmation",
            }
        )
        evidence = {
            "evidence_key": "synthetic-confirmation-access-original",
            "evidence_origin": "simulated",
            "received_at": "2026-11-30 21:30:00",
            "external_reference": "Synthetic FTA registration receipt",
            "source_reference": "Supplied controlled original",
            "review_note": "No real FTA authorship or receipt verified",
            "document_filename": "synthetic-fta-confirmation.txt",
            "document": base64.b64encode(
                b"Synthetic FTA registration confirmation\n"
            ).decode(),
        }
        before = case.read(
            [
                "state",
                "write_date",
                "fta_confirmation_received_at",
                "fta_confirmation_reference",
                "activity_ids",
            ]
        )
        for changed in (
            {"recorded_by_id": manager.id},
            {"company_id": company_b.id},
            {"document_sha256": "forged"},
            {"received_at": "2026-12-09 08:00:00"},
            {"received_at": "2026-12-08T08:00:00+00:00"},
            {"document_filename": "../original.txt"},
            {"document_filename": "bad\nname.txt"},
            {"evidence_origin": "verified_fta"},
            {"document": ""},
            {"document": "not Base64!"},
            {"document": "A" * 13981017},
        ):
            with (
                self.subTest(changed=list(changed)),
                self.cr.savepoint(),
                self.assertRaises(ValidationError),
            ):
                case.record_fta_confirmation({**evidence, **changed})
        for elevated in (False, True):
            with (
                self.subTest(reader=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                case.with_user(reader).sudo(elevated).record_fta_confirmation(evidence)
            with (
                self.subTest(foreign=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                foreign.with_context(allowed_company_ids=company_a.ids).sudo(
                    elevated
                ).record_fta_confirmation(evidence)
            with (
                self.subTest(wrong_kind=elevated),
                self.cr.savepoint(),
                self._assert_user_error("registration amendment incident"),
            ):
                failure.sudo(elevated).record_fta_confirmation(evidence)
        self.assertFalse(case.fta_confirmation_ids)
        self.assertFalse(foreign.fta_confirmation_ids)
        self.assertEqual(
            case.read(
                [
                    "state",
                    "write_date",
                    "fta_confirmation_received_at",
                    "fta_confirmation_reference",
                    "activity_ids",
                ]
            ),
            before,
        )
        action = case.action_record_fta_confirmation()
        wizard = (
            self.env[action["res_model"]]
            .with_user(manager)
            .with_context(**action["context"])
            .create(
                {
                    **{
                        key: value
                        for key, value in evidence.items()
                        if key != "document"
                    },
                    "document": BinaryBytes(
                        b"Synthetic FTA registration confirmation\n"
                    ),
                }
            )
        )
        parameters = self.env["ir.config_parameter"].sudo()
        self.assertFalse(parameters.get_bool("database.is_neutralized"))
        parameters.set_bool("database.is_neutralized", True)
        try:
            for elevated in (False, True):
                with (
                    self.subTest(neutralized_launch=elevated),
                    self.cr.savepoint(),
                    self._assert_user_error("disabled on a neutralized copy"),
                ):
                    case.sudo(elevated).action_record_fta_confirmation()
                with (
                    self.subTest(neutralized_capture=elevated),
                    self.cr.savepoint(),
                    self._assert_user_error("disabled on a neutralized copy"),
                ):
                    wizard.sudo(elevated).action_confirm()
        finally:
            parameters.set_bool("database.is_neutralized", False)
        self.assertFalse(case.fta_confirmation_ids)
        wizard.action_confirm()
        original = case.fta_confirmation_ids
        foreign.record_fta_confirmation(evidence)
        foreign_original = foreign.fta_confirmation_ids
        for elevated in (False, True):
            with (
                self.subTest(foreign_download=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                _ = (
                    foreign_original.with_context(allowed_company_ids=company_a.ids)
                    .sudo(elevated)
                    .document
                )
            with (
                self.subTest(reader_replay=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                case.with_user(reader).sudo(elevated).record_fta_confirmation(evidence)
            with (
                self.subTest(raw_orm_create=elevated),
                self.cr.savepoint(),
                self._assert_user_error("incident FTA confirmation action"),
            ):
                self.env[original._name].with_user(manager).sudo(elevated).create(
                    {"incident_id": case.id}
                )
            with (
                self.subTest(reparent=elevated),
                self.cr.savepoint(),
                self._assert_user_error("confirmation evidence is immutable"),
            ):
                original.sudo(elevated).write({"incident_id": foreign.id})
        with self.cr.savepoint(), self.assertRaises(AccessError):
            original.with_user(reader).read(["document_content"])
        # Corrupt storage only as a fixture fault, then exercise the public binary read.
        with self.cr.savepoint(), self._assert_user_error("failed integrity review"):
            self.cr.execute(
                "UPDATE l10n_ae_edi_incident_confirmation "
                "SET document_content = %s WHERE id = %s",
                (b"Corrupt fixture original", original.id),
            )
            original.invalidate_recordset(["document_content", "document"])
            _ = original.with_user(reader).document
        self.assertEqual(
            original.with_user(reader).document.content,
            b"Synthetic FTA registration confirmation\n",
        )
        self.assertEqual(len(case.fta_confirmation_ids), 1)
        self.assertFalse(case.activity_ids)
        self.assertFalse(case.notice_ids)

    @freeze_time("2026-12-08 12:00:00")
    def test_manager_retains_fta_confirmation_and_binds_registration_clock(self):
        manager = new_test_user(
            self.env,
            login="synthetic-fta-confirmation-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        reader = new_test_user(
            self.env,
            login="synthetic-fta-confirmation-reader",
            groups="account.group_account_readonly",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        incident = (
            self.env["l10n_ae.edi.incident"]
            .with_user(manager)
            .create(
                {
                    "name": "Synthetic supplied FTA registration confirmation",
                    "incident_kind": "registration_change",
                    "reason": "Synthetic address amendment confirmation received",
                    "responsible_id": manager.id,
                    "calendar_start": "2026-12-01",
                    "calendar_end": "2026-12-10",
                    "calendar_holidays": ["2026-12-02", "2026-12-03"],
                    "calendar_source": "Reviewed synthetic federal holidays",
                    "calendar_version": "synthetic-federal-2026",
                    "source_reference": "Reviewed MD243 Art5.3 and day exclusion",
                    "review_note": "Dubai Dec1 receipt; Dec4,7,8,9,10 are days1-5",
                }
            )
        )
        content = b"Synthetic FTA registration confirmation\n"
        digest = "203b34111aa50c86b0fafe85c264901e9d46c29363082ed659e584822730b639"
        evidence = {
            "evidence_key": "synthetic-fta-confirmation-A",
            "evidence_origin": "simulated",
            "received_at": "2026-11-30 21:30:00",
            "external_reference": "Synthetic FTA confirmation A",
            "source_reference": "Supplied synthetic confirmation file",
            "review_note": "Receipt reviewed; no authorship or delivery verification",
            "document_filename": "synthetic-fta-confirmation.txt",
            "document": base64.b64encode(content).decode(),
        }
        result = incident.record_fta_confirmation(evidence)
        confirmation = incident.fta_confirmation_ids
        self.assertEqual(result["id"], confirmation.id)
        self.assertEqual(result["document_sha256"], digest)
        self.assertEqual(len(confirmation), 1)
        self.assertEqual(confirmation.document.content, content)
        self.assertEqual(confirmation.document_sha256, digest)
        self.assertEqual(confirmation.evidence_origin, "simulated")
        self.assertEqual(confirmation.recorded_by_id, manager)
        self.assertEqual(confirmation.recorded_at, fields.Datetime.now())
        self.assertEqual(
            incident.fta_confirmation_received_at,
            fields.Datetime.to_datetime("2026-11-30 21:30:00"),
        )
        self.assertEqual(
            incident.fta_confirmation_reference, "Synthetic FTA confirmation A"
        )
        self.assertEqual(incident.state, "draft")
        self.assertFalse(incident.notification_due_date)
        self.assertFalse(incident.activity_ids)
        self.assertFalse(incident.notice_ids)
        recorded = incident.read(["write_date"])
        self.assertEqual(incident.record_fta_confirmation(evidence), result)
        self.assertEqual(incident.read(["write_date"]), recorded)
        for field, value in (
            ("fta_confirmation_received_at", "2026-12-08 08:00:00"),
            ("fta_confirmation_reference", "Changed reference"),
            ("incident_kind", "system_failure"),
        ):
            with (
                self.subTest(field=field),
                self.cr.savepoint(),
                self.assertRaises(UserError),
            ):
                incident.write({field: value})
        for changed in (
            {"evidence_key": "different-key"},
            {"document": base64.b64encode(b"Different original").decode()},
            {"received_at": "2026-12-01 10:00:00"},
        ):
            with (
                self.subTest(changed=changed),
                self.cr.savepoint(),
                self.assertRaises(UserError),
            ):
                incident.record_fta_confirmation({**evidence, **changed})
        for elevated in (False, True):
            with (
                self.subTest(reader_sudo=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                incident.with_user(reader).sudo(elevated).record_fta_confirmation(
                    evidence
                )
        self.assertEqual(confirmation.with_user(reader).document.content, content)
        with self.cr.savepoint(), self.assertRaises(UserError):
            confirmation.write({"external_reference": "Changed"})
        with self.cr.savepoint(), self.assertRaises((AccessError, UserError)):
            confirmation.unlink()
        select_reviewed_calendar_fixture(incident)
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.notification_due_date, date(2026, 12, 10))
        self.assertEqual(incident.activity_ids.date_deadline, date(2026, 12, 10))
        opened = incident.read(
            ["state", "write_date", "notification_due_date", "activity_ids"]
        )
        self.assertEqual(incident.record_fta_confirmation(evidence), result)
        self.assertEqual(
            incident.read(
                ["state", "write_date", "notification_due_date", "activity_ids"]
            ),
            opened,
        )
        self.assertEqual(incident.fta_confirmation_ids, confirmation)
        self.assertFalse(incident.notice_ids)

    @freeze_time("2026-12-08 12:00:00")
    def test_manager_retains_reviewed_outage_impacts_without_new_submissions(self):
        manager = new_test_user(
            self.env,
            login="synthetic-outage-impact-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        self.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        self.partner_a.country_id = self.env.ref("base.ae")
        self.partner_a.state_id = self.env.ref("base.state_ae_du")
        if "routing_scheme" in self.env["res.partner"]._fields:
            self.env.company.partner_id.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000001"}
            )
            self.partner_a.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000002"}
            )
        binding = review_controlled_binding(
            self.env["l10n_ae.edi.binding"]
            .with_user(manager)
            .create({"name": "Synthetic outage binding"}),
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic reviewed controlled contract fixture.",
        )
        invoices = self.env["account.move"]
        for amount in (100, 200):
            invoice = self.init_invoice(
                "out_invoice", amounts=[amount], taxes=self.tax_sale_a, post=True
            ).with_user(manager)
            invoice.l10n_ae_binding_id = binding
            invoice.action_l10n_ae_prepare()
            invoices |= invoice
        issues = invoices.mapped("l10n_ae_transmission_id").with_user(manager)
        self.assertEqual(invoices.mapped("amount_total"), [105, 210])
        incident = (
            self.env["l10n_ae.edi.incident"]
            .with_user(manager)
            .create(
                {
                    "name": "Synthetic outage with later affected documents",
                    "affected_roles": "issuer",
                    "occurrence_at": "2026-12-01 09:00:00",
                    "detected_at": "2026-12-01 10:00:00",
                    "reason": "Controlled service unavailable",
                    "responsible_id": manager.id,
                    "calendar_start": "2026-12-01",
                    "calendar_end": "2026-12-10",
                    "calendar_holidays": [],
                    "calendar_source": "Reviewed synthetic calendar",
                    "calendar_version": "synthetic-2026",
                    "source_reference": "Reviewed MD243 Art12",
                    "review_note": "Local outage review; no recovery inferred",
                }
            )
        )
        select_reviewed_calendar_fixture(incident)
        incident.action_open()
        basis = incident.read(
            ["state", "opened_at", "notification_due_date", "write_date"]
        )
        activity = incident.activity_ids.read(["user_id", "date_deadline", "summary"])
        accounting = invoices.read(["state", "amount_total", "write_date", "line_ids"])
        issue_fields = [
            "idempotency_key",
            "payload_hash",
            "xml_hash",
            "submission_state",
            "confirmation_state",
            "attempt_ids",
            "remote_id",
            "write_date",
        ]
        issue_facts = issues.read(issue_fields)
        evidence = {
            "evidence_key": "outage-impact-first",
            "transmission_ids": issues[:1].ids,
            "source_reference": "Controlled outage affected-document inventory A",
            "review_note": "First reviewed affected invoice; no HTTP action",
        }
        result = incident.record_affected_transmissions(evidence)
        batch = incident.impact_ids
        self.assertEqual(result["id"], batch.id)
        self.assertEqual(batch.transmission_ids, issues[:1])
        self.assertEqual(batch.recorded_by_id, manager)
        self.assertEqual(batch.recorded_at, fields.Datetime.now())
        self.assertEqual(
            batch.identity_snapshot,
            [
                {
                    "transmission_id": issues[0].id,
                    "move_id": invoices[0].id,
                    "binding_id": binding.id,
                    "process": "billing",
                    "revision": 1,
                    "idempotency_key": issue_facts[0]["idempotency_key"],
                    "payload_hash": issue_facts[0]["payload_hash"],
                    "xml_hash": issue_facts[0]["xml_hash"],
                }
            ],
        )
        self.assertEqual(incident.record_affected_transmissions(evidence), result)
        self.assertEqual(incident.impact_ids, batch)
        changed = dict(evidence, review_note="Conflicting review under original key")
        with self.cr.savepoint(), self.assertRaises(ValidationError):
            incident.record_affected_transmissions(changed)
        incident.record_affected_transmissions(
            dict(
                evidence,
                evidence_key="outage-impact-later",
                transmission_ids=issues[1:].ids,
                source_reference="Controlled outage affected-document inventory B",
            )
        )
        self.assertEqual(len(incident.impact_ids), 2)
        self.assertEqual(incident.impact_ids.mapped("transmission_ids"), issues)
        self.assertEqual(
            incident.read(
                ["state", "opened_at", "notification_due_date", "write_date"]
            ),
            basis,
        )
        self.assertEqual(
            incident.activity_ids.read(["user_id", "date_deadline", "summary"]),
            activity,
        )
        self.assertEqual(
            invoices.read(["state", "amount_total", "write_date", "line_ids"]),
            accounting,
        )
        self.assertEqual(issues.read(issue_fields), issue_facts)
        self.assertEqual(json.loads(issues[0].payload)["process"], "billing")
        with self.cr.savepoint(), self.assertRaises(UserError):
            batch.write({"review_note": "Rewritten review"})
        with self.cr.savepoint(), self.assertRaises(UserError):
            batch.unlink()

    @freeze_time("2026-12-08 12:00:00")
    def test_reviewed_restoration_keeps_submission_and_confirmation_backlog_visible(
        self,
    ):
        manager = new_test_user(
            self.env,
            login="synthetic-restoration-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        self.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        self.partner_a.country_id = self.env.ref("base.ae")
        self.partner_a.state_id = self.env.ref("base.state_ae_du")
        if "routing_scheme" in self.env["res.partner"]._fields:
            self.env.company.partner_id.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000001"}
            )
            self.partner_a.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000002"}
            )
        binding = review_controlled_binding(
            self.env["l10n_ae.edi.binding"].create(
                {"name": "Synthetic restoration binding"}
            ),
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic reviewed controlled contract fixture.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        ).with_user(manager)
        invoice.l10n_ae_binding_id = binding
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        incident = (
            self.env["l10n_ae.edi.incident"]
            .with_user(manager)
            .create(
                {
                    "name": "Synthetic restored service with pending obligations",
                    "affected_roles": "issuer",
                    "occurrence_at": "2026-12-01 09:00:00",
                    "detected_at": "2026-12-01 10:00:00",
                    "reason": "Controlled outage",
                    "responsible_id": manager.id,
                    "calendar_start": "2026-12-01",
                    "calendar_end": "2026-12-10",
                    "calendar_holidays": [],
                    "calendar_source": "Reviewed synthetic calendar",
                    "calendar_version": "synthetic-2026",
                    "source_reference": "Reviewed MD243 Art12",
                    "review_note": "Service restoration is not fiscal confirmation",
                }
            )
        )
        select_reviewed_calendar_fixture(incident)
        incident.action_open()
        incident.record_affected_transmissions(
            {
                "evidence_key": "restoration-impact-one",
                "transmission_ids": issue.ids,
                "source_reference": "Reviewed controlled affected-document inventory",
                "review_note": "Queued original still requires processing",
            }
        )
        before = incident.get_recovery_backlog()
        self.assertEqual(before["affected_count"], 1)
        self.assertEqual(before["submission_pending_count"], 1)
        self.assertEqual(before["confirmation_pending_count"], 1)
        self.assertFalse(before["restoration_reviewed"])
        invoice_before = invoice.read(
            ["state", "amount_total", "line_ids", "write_date"]
        )
        activity_before = incident.activity_ids.read(
            ["user_id", "date_deadline", "summary"]
        )
        basis_before = incident.read(
            ["state", "opened_at", "notification_due_date", "write_date"]
        )
        declaration = {
            "evidence_key": "restoration-review-one",
            "evidence_origin": "simulated",
            "restored_at": "2026-12-08 10:00:00",
            "source_reference": "Controlled service health check reference A",
            "review_note": "Local review only; all confirmations remain pending",
        }
        for timestamp in ("2026-12-01 08:59:59", "2026-12-08 12:00:01"):
            with self.cr.savepoint(), self.assertRaises(ValidationError):
                incident.record_restoration(dict(declaration, restored_at=timestamp))
        result = incident.record_restoration(declaration)
        self.assertEqual(incident.record_restoration(declaration), result)
        with self.cr.savepoint(), self.assertRaises(ValidationError):
            incident.record_restoration(dict(declaration, evidence_origin="external"))
        review = incident.restoration_ids
        self.assertEqual(len(review), 1)
        self.assertEqual(review.recorded_by_id, manager)
        self.assertEqual(review.recorded_at, fields.Datetime.now())
        self.assertEqual(
            review.restored_at, fields.Datetime.to_datetime("2026-12-08 10:00:00")
        )
        after = incident.get_recovery_backlog()
        self.assertTrue(after["restoration_reviewed"])
        self.assertEqual(after["transmissions"], before["transmissions"])
        self.assertEqual(after["submission_pending_count"], 1)
        self.assertEqual(after["confirmation_pending_count"], 1)
        row = after["transmissions"][0]
        self.assertEqual(row["submission_state"], "queued")
        self.assertEqual(row["confirmation_state"], "pending")
        self.assertEqual(row["validation_c2_state"], "unknown")
        self.assertEqual(row["delivery_c3_state"], "unknown")
        self.assertEqual(row["reporting_c2_state"], "unknown")
        self.assertEqual(row["reporting_c3_state"], "unknown")
        self.assertEqual(row["idempotency_key"], issue.idempotency_key)
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.remote_id)
        self.assertEqual(
            invoice.read(["state", "amount_total", "line_ids", "write_date"]),
            invoice_before,
        )
        self.assertEqual(
            incident.activity_ids.read(["user_id", "date_deadline", "summary"]),
            activity_before,
        )
        self.assertEqual(
            incident.read(
                ["state", "opened_at", "notification_due_date", "write_date"]
            ),
            basis_before,
        )
        with self.cr.savepoint(), self.assertRaises(UserError):
            review.write({"restored_at": "2026-12-08 11:00:00"})
        with self.cr.savepoint(), self.assertRaises(UserError):
            review.unlink()

    @freeze_time("2026-12-08 12:00:00")
    def test_recovery_dialogs_recheck_actual_actor_company_and_readonly_overview(self):
        company_a = self.env.company
        company_b = self.setup_other_company(name="Synthetic recovery company B")[
            "company"
        ]
        company_b.country_id = self.env.ref("base.ae")
        manager = new_test_user(
            self.env,
            login="synthetic-recovery-access-manager",
            groups="account.group_account_manager",
            company_ids=[company_a.id, company_b.id],
            company_id=company_a.id,
        )
        reader = new_test_user(
            self.env,
            login="synthetic-recovery-access-reader",
            groups="account.group_account_readonly",
            company_ids=company_a.ids,
            company_id=company_a.id,
        )

        def create_incident(company, name, kind="system_failure", open_record=True):
            values = {
                "name": name,
                "company_id": company.id,
                "incident_kind": kind,
                "affected_roles": "issuer" if kind == "system_failure" else False,
                "occurrence_at": (
                    "2026-12-01 09:00:00" if kind == "system_failure" else False
                ),
                "detected_at": (
                    "2026-12-01 10:00:00" if kind == "system_failure" else False
                ),
                "fta_confirmation_received_at": (
                    "2026-12-01 09:00:00" if kind == "registration_change" else False
                ),
                "fta_confirmation_reference": (
                    "Synthetic FTA confirmation"
                    if kind == "registration_change"
                    else False
                ),
                "reason": "Synthetic reviewed outage or registration change",
                "responsible_id": manager.id,
                "calendar_start": "2026-12-01",
                "calendar_end": "2026-12-10",
                "calendar_holidays": [],
                "calendar_source": "Reviewed synthetic calendar",
                "calendar_version": "synthetic-recovery-access-1",
                "source_reference": "Synthetic reviewed source reference",
                "review_note": "Synthetic local review; no external action",
            }
            incident = (
                self.env["l10n_ae.edi.incident"]
                .with_user(manager)
                .with_context(allowed_company_ids=company.ids)
                .create(values)
            )
            if open_record:
                open_recovery_fixture(incident)
            return incident

        system_a = create_incident(company_a, "Synthetic open system incident A")
        draft_a = create_incident(
            company_a, "Synthetic draft system incident A", open_record=False
        )
        registration_a = create_incident(
            company_a,
            "Synthetic open registration incident A",
            kind="registration_change",
        )
        system_b = create_incident(company_b, "Synthetic open system incident B")

        company_a.write({"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True})
        self.partner_a.country_id = self.env.ref("base.ae")
        self.partner_a.state_id = self.env.ref("base.state_ae_du")
        if "routing_scheme" in self.env["res.partner"]._fields:
            company_a.partner_id.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000001"}
            )
            self.partner_a.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000002"}
            )

        binding = review_controlled_binding(
            self.env["l10n_ae.edi.binding"]
            .with_user(manager)
            .with_context(allowed_company_ids=company_a.ids)
            .create({"name": "Synthetic recovery access binding"}),
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic reviewed controlled contract fixture.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        ).with_user(manager)
        invoice.l10n_ae_binding_id = binding
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id

        def new_impact_wizard(incident=system_a, context=None):
            action = incident.with_user(manager).action_review_impact()
            dialog_context = {
                **action["context"],
                **(context or {}),
                "allowed_company_ids": company_a.ids,
            }
            return (
                self.env[action["res_model"]]
                .with_user(manager)
                .with_context(**dialog_context)
                .create(
                    {
                        "transmission_ids": [(6, 0, issue.ids)],
                        "source_reference": "Synthetic affected-document inventory",
                        "review_note": "Original issue remains queued for review",
                    }
                )
            )

        def new_restoration_wizard(incident=system_a, context=None):
            action = incident.with_user(manager).action_review_restoration()
            dialog_context = {
                **action["context"],
                **(context or {}),
                "allowed_company_ids": company_a.ids,
            }
            return (
                self.env[action["res_model"]]
                .with_user(manager)
                .with_context(**dialog_context)
                .create(
                    {
                        "evidence_origin": "simulated",
                        "restored_at": "2026-12-08 10:00:00",
                        "source_reference": "Synthetic local service-health review",
                        "review_note": "Restoration leaves fiscal obligations pending",
                    }
                )
            )

        impact_wizard = new_impact_wizard()
        restoration_wizard = new_restoration_wizard()
        incidents = (system_a, draft_a, registration_a, system_b)
        history_before = {
            model: self.env[model].search([], order="id").ids
            for model in (
                "l10n_ae.edi.incident.impact",
                "l10n_ae.edi.incident.restoration",
                "l10n_ae.edi.attempt",
                "l10n_ae.edi.event",
            )
        }
        incident_before = [
            row.read(["state", "write_date", "opened_at", "notification_due_date"])
            for row in incidents
        ]
        activity_before = [row.activity_ids.ids for row in incidents]
        invoice_before = invoice.read(
            ["state", "amount_total", "line_ids", "write_date"]
        )
        issue_before = issue.read(
            [
                "submission_state",
                "confirmation_state",
                "idempotency_key",
                "payload_hash",
                "xml_hash",
                "attempt_ids",
                "remote_id",
                "write_date",
            ]
        )

        # Readonly users cannot launch or confirm, including sudoed recordsets.
        for elevated in (False, True):
            with (
                self.subTest(readonly_launch=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                system_a.with_user(reader).sudo(elevated).action_review_impact()
            with (
                self.subTest(readonly_restoration_launch=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                system_a.with_user(reader).sudo(elevated).action_review_restoration()
            with (
                self.subTest(readonly_impact_confirm=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                impact_wizard.with_user(reader).sudo(elevated).action_confirm()
            with (
                self.subTest(readonly_restoration_confirm=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                restoration_wizard.with_user(reader).sudo(elevated).action_confirm()

        # Public ORM retargets still require an open system-failure incident.
        for wizard, targets in (
            (impact_wizard, (draft_a, registration_a)),
            (restoration_wizard, (draft_a, registration_a)),
        ):
            for target in targets:
                wizard.write({"incident_id": target.id})
                with (
                    self.subTest(wizard=wizard._name, target=target.id),
                    self.cr.savepoint(),
                    self.assertRaises(UserError),
                ):
                    wizard.action_confirm()
            wizard.write({"incident_id": system_a.id})

        # Company B must be active even for a two-company manager using sudo.
        for elevated in (False, True):
            for action_name in ("action_review_impact", "action_review_restoration"):
                with (
                    self.subTest(foreign_launch=action_name, elevated=elevated),
                    self.cr.savepoint(),
                    self.assertRaises(AccessError),
                ):
                    getattr(
                        system_b.with_user(manager)
                        .with_context(allowed_company_ids=company_a.ids)
                        .sudo(elevated),
                        action_name,
                    )()
            for wizard in (impact_wizard, restoration_wizard):
                with (
                    self.subTest(foreign_retarget=wizard._name, elevated=elevated),
                    self.cr.savepoint(),
                    self.assertRaises((AccessError, UserError)),
                ):
                    wizard.with_user(manager).with_context(
                        allowed_company_ids=company_a.ids
                    ).write({"incident_id": system_b.id})
                    wizard.with_user(manager).with_context(
                        allowed_company_ids=company_a.ids
                    ).sudo(elevated).action_confirm()
                wizard.with_user(manager).with_context(
                    allowed_company_ids=company_a.ids
                ).write({"incident_id": system_a.id})

        # Forged defaults cannot authorize a foreign restoration parent.
        for elevated in (False, True):
            action = system_a.action_review_restoration()
            forged_context = {
                **action["context"],
                "default_incident_id": system_b.id,
                "allowed_company_ids": company_a.ids,
            }
            forged_default = (
                self.env[action["res_model"]]
                .with_user(manager)
                .sudo(elevated)
                # Escalating sudo removes default_* context in Odoo 20. Supply
                # the forged form default afterward to test the actual guard.
                .with_context(**forged_context)
            )
            self.assertEqual(
                forged_default.default_get(["incident_id"]),
                {"incident_id": system_b.id},
            )
            with (
                self.subTest(foreign_default=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                forged_default.create(
                    {
                        "evidence_origin": "simulated",
                        "restored_at": "2026-12-08 10:00:00",
                        "source_reference": "Synthetic forged-parent attempt",
                        "review_note": "Must fail at the public persistence boundary",
                    }
                ).action_confirm()

        # Computed overview fields are protected at both ORM write boundaries.
        for elevated in (False, True):
            with (
                self.subTest(computed_create=elevated),
                self.cr.savepoint(),
                self.assertRaises(UserError),
            ):
                self.env["l10n_ae.edi.incident"].with_user(manager).sudo(
                    elevated
                ).create(
                    {
                        "name": "Synthetic forged overview",
                        "company_id": company_a.id,
                        "affected_document_count": 99,
                    }
                )
            with (
                self.subTest(computed_write=elevated),
                self.cr.savepoint(),
                self.assertRaises(UserError),
            ):
                system_a.with_user(manager).sudo(elevated).write(
                    {"pending_confirmation_count": 0}
                )

        # Neutralized copies reject both launching a dialog and confirming a previously
        # opened dialog. Restore the database flag even if an assertion fails.
        parameters = self.env["ir.config_parameter"].sudo()
        neutralized_before = parameters.get_bool("database.is_neutralized")
        self.assertFalse(neutralized_before)
        parameters.set_bool("database.is_neutralized", True)
        try:
            for elevated in (False, True):
                for action_name in (
                    "action_review_impact",
                    "action_review_restoration",
                ):
                    with (
                        self.subTest(neutralized_launch=action_name, elevated=elevated),
                        self.cr.savepoint(),
                        self.assertRaises(UserError),
                    ):
                        getattr(system_a.sudo(elevated), action_name)()
                with (
                    self.subTest(neutralized_confirm="impact", elevated=elevated),
                    self.cr.savepoint(),
                    self.assertRaises(UserError),
                ):
                    impact_wizard.sudo(elevated).action_confirm()
                with (
                    self.subTest(neutralized_confirm="restoration", elevated=elevated),
                    self.cr.savepoint(),
                    self.assertRaises(UserError),
                ):
                    restoration_wizard.sudo(elevated).action_confirm()
        finally:
            parameters.set_bool("database.is_neutralized", neutralized_before)

        self.assertEqual(
            {
                model: self.env[model].search([], order="id").ids
                for model in history_before
            },
            history_before,
        )
        self.assertEqual(
            [
                row.read(["state", "write_date", "opened_at", "notification_due_date"])
                for row in incidents
            ],
            incident_before,
        )
        self.assertEqual([row.activity_ids.ids for row in incidents], activity_before)
        self.assertEqual(
            invoice.read(["state", "amount_total", "line_ids", "write_date"]),
            invoice_before,
        )
        self.assertEqual(
            issue.read(
                [
                    "submission_state",
                    "confirmation_state",
                    "idempotency_key",
                    "payload_hash",
                    "xml_hash",
                    "attempt_ids",
                    "remote_id",
                    "write_date",
                ]
            ),
            issue_before,
        )

        # Same-company actions succeed and replay each stable dialog key.
        self.assertEqual(
            impact_wizard.with_user(manager).action_confirm()["type"],
            "ir.actions.act_window_close",
        )
        impact_ids = system_a.impact_ids.ids
        self.assertEqual(
            impact_wizard.with_user(manager).action_confirm()["type"],
            "ir.actions.act_window_close",
        )
        self.assertEqual(system_a.impact_ids.ids, impact_ids)
        self.assertEqual(len(system_a.impact_ids), 1)

        self.assertEqual(
            restoration_wizard.with_user(manager).action_confirm()["type"],
            "ir.actions.act_window_close",
        )
        restoration_ids = system_a.restoration_ids.ids
        self.assertEqual(
            restoration_wizard.with_user(manager).action_confirm()["type"],
            "ir.actions.act_window_close",
        )
        self.assertEqual(system_a.restoration_ids.ids, restoration_ids)
        self.assertEqual(len(system_a.restoration_ids), 1)
        self.assertEqual(system_a.impact_ids.recorded_by_id, manager)
        self.assertEqual(system_a.restoration_ids.recorded_by_id, manager)
        self.assertEqual(
            {
                model: self.env[model].search([], order="id").ids
                for model in ("l10n_ae.edi.attempt", "l10n_ae.edi.event")
            },
            {
                model: history_before[model]
                for model in ("l10n_ae.edi.attempt", "l10n_ae.edi.event")
            },
        )
        self.assertEqual(
            invoice.read(["state", "amount_total", "line_ids", "write_date"]),
            invoice_before,
        )
        self.assertEqual(
            issue.read(
                [
                    "submission_state",
                    "confirmation_state",
                    "idempotency_key",
                    "payload_hash",
                    "xml_hash",
                    "attempt_ids",
                    "remote_id",
                    "write_date",
                ]
            ),
            issue_before,
        )

        overview = (
            system_a.with_user(reader)
            .with_context(allowed_company_ids=company_a.ids)
            .read(
                [
                    "affected_transmission_ids",
                    "affected_document_count",
                    "pending_submission_count",
                    "pending_confirmation_count",
                    "restoration_reviewed",
                ]
            )[0]
        )
        self.assertEqual(overview["affected_document_count"], 1)
        self.assertEqual(overview["affected_transmission_ids"], issue.ids)
        self.assertEqual(overview["pending_submission_count"], 1)
        self.assertEqual(overview["pending_confirmation_count"], 1)
        self.assertTrue(overview["restoration_reviewed"])

    @freeze_time("2026-12-08 12:00:00")
    def test_manager_reviews_impact_and_restoration_in_native_dialogs(self):
        manager = new_test_user(
            self.env,
            login="synthetic-recovery-dialog-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        self.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        self.partner_a.country_id = self.env.ref("base.ae")
        self.partner_a.state_id = self.env.ref("base.state_ae_du")
        if "routing_scheme" in self.env["res.partner"]._fields:
            self.env.company.partner_id.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000001"}
            )
            self.partner_a.write(
                {"routing_scheme": "0235", "routing_endpoint": "1000000002"}
            )
        binding = review_controlled_binding(
            self.env["l10n_ae.edi.binding"].create(
                {"name": "Synthetic native recovery binding"}
            ),
            self.simple_accountman,
            capabilities=CONTROLLED_CAPABILITIES,
            note="Synthetic reviewed controlled contract fixture.",
        )
        invoice = self.init_invoice(
            "out_invoice", amounts=[100], taxes=self.tax_sale_a, post=True
        ).with_user(manager)
        invoice.l10n_ae_binding_id = binding
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        incident = (
            self.env["l10n_ae.edi.incident"]
            .with_user(manager)
            .create(
                {
                    "name": "Synthetic native recovery review",
                    "affected_roles": "issuer",
                    "occurrence_at": "2026-12-01 09:00:00",
                    "detected_at": "2026-12-01 10:00:00",
                    "reason": "Controlled service failure",
                    "responsible_id": manager.id,
                    "calendar_start": "2026-12-01",
                    "calendar_end": "2026-12-10",
                    "calendar_holidays": [],
                    "calendar_source": "Reviewed synthetic calendar",
                    "calendar_version": "synthetic-2026",
                    "source_reference": "Reviewed MD243 Art12",
                    "review_note": "Native review; no fiscal success inferred",
                }
            )
        )
        select_reviewed_calendar_fixture(incident)
        incident.action_open()
        invoice_before = invoice.read(
            ["state", "amount_total", "line_ids", "write_date"]
        )
        basis_before = incident.read(
            ["state", "opened_at", "notification_due_date", "write_date"]
        )
        activity_before = incident.activity_ids.read(
            ["user_id", "date_deadline", "summary"]
        )
        action = incident.action_review_impact()
        selection = incident.env["l10n_ae.edi.transmission"].with_context(
            **action["context"]
        )
        selection_domain = [("company_id", "=", incident.company_id.id)]
        expected_selection = [(issue.id, f"{invoice.name} (Revision 1)")]
        self.assertEqual(
            selection.name_search(invoice.name, domain=selection_domain),
            expected_selection,
        )
        self.assertEqual(
            selection.name_search(issue.origin_document_id, domain=selection_domain),
            expected_selection,
        )
        self.assertEqual(
            selection.name_search(
                invoice.name, domain=[("id", "=", issue.id)], operator="not ilike"
            ),
            [],
        )
        self.assertEqual(issue.display_name, issue.origin_document_id)
        sender = new_test_user(
            self.env,
            login="synthetic-impact-selection-sender",
            groups="l10n_ae_edi.group_controlled_sender",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        self.assertEqual(issue.with_user(sender).display_name, issue.origin_document_id)
        for elevated in (False, True):
            with (
                self.subTest(selection_sender_sudo=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                selection.with_user(sender).sudo(elevated).name_search(
                    invoice.name, domain=selection_domain
                )
        with Form(
            incident.env[action["res_model"]].with_context(**action["context"]),
            view=action["view_id"],
        ) as form:
            form.transmission_ids.add(issue)
            form.source_reference = "Reviewed original queued invoice inventory"
            form.review_note = "Original is affected; preserve its transmission key"
        impact_dialog = form.record
        self.assertTrue(impact_dialog.evidence_key)
        self.assertEqual(impact_dialog.incident_id, incident)
        self.assertEqual(
            impact_dialog.action_confirm()["type"], "ir.actions.act_window_close"
        )
        batch = incident.impact_ids
        self.assertEqual(len(batch), 1)
        impact_dialog.action_confirm()
        self.assertEqual(incident.impact_ids, batch)
        self.assertEqual(batch.transmission_ids, issue)
        action = incident.action_review_restoration()
        with Form(
            incident.env[action["res_model"]].with_context(**action["context"]),
            view=action["view_id"],
        ) as form:
            form.evidence_origin = "simulated"
            form.restored_at = fields.Datetime.to_datetime("2026-12-08 10:00:00")
            form.source_reference = "Controlled service response available"
            form.review_note = "Local restoration review; invoice still pending"
        restoration_dialog = form.record
        self.assertEqual(
            restoration_dialog.action_confirm()["type"], "ir.actions.act_window_close"
        )
        review = incident.restoration_ids
        restoration_dialog.action_confirm()
        self.assertEqual(incident.restoration_ids, review)
        self.assertEqual(len(review), 1)
        overview = Form(incident, view="l10n_ae_edi.edi_incident_form")
        self.assertTrue(overview.restoration_reviewed)
        self.assertEqual(overview.affected_document_count, 1)
        self.assertEqual(overview.pending_submission_count, 1)
        self.assertEqual(overview.pending_confirmation_count, 1)
        self.assertEqual(incident.affected_transmission_ids, issue)
        with self.assertRaises(AssertionError):
            overview.pending_confirmation_count = 0
        self.assertEqual(issue.submission_state, "queued")
        self.assertEqual(issue.confirmation_state, "pending")
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.remote_id)
        self.assertEqual(
            invoice.read(["state", "amount_total", "line_ids", "write_date"]),
            invoice_before,
        )
        self.assertEqual(
            incident.read(
                ["state", "opened_at", "notification_due_date", "write_date"]
            ),
            basis_before,
        )
        self.assertEqual(
            incident.activity_ids.read(["user_id", "date_deadline", "summary"]),
            activity_before,
        )

    @freeze_time("2026-12-01 12:00:00")
    def test_manager_enters_reviewed_holidays_in_native_incident_form(self):
        manager = new_test_user(
            self.env,
            login="synthetic-incident-form-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        view = self.env.ref("l10n_ae_edi.edi_incident_form", raise_if_not_found=False)
        self.assertTrue(view, "The native incident review form must be installed")
        with Form(
            self.env["l10n_ae.edi.incident"].with_user(manager), view=view
        ) as form:
            form.name = "Synthetic incident entered through the native form"
            form.affected_roles = "both"
            form.occurrence_at = fields.Datetime.to_datetime("2026-11-30 21:30:00")
            form.detected_at = fields.Datetime.to_datetime("2026-12-01 08:00:00")
            form.reason = "Synthetic electronic issue and receipt failure"
            form.responsible_id = manager
            form.calendar_start = date(2026, 12, 1)
            form.calendar_end = date(2026, 12, 10)
            form.calendar_holiday_dates = "2026-12-02\n2026-12-03"
            form.calendar_source = "Reviewed synthetic federal holidays"
            form.calendar_version = "synthetic-federal-2026"
            form.source_reference = "MD243 Art12; reviewed Art49 day exclusion"
            form.review_note = "Dubai Dec1 anchor; Dec4 day1 and Dec7 day2"
        incident = form.record
        self.assertEqual(incident.company_id, self.env.company)
        self.assertEqual(incident.calendar_holidays, ["2026-12-02", "2026-12-03"])
        self.assertEqual(
            incident.occurrence_at, fields.Datetime.to_datetime("2026-11-30 21:30:00")
        )
        self.assertEqual(
            incident.detected_at, fields.Datetime.to_datetime("2026-12-01 08:00:00")
        )
        self.assertFalse(incident.notification_due_date)
        select_reviewed_calendar_fixture(incident)
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.notification_due_date, date(2026, 12, 7))
        self.assertEqual(len(incident.activity_ids), 1)
        self.assertEqual(incident.activity_ids.user_id, manager)
        self.assertEqual(incident.activity_ids.date_deadline, date(2026, 12, 7))
        reviewed = Form(incident, view=view)
        self.assertEqual(reviewed.calendar_holiday_dates, "2026-12-02\n2026-12-03")
        with self.assertRaises(AssertionError):
            reviewed.calendar_holiday_dates = "2026-12-04"
        self.assertEqual(incident.calendar_holidays, ["2026-12-02", "2026-12-03"])

    @freeze_time("2026-12-08 12:00:00")
    def test_registration_notice_due_from_receiving_fta_confirmation(self):
        manager = new_test_user(
            self.env,
            login="synthetic-registration-notice-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        incident = (
            self.env["l10n_ae.edi.incident"]
            .with_user(manager)
            .create(
                {
                    "name": "Synthetic FTA registration amendment confirmation",
                    "incident_kind": "registration_change",
                    "company_id": self.env.company.id,
                    "fta_confirmation_received_at": "2026-11-30 21:30:00",
                    "reason": "Synthetic confirmation of changed business address",
                    "responsible_id": manager.id,
                    "calendar_start": "2026-12-01",
                    "calendar_end": "2026-12-10",
                    "calendar_holidays": ["2026-12-02", "2026-12-03"],
                    "calendar_source": "Reviewed synthetic federal holidays",
                    "calendar_version": "synthetic-federal-2026",
                    "source_reference": "MD243 Art5.3; reviewed Art49 day exclusion",
                    "review_note": "Dubai Dec1 receipt; Dec4,7,8,9,10 are days1-5",
                }
            )
        )
        select_reviewed_calendar_fixture(incident)
        with self.cr.savepoint(), self.assertRaises(ValidationError):
            incident.action_open()
        self.assertEqual(incident.state, "draft")
        self.assertFalse(incident.notification_due_date)
        self.assertFalse(incident.activity_ids)
        incident.fta_confirmation_reference = "Synthetic FTA confirmation receipt A"
        with self.cr.savepoint(), self.assertRaises(ValidationError):
            incident.action_open()
        self.assertEqual(incident.state, "draft")
        self.assertFalse(incident.notification_due_date)
        self.assertFalse(incident.activity_ids)
        incident.record_fta_confirmation(
            {
                "evidence_key": "synthetic-registration-deadline-confirmation-A",
                "evidence_origin": "simulated",
                "received_at": "2026-11-30 21:30:00",
                "external_reference": "Synthetic FTA confirmation receipt A",
                "source_reference": "Supplied synthetic registration fixture",
                "review_note": "Reviewed declaration; no independent verification",
                "document_filename": "synthetic-registration-confirmation-A.txt",
                "document": base64.b64encode(
                    b"Synthetic registration deadline confirmation A\n"
                ).decode(),
            }
        )
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.notification_due_date, date(2026, 12, 10))
        self.assertFalse(incident.occurrence_at)
        self.assertFalse(incident.detected_at)
        self.assertEqual(
            incident.fta_confirmation_received_at,
            fields.Datetime.to_datetime("2026-11-30 21:30:00"),
        )
        activities = incident.activity_ids
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities.user_id, manager)
        self.assertEqual(activities.date_deadline, date(2026, 12, 10))
        self.assertEqual(activities.summary, "Review UAE registration amendment notice")
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.activity_ids.ids, activities.ids)
        with self.cr.savepoint(), self.assertRaises(UserError):
            incident.write({"fta_confirmation_received_at": "2026-12-08 08:00:00"})
        self.assertEqual(incident.notification_due_date, date(2026, 12, 10))

    @freeze_time("2026-12-08 12:00:00")
    def test_notice_artifacts_keep_asp_and_taxpayer_evidence_separate(self):
        manager = new_test_user(
            self.env,
            login="synthetic-incident-notice-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        reader = new_test_user(
            self.env,
            login="synthetic-incident-notice-reader",
            groups="account.group_account_readonly",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        incident = (
            self.env["l10n_ae.edi.incident"]
            .with_user(manager)
            .create(
                {
                    "name": "Synthetic incident notice evidence",
                    "company_id": self.env.company.id,
                    "affected_roles": "both",
                    "occurrence_at": "2026-11-30 21:30:00",
                    "detected_at": "2026-12-01 08:00:00",
                    "reason": "Synthetic electronic issue and receipt outage",
                    "responsible_id": manager.id,
                    "calendar_start": "2026-12-01",
                    "calendar_end": "2026-12-10",
                    "calendar_holidays": ["2026-12-02", "2026-12-03"],
                    "calendar_source": "Reviewed synthetic federal holidays",
                    "calendar_version": "synthetic-federal-2026",
                    "source_reference": "MD243 Art12; reviewed Art49 day exclusion",
                    "review_note": "Dubai Dec1 anchor; Dec4 day1, Dec7 day2",
                }
            )
        )
        select_reviewed_calendar_fixture(incident)
        incident.action_open()
        basis = incident.read(
            [
                "state",
                "notification_due_date",
                "opened_by_id",
                "opened_at",
                "write_date",
            ]
        )
        activity_ids = incident.activity_ids.ids
        asp_content = b"Controlled ASP service interruption notice\n"
        asp_digest = "62a1082c4b3b708df91fd7185e220e2cf7efa52ae97e549e9bc29d9ce63e6d8d"
        asp_evidence = {
            "evidence_key": "synthetic-asp-fta-1",
            "evidence_origin": "simulated",
            "sender_role": "asp",
            "sender_name": "Controlled ASP",
            "recipient_role": "fta",
            "recipient_name": "Controlled FTA recipient",
            "recipient_address": "fta-recipient@example.invalid",
            "channel": "email",
            "sent_at": "2026-12-04 08:00:00",
            "external_reference": "synthetic-asp-notice-1",
            "review_note": "Controlled artifact; not proof of taxpayer duty discharge",
            "document_filename": "synthetic-asp-notice.txt",
            "document": base64.b64encode(asp_content).decode(),
        }
        for elevated in (False, True):
            with (
                self.subTest(elevated=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                incident.with_user(reader).sudo(elevated).record_notice(asp_evidence)
        asp_result = incident.record_notice(asp_evidence)
        asp_notice = (
            self.env["l10n_ae.edi.incident.notice"]
            .with_user(manager)
            .browse(asp_result["id"])
        )
        self.assertEqual(asp_result["document_sha256"], asp_digest)
        self.assertEqual(asp_notice.document.content, asp_content)
        self.assertEqual(asp_notice.sender_role, "asp")
        self.assertEqual(asp_notice.notice_purpose, "asp_service_disruption")
        self.assertFalse(asp_notice.taxpayer_roles)
        self.assertEqual(asp_notice.evidence_origin, "simulated")
        self.assertEqual(asp_notice.recorded_by_id, manager)
        self.assertEqual(
            asp_notice.recorded_at, fields.Datetime.to_datetime("2026-12-08 12:00:00")
        )
        self.assertEqual(incident.record_notice(asp_evidence), asp_result)
        with self.cr.savepoint(), self.assertRaises(UserError):
            incident.record_notice({**asp_evidence, "external_reference": "Changed"})
        taxpayer_content = b"Controlled taxpayer issuer and recipient notice\n"
        taxpayer_digest = (
            "0f31a00f2ba51212cd9edf0a806bacae1b466cc13b2bf2fbb3bb71e2d3b07e49"
        )
        taxpayer_evidence = {
            **asp_evidence,
            "evidence_key": "synthetic-taxpayer-fta-1",
            "sender_role": "taxpayer",
            "sender_name": "Controlled UAE taxpayer",
            "taxpayer_roles": "both",
            "sent_at": "2026-12-07 10:00:00",
            "external_reference": "synthetic-taxpayer-notice-1",
            "review_note": "Controlled artifact reviewed for both taxpayer roles",
            "document_filename": "synthetic-taxpayer-notice.txt",
            "document": base64.b64encode(taxpayer_content).decode(),
        }
        taxpayer_result = incident.record_notice(taxpayer_evidence)
        taxpayer_notice = asp_notice.browse(taxpayer_result["id"])
        self.assertNotEqual(asp_notice, taxpayer_notice)
        self.assertEqual(taxpayer_notice.document.content, taxpayer_content)
        self.assertEqual(taxpayer_notice.document_sha256, taxpayer_digest)
        self.assertEqual(taxpayer_notice.sender_role, "taxpayer")
        self.assertEqual(taxpayer_notice.notice_purpose, "taxpayer_system_failure")
        self.assertEqual(taxpayer_notice.taxpayer_roles, "both")
        self.assertEqual(incident.record_notice(taxpayer_evidence), taxpayer_result)
        self.assertEqual(
            set(incident.notice_ids.ids), {asp_notice.id, taxpayer_notice.id}
        )
        for notice in (asp_notice, taxpayer_notice):
            self.assertEqual(notice.company_id, incident.company_id)
            self.assertEqual(
                notice.with_user(reader).document.content, notice.document.content
            )
            with self.cr.savepoint(), self.assertRaises(UserError):
                notice.write({"document_filename": "changed.txt"})
            with self.cr.savepoint(), self.assertRaises(UserError):
                notice.unlink()
        with self.cr.savepoint(), self.assertRaises(UserError):
            self.env["l10n_ae.edi.incident.notice"].with_user(manager).create(
                {"incident_id": incident.id, "document": taxpayer_evidence["document"]}
            )
        self.assertEqual(
            incident.read(
                [
                    "state",
                    "notification_due_date",
                    "opened_by_id",
                    "opened_at",
                    "write_date",
                ]
            ),
            basis,
        )
        self.assertEqual(incident.activity_ids.ids, activity_ids)

    @freeze_time("2026-12-08 12:00:00")
    def test_registration_notice_is_recorded_through_native_forms(self):
        manager = new_test_user(
            self.env,
            login="synthetic-registration-form-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        view = "l10n_ae_edi.edi_incident_form"
        with Form(
            self.env["l10n_ae.edi.incident"].with_user(manager), view=view
        ) as form:
            form.name = "Synthetic registration notice from the native form"
            form.incident_kind = "registration_change"
            with self.assertRaises(AssertionError):
                form.occurrence_at = fields.Datetime.to_datetime("2026-11-30 21:30:00")
            form.fta_confirmation_received_at = fields.Datetime.to_datetime(
                "2026-11-30 21:30:00"
            )
            form.fta_confirmation_reference = "Synthetic FTA registration receipt B"
            form.reason = "Synthetic confirmed business address amendment"
            form.responsible_id = manager
            form.calendar_start = date(2026, 12, 1)
            form.calendar_end = date(2026, 12, 10)
            form.calendar_holiday_dates = "2026-12-02\n2026-12-03"
            form.calendar_source = "Reviewed synthetic federal holidays"
            form.calendar_version = "synthetic-federal-2026"
            form.source_reference = "MD243 Art5.3; reviewed Art49 day exclusion"
            form.review_note = "Dubai Dec1 receipt; Dec4,7,8,9,10 are days1-5"
        incident = form.record
        confirmation_action = incident.action_record_fta_confirmation()
        confirmation_model = (
            self.env[confirmation_action["res_model"]]
            .with_user(manager)
            .with_context(**confirmation_action["context"])
        )
        original = b"Synthetic FTA registration confirmation\n"
        with Form(confirmation_model, view=confirmation_action["view_id"]) as capture:
            capture.evidence_origin = "simulated"
            capture.received_at = fields.Datetime.to_datetime("2026-11-30 21:30:00")
            capture.external_reference = "Synthetic FTA registration receipt B"
            capture.source_reference = "Supplied synthetic FTA original"
            capture.review_note = "Reviewed receipt; no verified authorship or delivery"
            capture.document_filename = "synthetic-fta-confirmation.txt"
            capture.document = BinaryBytes(original)
        confirmation_wizard = capture.record
        self.assertEqual(
            confirmation_wizard.action_confirm(),
            {"type": "ir.actions.act_window_close"},
        )
        confirmation = incident.fta_confirmation_ids
        self.assertEqual(len(confirmation), 1)
        self.assertEqual(confirmation.document.content, original)
        self.assertEqual(
            confirmation.document_sha256,
            "203b34111aa50c86b0fafe85c264901e9d46c29363082ed659e584822730b639",
        )
        self.assertEqual(confirmation.recorded_by_id, manager)
        self.assertEqual(
            confirmation_wizard.action_confirm(),
            {"type": "ir.actions.act_window_close"},
        )
        self.assertEqual(incident.fta_confirmation_ids, confirmation)
        self.assertFalse(incident.notice_ids)
        captured = Form(incident, view=view)
        self.assertEqual(len(captured.fta_confirmation_ids), 1)
        with self.assertRaises(AssertionError):
            captured.fta_confirmation_received_at = fields.Datetime.to_datetime(
                "2026-12-08 10:00:00"
            )
        select_reviewed_calendar_fixture(incident)
        incident.action_open()
        self.assertEqual(incident.notification_due_date, date(2026, 12, 10))
        self.assertFalse(incident.occurrence_at)
        self.assertFalse(incident.detected_at)
        activity_ids = incident.activity_ids.ids
        action = incident.action_record_notice()
        content = b"Controlled taxpayer issuer and recipient notice\n"
        dialog_model = (
            self.env[action["res_model"]]
            .with_user(manager)
            .with_context(**action["context"])
        )
        with Form(dialog_model, view=action["view_id"]) as dialog:
            dialog.evidence_origin = "simulated"
            dialog.sender_role = "taxpayer"
            dialog.sender_name = "Controlled UAE taxpayer"
            dialog.taxpayer_roles = "both"
            dialog.recipient_role = "fta"
            dialog.recipient_name = "Controlled appointed ASP"
            dialog.recipient_address = "asp-recipient@example.invalid"
            dialog.channel = "email"
            dialog.sent_at = fields.Datetime.to_datetime("2026-12-04 08:00:00")
            dialog.external_reference = "synthetic-written-registration-notice-1"
            dialog.review_note = "Controlled written notice; no actual transmission"
            dialog.document_filename = "synthetic-registration-notice.txt"
            dialog.document = BinaryBytes(content)
        wizard = dialog.record
        with self.cr.savepoint(), self.assertRaises(ValidationError):
            wizard.action_confirm()
        self.assertFalse(incident.notice_ids)
        wizard.write({"recipient_role": "asp", "sent_at": "2026-11-30 20:30:00"})
        with self.cr.savepoint(), self.assertRaises(ValidationError):
            wizard.action_confirm()
        self.assertFalse(incident.notice_ids)
        wizard.sent_at = "2026-12-04 08:00:00"
        self.assertEqual(
            wizard.action_confirm(), {"type": "ir.actions.act_window_close"}
        )
        notice = incident.notice_ids
        self.assertEqual(len(notice), 1)
        self.assertEqual(notice.notice_purpose, "taxpayer_registration")
        self.assertEqual(notice.sender_role, "taxpayer")
        self.assertEqual(notice.recipient_role, "asp")
        self.assertEqual(notice.taxpayer_roles, "both")
        self.assertEqual(notice.document.content, content)
        self.assertEqual(
            notice.document_sha256,
            "0f31a00f2ba51212cd9edf0a806bacae1b466cc13b2bf2fbb3bb71e2d3b07e49",
        )
        self.assertEqual(
            wizard.action_confirm(), {"type": "ir.actions.act_window_close"}
        )
        self.assertEqual(incident.notice_ids.ids, notice.ids)
        reviewed = Form(incident, view=view)
        self.assertEqual(len(reviewed.notice_ids), 1)
        with self.assertRaises(AssertionError):
            reviewed.fta_confirmation_received_at = fields.Datetime.to_datetime(
                "2026-12-08 10:00:00"
            )
        self.assertEqual(incident.notification_due_date, date(2026, 12, 10))
        self.assertEqual(incident.state, "open")
        self.assertEqual(incident.activity_ids.ids, activity_ids)

    @freeze_time("2026-12-01 12:00:00")
    def test_manager_opens_failure_with_reviewed_calendar_and_one_internal_alert(self):
        self.assertIn("l10n_ae.edi.incident", self.env.registry)
        manager = new_test_user(
            self.env,
            login="synthetic-incident-manager",
            groups="account.group_account_manager",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        reader = new_test_user(
            self.env,
            login="synthetic-incident-reader",
            groups="account.group_account_readonly",
            company_ids=self.env.company.ids,
            company_id=self.env.company.id,
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.action_post()
        ledger = invoice.line_ids.sorted("id").read(
            ["debit", "credit", "balance", "amount_currency", "write_date"]
        )
        move_before = invoice.read(["state", "amount_total", "write_date"])
        models = (
            "l10n_ae.edi.transmission",
            "l10n_ae.edi.attempt",
            "l10n_ae.edi.event",
        )
        baseline = {
            model: self.env[model].search([], order="id").ids for model in models
        }
        incident = (
            self.env["l10n_ae.edi.incident"]
            .with_user(manager)
            .with_context(
                default_state="open", default_notification_due_date="2000-01-01"
            )
            .create(
                {
                    "name": "Synthetic system outage over reviewed federal holidays",
                    "company_id": self.env.company.id,
                    "affected_roles": "both",
                    "occurrence_at": "2026-11-30 21:30:00",
                    "detected_at": "2026-12-01 08:00:00",
                    "reason": "Synthetic outage prevents electronic issue and receipt",
                    "responsible_id": manager.id,
                    "calendar_start": "2026-12-01",
                    "calendar_end": "2026-12-10",
                    "calendar_holidays": ["2026-12-02", "2026-12-03"],
                    "calendar_source": "Reviewed synthetic federal holidays",
                    "calendar_version": "synthetic-federal-2026",
                    "source_reference": "MD243 Art12; reviewed Art49 day exclusion",
                    "review_note": "Dubai anchor Dec1; Dec4 day1, Dec7 day2",
                }
            )
        )
        self.assertEqual(incident.state, "draft")
        self.assertFalse(incident.notification_due_date)
        for elevated in (False, True):
            with (
                self.subTest(elevated=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                incident.with_user(reader).sudo(elevated).action_open()
        select_reviewed_calendar_fixture(incident)
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.state, "open")
        self.assertEqual(incident.notification_due_date, date(2026, 12, 7))
        self.assertEqual(incident.timezone, "Asia/Dubai")
        self.assertEqual(incident.affected_roles, "both")
        self.assertEqual(incident.opened_by_id, manager)
        self.assertTrue(incident.opened_at)
        activities = incident.activity_ids.sorted("id")
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities.user_id, manager)
        self.assertEqual(activities.date_deadline, date(2026, 12, 7))
        self.assertEqual(activities.summary, "Review UAE system failure notification")
        self.assertTrue(incident.action_open())
        self.assertEqual(incident.activity_ids.ids, activities.ids)
        for values in (
            {"occurrence_at": "2026-12-01 08:00:00"},
            {"calendar_holidays": []},
            {"notification_due_date": "2026-12-04"},
            {"opened_by_id": reader.id},
        ):
            with (
                self.subTest(values=values),
                self.cr.savepoint(),
                self.assertRaises(UserError),
            ):
                incident.write(values)
        self.assertEqual(
            invoice.read(["state", "amount_total", "write_date"]), move_before
        )
        self.assertEqual(
            invoice.line_ids.sorted("id").read(
                ["debit", "credit", "balance", "amount_currency", "write_date"]
            ),
            ledger,
        )
        self.assertEqual(
            {model: self.env[model].search([], order="id").ids for model in models},
            baseline,
        )

    @freeze_time("2026-12-08 12:00:00")
    def test_notice_dialog_rechecks_actor_and_company_after_public_orm_edits(self):
        company_a = self.env.company
        company_b = self.setup_other_company(name="Synthetic incident company B")[
            "company"
        ]
        company_b.country_id = self.env.ref("base.ae")
        manager = new_test_user(
            self.env,
            login="synthetic-dialog-two-company-manager",
            groups="account.group_account_manager",
            company_ids=[company_a.id, company_b.id],
            company_id=company_a.id,
        )
        reader = new_test_user(
            self.env,
            login="synthetic-dialog-company-a-reader",
            groups="account.group_account_readonly",
            company_ids=company_a.ids,
            company_id=company_a.id,
        )
        cases = []
        for company in (company_a, company_b):
            incident = (
                self.env["l10n_ae.edi.incident"]
                .with_user(manager)
                .with_context(allowed_company_ids=company.ids)
                .create(
                    {
                        "name": "Synthetic registration case " + company.name,
                        "company_id": company.id,
                        "incident_kind": "registration_change",
                        "fta_confirmation_received_at": "2026-11-30 21:30:00",
                        "fta_confirmation_reference": "Synthetic FTA receipt",
                        "reason": "Synthetic confirmed registration amendment",
                        "responsible_id": manager.id,
                        "calendar_start": "2026-12-01",
                        "calendar_end": "2026-12-10",
                        "calendar_holidays": ["2026-12-02", "2026-12-03"],
                        "calendar_source": "Reviewed synthetic federal holidays",
                        "calendar_version": "synthetic-federal-2026",
                        "source_reference": "MD243 Art5.3; reviewed Art49",
                        "review_note": "Dubai Dec1 receipt; Dec10 fifth business day",
                    }
                )
            )
            with self.cr.savepoint(), self.assertRaises(UserError):
                incident.action_record_notice()
            incident.record_fta_confirmation(
                {
                    "evidence_key": (
                        "synthetic-dialog-registration-confirmation-" + str(company.id)
                    ),
                    "evidence_origin": "simulated",
                    "received_at": "2026-11-30 21:30:00",
                    "external_reference": "Synthetic FTA receipt",
                    "source_reference": "Supplied synthetic company fixture",
                    "review_note": "Reviewed declaration; no independent verification",
                    "document_filename": "synthetic-dialog-confirmation.txt",
                    "document": base64.b64encode(
                        b"Synthetic dialog registration confirmation\n"
                    ).decode(),
                }
            )
            select_reviewed_calendar_fixture(incident)
            incident.action_open()
            cases.append(incident)
        case_a, case_b = cases
        facts = [
            case.read(["state", "notification_due_date", "write_date", "opened_at"])
            for case in cases
        ]
        activities = [case.activity_ids.ids for case in cases]
        values = {
            "evidence_origin": "simulated",
            "sender_role": "taxpayer",
            "sender_name": "Controlled UAE taxpayer",
            "taxpayer_roles": "both",
            "recipient_role": "asp",
            "recipient_name": "Controlled appointed ASP",
            "recipient_address": "asp-recipient@example.invalid",
            "channel": "email",
            "sent_at": "2026-12-04 08:00:00",
            "external_reference": "synthetic-dialog-company-review",
            "review_note": "Controlled artifact; no notice sent",
            "document_filename": "synthetic-notice.txt",
            "document": BinaryBytes(
                b"Controlled taxpayer issuer and recipient notice\n"
            ),
        }
        action = case_a.action_record_notice()
        wizard = (
            self.env[action["res_model"]]
            .with_user(manager)
            .with_context(allowed_company_ids=company_a.ids, **action["context"])
            .create(values)
        )
        for elevated in (False, True):
            for target in (case_a, case_b):
                with (
                    self.subTest(launch=target.id, elevated=elevated),
                    self.cr.savepoint(),
                    self.assertRaises(AccessError),
                ):
                    target.with_user(reader).sudo(elevated).with_context(
                        allowed_company_ids=company_a.ids
                    ).action_record_notice()
            with (
                self.subTest(confirm_reader=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                wizard.with_user(reader).sudo(elevated).action_confirm()
        draft = (
            self.env["l10n_ae.edi.incident"]
            .with_user(manager)
            .with_context(allowed_company_ids=company_a.ids)
            .create({"name": "Synthetic unreviewed case", "company_id": company_a.id})
        )
        wizard.write({"incident_id": draft.id})
        with self.cr.savepoint(), self.assertRaises(UserError):
            wizard.action_confirm()
        self.assertFalse(draft.notice_ids)
        # Readonly widgets do not prevent public ORM writes; the parent must recheck.
        wizard.write({"incident_id": case_b.id})
        for elevated in (False, True):
            with (
                self.subTest(inactive_company=elevated),
                self.cr.savepoint(),
                self.assertRaises(AccessError),
            ):
                wizard.sudo(elevated).action_confirm()
        self.assertFalse(case_a.notice_ids)
        self.assertFalse(case_b.notice_ids)
        wizard.write({"incident_id": case_a.id, "recipient_role": "fta"})
        with self.cr.savepoint(), self.assertRaises(ValidationError):
            wizard.action_confirm()
        wizard.write({"recipient_role": "asp", "taxpayer_roles": False})
        with self.cr.savepoint(), self.assertRaises(ValidationError):
            wizard.action_confirm()
        self.assertFalse(case_a.notice_ids)
        wizard.taxpayer_roles = "both"
        wizard.action_confirm()
        own_notice = case_a.notice_ids
        self.assertEqual(len(own_notice), 1)
        self.assertEqual(own_notice.company_id, company_a)
        self.assertEqual(
            own_notice.with_user(reader).document_sha256,
            "0f31a00f2ba51212cd9edf0a806bacae1b466cc13b2bf2fbb3bb71e2d3b07e49",
        )
        # Forged defaults are still checked after normal native wizard creation.
        foreign_wizard = (
            wizard.env[wizard._name]
            .with_context(
                allowed_company_ids=company_a.ids,
                default_incident_id=case_b.id,
                default_recipient_role="fta",
                default_taxpayer_roles="issuer",
            )
            .create({k: v for k, v in values.items() if k != "recipient_role"})
        )
        with self.cr.savepoint(), self.assertRaises(AccessError):
            foreign_wizard.action_confirm()
        foreign_wizard.write({"recipient_role": "asp"})
        foreign_wizard.with_context(allowed_company_ids=company_b.ids).action_confirm()
        foreign_notice = case_b.notice_ids
        self.assertEqual(len(foreign_notice), 1)
        self.assertNotEqual(own_notice, foreign_notice)
        self.assertEqual(foreign_notice.company_id, company_b)
        with self.cr.savepoint(), self.assertRaises(AccessError):
            foreign_notice.with_user(reader).with_context(
                allowed_company_ids=company_a.ids
            ).read(["document", "external_reference"])
        self.assertEqual(
            [
                case.read(["state", "notification_due_date", "write_date", "opened_at"])
                for case in cases
            ],
            facts,
        )
        self.assertEqual([case.activity_ids.ids for case in cases], activities)
