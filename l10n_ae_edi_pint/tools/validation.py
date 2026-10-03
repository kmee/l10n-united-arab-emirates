# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Offline validation entry points for untrusted electronic invoice XML."""

from lxml import etree


class UnsafeInvoiceXml(ValueError):
    """Rejected input, with diagnostics that do not expose its contents."""


def parse_invoice_xml(content, *, max_bytes=10 * 1024 * 1024):
    """Parse without network/entity resolution; reject DTD in every encoding."""
    if not isinstance(content, bytes) or not content or len(content) > max_bytes:
        raise UnsafeInvoiceXml("Invalid XML input or configured size limit exceeded.")
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
    )
    try:
        root = etree.fromstring(content, parser)
    except etree.XMLSyntaxError as error:
        raise UnsafeInvoiceXml("Invalid invoice XML.") from error
    if root.getroottree().docinfo.doctype:
        raise UnsafeInvoiceXml("DTD is forbidden in invoice XML.")
    return root
