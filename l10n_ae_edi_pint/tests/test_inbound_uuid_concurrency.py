# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Real transaction regression; opt in only on a dedicated disposable database.

Ordinary TransactionCase fixtures cannot be committed to concurrent transactions.
This native integration case deliberately preserves its committed synthetic fixture
in the owning QA database. It never commits a TransactionCase cursor or patches
registry.cursor into a shared TestCursor. Use the exact database opt-in documented
in docs/native-inbound-regressions.md; the normal native suite skips this case.
"""

import hashlib
import json
import logging
import os
import threading
import time

from odoo import Command, api
from odoo.orm.registry import Registry
from odoo.tests import tagged
from odoo.tests.common import BaseCase, get_db_name

from .common import requires_normative_resources

_logger = logging.getLogger(__name__)


def _company_setup(environment, suffix, tin):
    company = environment["res.company"].create(
        {
            "name": "Synthetic concurrent UUID " + suffix,
            "country_id": environment.ref("base.ae").id,
            "currency_id": environment.ref("base.AED").id,
        }
    )
    environment["account.chart.template"].try_loading("ae", company, install_demo=False)
    company.write({"l10n_ae_tin": tin, "l10n_ae_tin_confirmed": True})
    company.partner_id.write(
        {
            "vat": tin + "00003",
            "street": "Synthetic concurrent UUID street",
            "city": "Dubai",
            "state_id": environment.ref("base.state_ae_du").id,
            "routing_scheme": "0235",
            "routing_endpoint": tin,
            "l10n_ae_registration_type": "TL",
            "l10n_ae_registration_authority": "Synthetic concurrent UUID authority",
            "additional_identifiers": {"AE_TL": "SYNTH-UUID-" + suffix},
        }
    )
    manager = environment["res.users"].create(
        {
            "name": "Synthetic concurrent UUID " + suffix + " manager",
            "login": environment.cr.dbname + "-" + suffix,
            "company_id": company.id,
            "company_ids": [Command.set(company.ids)],
            "group_ids": [
                Command.set([environment.ref("account.group_account_manager").id])
            ],
        }
    )
    assert not manager.has_group("base.group_system")
    return company, manager


def _binding_setup(environment, company, manager, name, codes):
    actor = environment["l10n_ae.edi.binding"].with_user(manager).with_company(company)
    assert not actor.env.su
    capabilities = actor.env["l10n_ae.edi.capability"].search(
        [("code", "in", list(codes))]
    )
    assert set(capabilities.mapped("code")) == set(codes)
    binding = actor.create(
        {
            "name": name,
            "company_id": company.id,
            "provider": "controlled",
            "environment": "sandbox",
            "fiscal_uuid_policy": "controlled_local",
            "capability_profile": "kmee-controlled-1",
            "capability_ids": [Command.set(capabilities.ids)],
            "capability_review_note": "Synthetic concurrent public Process regression.",
        }
    )
    binding.action_review_capabilities()
    assert binding.capability_review_state == "ready"
    assert not binding.controlled_dispatch_enabled
    return binding


def _seed(environment):
    issuer, issuer_manager = _company_setup(environment, "issuer", "1000000001")
    recipient, recipient_manager = _company_setup(
        environment, "recipient", "1000000002"
    )
    source_binding = _binding_setup(
        environment,
        issuer,
        issuer_manager,
        "Synthetic concurrent issuer",
        ("submit", "lookup", "report", "billing_invoice"),
    )
    incoming_bindings = [
        _binding_setup(
            environment,
            recipient,
            recipient_manager,
            "Synthetic independent receive binding " + str(i),
            ("receive",),
        )
        for i in range(2)
    ]
    journal = environment["account.journal"].search(
        [("company_id", "=", issuer.id), ("type", "=", "sale")], limit=1
    )
    tax = environment.ref(f"account.{issuer.id}_uae_sale_tax_5_dubai")
    tax.ubl_cii_tax_category_code = "S"
    purchase_tax = environment.ref(f"account.{recipient.id}_uae_purchase_tax_5")
    purchase_tax.ubl_cii_tax_category_code = "S"
    recipient.account_purchase_tax_id = purchase_tax
    product = environment["product.product"].create(
        {
            "name": "Synthetic concurrent service",
            "type": "service",
            "l10n_ae_service_accounting_code": "SYNTHETIC-CONCURRENT-SAC",
            "l10n_ae_service_accounting_version": "SYNTHETIC-SAC-2026",
        }
    )
    source = (
        environment["account.move"]
        .with_user(issuer_manager)
        .with_company(issuer)
        .create(
            {
                "move_type": "out_invoice",
                "company_id": issuer.id,
                "journal_id": journal.id,
                "partner_id": recipient.partner_id.id,
                "invoice_date": "2026-10-02",
                "l10n_ae_binding_id": source_binding.id,
                "preferred_payment_method_line_id": False,
                "partner_bank_id": False,
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": "Synthetic concurrent service",
                            "product_id": product.id,
                            "quantity": 1,
                            "price_unit": 100,
                            "account_id": journal.default_account_id.id,
                            "tax_ids": [Command.set(tax.ids)],
                        }
                    )
                ],
            }
        )
    )
    source.action_post()
    assert (source.amount_untaxed, source.amount_tax, source.amount_total) == (
        100,
        5,
        105,
    )
    source.action_l10n_ae_prepare()
    report = source.validate_l10n_ae_pint()
    assert report["valid"], report
    content = source.preview_l10n_ae_pint().encode()
    receiver = (
        environment["l10n_ae.edi.inbox"]
        .with_user(recipient_manager)
        .with_company(recipient)
    )
    inboxes = [
        receiver.receive_controlled_document(
            binding.id, "concurrent-" + str(i), content
        )
        for i, binding in enumerate(incoming_bindings)
    ]
    data = {
        "company_id": recipient.id,
        "manager_id": recipient_manager.id,
        "inbox_ids": [inbox.id for inbox in inboxes],
        "fiscal_uuid": source.l10n_ae_fiscal_uuid,
        "xml_hash": hashlib.sha256(content).hexdigest(),
    }
    environment.cr.commit()
    return {
        **data,
        "source_id": source.id,
        "source_issue_id": source.l10n_ae_transmission_id.id,
        "content": content,
    }


@tagged("post_install", "-at_install", "-standard", "uae_inbound_concurrency")
class TestUaeInboundUuidConcurrency(BaseCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.registry = Registry(get_db_name())

    def setUp(self):
        super().setUp()
        database = self.registry.db_name
        if os.environ.get("KMEE_UAE_NATIVE_CONCURRENCY_DATABASE") != database:
            self.skipTest(
                "Real concurrent import requires the exact disposable database opt-in."
            )
        self.assertRegex(database, r"^uae_test_[a-z0-9_]+$")
        self.assertFalse(
            self._registry_patched, "Concurrent cursors must not share TestCursor."
        )
        with self.registry.cursor() as cr:
            admin = api.Environment(cr, api.SUPERUSER_ID, {})
            self.assertEqual(
                admin["account.move"].search_count([]),
                0,
                "The dedicated native concurrency database must have "
                "no business invoices.",
            )

    @requires_normative_resources("1.0.4")
    def test_independent_bindings_concurrently_import_one_fiscal_uuid(self):
        with self.registry.cursor() as cr:
            data = _seed(api.Environment(cr, api.SUPERUSER_ID, {}))
        self.assertEqual(len(data["inbox_ids"]), 2)
        barrier = threading.Barrier(2)
        outcomes = []
        names = ["uae_native_uuid_worker_" + str(i) for i in range(2)]

        def worker(index):
            try:
                with self.registry.cursor() as cr:
                    cr.execute(
                        "SELECT set_config('application_name', %s, true)",
                        [names[index]],
                    )
                    cr.execute(
                        "SELECT pg_backend_pid(), "
                        "current_setting('transaction_isolation')"
                    )
                    pid, isolation = cr.fetchone()
                    actor = api.Environment(
                        cr,
                        data["manager_id"],
                        {"allowed_company_ids": [data["company_id"]]},
                    )
                    self.assertFalse(actor.su)
                    self.assertFalse(actor.user.has_group("base.group_system"))
                    self.assertEqual(actor.user.company_ids.ids, [data["company_id"]])
                    self.assertEqual(isolation, "repeatable read")
                    inbox = actor["l10n_ae.edi.inbox"].browse(data["inbox_ids"][index])
                    barrier.wait(timeout=20)
                    processed = inbox.action_process()
                    # Only this worker owns this real cursor; no shared test cursor.
                    cr.commit()  # pylint: disable=invalid-commit
                    outcomes.append(
                        {
                            "worker": index,
                            "pid": pid,
                            "isolation": isolation,
                            "processed": processed,
                        }
                    )
            except Exception as error:  # noqa: BLE001 - surface every worker failure
                outcomes.append(
                    {
                        "worker": index,
                        "error_type": type(error).__name__,
                        "error": str(error),
                    }
                )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        both_waiting = False
        # SQL coordinates the real contention, never supplies the business oracle.
        with self.registry.cursor() as coordinator:
            coordinator.execute("SELECT pg_backend_pid()")
            coordinator_pid = coordinator.fetchone()[0]
            coordinator.execute(
                "SELECT id FROM res_company WHERE id = %s FOR UPDATE",
                [data["company_id"]],
            )
            for thread in threads:
                thread.start()
            try:
                deadline = time.monotonic() + 30
                with self.registry.cursor() as observer:
                    while time.monotonic() < deadline:
                        observer.execute("SELECT pg_stat_clear_snapshot()")
                        observer.execute(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() "
                            "AND application_name = ANY(%s) "
                            "AND wait_event_type = 'Lock' "
                            "AND query LIKE 'SELECT id FROM res_company%%'",
                            [names],
                        )
                        if observer.fetchone()[0] == 2:
                            both_waiting = True
                            break
                        time.sleep(0.05)
            finally:
                coordinator.rollback()
        for thread in threads:
            thread.join(timeout=45)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertTrue(
            both_waiting, "Both actual importers must overlap at the company lock."
        )
        self.assertEqual(len(outcomes), 2, outcomes)
        self.assertFalse(any("error_type" in row for row in outcomes), outcomes)
        self.assertEqual(len({row["pid"] for row in outcomes} | {coordinator_pid}), 3)
        self.assertEqual(sorted(row["processed"] for row in outcomes), [False, True])

        with self.registry.cursor() as cr:
            actor = api.Environment(
                cr, data["manager_id"], {"allowed_company_ids": [data["company_id"]]}
            )
            domain = [
                ("company_id", "=", data["company_id"]),
                ("l10n_ae_received_document", "=", True),
                ("l10n_ae_fiscal_uuid", "=", data["fiscal_uuid"]),
            ]
            moves = actor["account.move"].search(domain)
            inboxes = actor["l10n_ae.edi.inbox"].browse(data["inbox_ids"])
            self.assertEqual(
                len(moves), 1, "Concurrent import duplicated the received fiscal UUID."
            )
            self.assertEqual((moves.state, moves.move_type), ("draft", "in_invoice"))
            self.assertEqual(
                (moves.amount_untaxed, moves.amount_tax, moves.amount_total),
                (100, 5, 105),
            )
            self.assertEqual(sorted(moves.line_ids.mapped("balance")), [-105, 5, 100])
            self.assertFalse(moves.l10n_ae_transmission_ids)
            self.assertEqual(sorted(inboxes.mapped("state")), ["processed", "review"])
            self.assertEqual(len(inboxes.binding_id), 2)
            self.assertTrue(
                all(i.original_xml.content == data["content"] for i in inboxes)
            )
            reports = inboxes.processing_history_ids
            self.assertEqual(len(reports), 2)
            for report in reports:
                self.assertTrue(json.loads(report.validation_report)["valid"])
                self.assertEqual(report.xml_hash, data["xml_hash"])
                self.assertEqual(
                    report.validation_report_hash,
                    hashlib.sha256(report.validation_report.encode()).hexdigest(),
                )
                self.assertTrue(
                    report.normative_ruleset and report.normative_manifest_hash
                )
            review = inboxes.filtered(lambda i: i.state == "review")
            self.assertFalse(review.move_id)
            self.assertIn("UUID", review.diagnostic)
            before_history = reports.read(
                [
                    "outcome",
                    "xml_hash",
                    "validation_report_hash",
                    "normative_ruleset",
                    "normative_manifest_hash",
                ]
            )
            # This observer is a new real transaction, beyond the losing snapshot.
            self.assertTrue(review.action_process(), review.diagnostic)
            self.assertEqual(review.move_id, moves)
            self.assertEqual(actor["account.move"].search_count(domain), 1)
            self.assertEqual(inboxes.mapped("move_id"), moves)
            self.assertEqual(inboxes.mapped("state"), ["processed", "processed"])
            self.assertEqual(
                reports.read(
                    [
                        "outcome",
                        "xml_hash",
                        "validation_report_hash",
                        "normative_ruleset",
                        "normative_manifest_hash",
                    ]
                ),
                before_history,
            )
            self.assertTrue(
                all(i.original_xml.content == data["content"] for i in inboxes)
            )
            # The fresh retry transaction is owned exclusively by this case.
            cr.commit()  # pylint: disable=invalid-commit
            facts = {
                "workers": outcomes,
                "coordinator_pid": coordinator_pid,
                "both_waiting": both_waiting,
                "received_moves": 1,
                "move_type": moves.move_type,
                "state": moves.state,
                "totals": [moves.amount_untaxed, moves.amount_tax, moves.amount_total],
                "ledger": sorted(moves.line_ids.mapped("balance")),
                "inbox_ids": data["inbox_ids"],
                "binding_ids": inboxes.binding_id.ids,
                "xml_hash": data["xml_hash"],
                "retained_report_hashes": reports.mapped("validation_report_hash"),
                "fresh_retry_linked_same_move": True,
                "received_move_outbound_issues": 0,
                "fixture_retained_in_owned_disposable_database": True,
            }
            _logger.info(
                "UAE_NATIVE_UUID_CONCURRENCY=%s", json.dumps(facts, sort_keys=True)
            )
