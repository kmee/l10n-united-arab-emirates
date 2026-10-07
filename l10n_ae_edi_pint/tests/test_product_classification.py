# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json

from lxml import etree

from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}


@tagged("post_install", "-at_install")
class TestUaeProductClassification(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country("ae")
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.write(
            {"l10n_ae_tin": "1000000001", "l10n_ae_tin_confirmed": True}
        )
        for partner, vat, endpoint, label in [
            (cls.env.company.partner_id, "100000000100003", "1000000001", "Supplier"),
            (cls.partner_a, "100000000200003", "1000000002", "Buyer"),
        ]:
            partner.write(
                {
                    "country_id": cls.env.ref("base.ae").id,
                    "vat": vat,
                    "street": "Synthetic classification street",
                    "city": "Dubai",
                    "state_id": cls.env.ref("base.state_ae_du").id,
                    "routing_scheme": "0235",
                    "routing_endpoint": endpoint,
                    "l10n_ae_registration_type": "TL",
                    "l10n_ae_registration_authority": "Synthetic Product Authority",
                    "additional_identifiers": {"AE_TL": "SYNTH-PRODUCT-" + label},
                }
            )

    def test_service_product_preserves_reviewed_sac_in_frozen_xml_and_snapshot(self):
        # BTAE-13/17, IBR-185/189: S requires a separate SAC identifier.
        # These arbitrary codes/version are synthetic reviewed test data only.
        product = self.env["product.product"].create(
            {
                "name": "Synthetic classified service",
                "type": "service",
                "list_price": 100,
                "l10n_ae_service_accounting_code": "SYNTHETIC-SAC-01",
                "l10n_ae_service_accounting_version": "SYNTHETIC-SAC-2026",
            }
        )
        binding = self.env["l10n_ae.edi.binding"].create(
            {"name": "Synthetic service classification sandbox"}
        )
        invoice = self.init_invoice("out_invoice", amounts=[100], taxes=self.tax_sale_a)
        invoice.invoice_line_ids.product_id = product
        invoice.l10n_ae_binding_id = binding
        invoice = invoice.with_user(self.simple_accountman)
        self.assertFalse(invoice.env.su)
        invoice.action_post()
        ledger = invoice.line_ids.read(
            ["account_id", "currency_id", "debit", "credit", "balance", "tax_ids"]
        )
        self.assertEqual(
            (invoice.amount_untaxed, invoice.amount_tax, invoice.amount_total),
            (100, 5, 105),
        )
        invoice.action_l10n_ae_prepare()
        issue = invoice.l10n_ae_transmission_id
        tree = etree.fromstring(issue.xml_content.encode())
        item = tree.find("cac:InvoiceLine/cac:Item", NS)
        self.assertEqual(
            item.xpath(
                "cac:CommodityClassification/cbc:CommodityCode/text()", namespaces=NS
            ),
            ["S"],
        )
        sac = item.find("cac:AdditionalItemIdentification/cbc:ID", NS)
        self.assertIsNotNone(sac)
        self.assertEqual(sac.text, "SYNTHETIC-SAC-01")
        self.assertEqual(
            sac.attrib, {"schemeID": "SAC", "schemeVersionID": "SYNTHETIC-SAC-2026"}
        )
        self.assertFalse(
            item.xpath(
                "cac:CommodityClassification/cbc:ItemClassificationCode", namespaces=NS
            )
        )
        self.assertEqual(
            json.loads(issue.payload)["lines"][0]["item_classification"],
            {
                "type": "S",
                "hs_code": None,
                "sac_code": "SYNTHETIC-SAC-01",
                "sac_scheme_version": "SYNTHETIC-SAC-2026",
            },
        )
        report = invoice.validate_l10n_ae_pint()
        self.assertTrue(report["valid"], report)
        original = issue.read(
            ["payload", "payload_hash", "xml_content", "xml_hash", "idempotency_key"]
        )
        product.write(
            {
                "l10n_ae_service_accounting_code": "SYNTHETIC-LATER-SAC",
                "l10n_ae_service_accounting_version": "SYNTHETIC-LATER-VERSION",
                "list_price": 999,
            }
        )
        self.assertEqual(invoice.preview_l10n_ae_pint(), issue.xml_content)
        invoice.action_l10n_ae_prepare()
        self.assertEqual(invoice.l10n_ae_transmission_id, issue)
        self.assertEqual(
            issue.read(
                [
                    "payload",
                    "payload_hash",
                    "xml_content",
                    "xml_hash",
                    "idempotency_key",
                ]
            ),
            original,
        )
        self.assertEqual(
            invoice.line_ids.read(
                ["account_id", "currency_id", "debit", "credit", "balance", "tax_ids"]
            ),
            ledger,
        )
        self.assertFalse(issue.attempt_ids)
        self.assertFalse(issue.event_ids)
