# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Offline validation of invoice bytes against pinned UBL and PINT resources."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from lxml import etree

MAX_XML_BYTES = 10 * 1024 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
UBL_BASE = "https://docs.oasis-open.org/ubl/os-UBL-2.1/"
MANIFEST = Path(__file__).with_name("normative.json")
SVRL_NS = "http://purl.oclc.org/dsdl/svrl"


class UnsafeInvoiceXml(ValueError):
    """Rejected input, with diagnostics that do not expose its contents."""


class ValidationUnavailable(RuntimeError):
    """Technical failure; never interpreted as fiscal approval."""


def parse_invoice_xml(content, *, max_bytes=MAX_XML_BYTES):
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


class OfflinePintValidator:
    """Public XML boundary. Every invocation runs in a disposable subprocess.

    Resource acquisition is separate. A missing/changed resource fails closed.
    CPU and file limits complement the supervisor deadline. Linux also enforces
    an address-space limit; macOS must enforce memory through its runtime.
    """

    def __init__(self, resource_directory, *, timeout=30):
        self.resource_directory = Path(resource_directory).resolve()
        if timeout <= 0 or timeout > 60:
            raise ValueError("Validation deadline must be between zero and 60 seconds.")
        self.timeout = timeout

    def validate(self, content):
        parse_invoice_xml(content)
        with tempfile.TemporaryDirectory(prefix="uae-pint-") as temporary:
            directory = Path(temporary)
            with (
                (directory / "report").open("w+b") as output,
                (directory / "diagnostic").open("w+b") as diagnostic,
            ):
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).resolve()),
                        str(self.resource_directory),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=output,
                    stderr=diagnostic,
                    cwd=directory,
                    env={"PATH": os.defpath, "LANG": "C.UTF-8", "TMPDIR": temporary},
                )
                try:
                    process.communicate(input=content, timeout=self.timeout)
                except subprocess.TimeoutExpired as error:
                    process.kill()
                    process.communicate()
                    raise ValidationUnavailable(
                        "Offline validation deadline exceeded."
                    ) from error
                output.seek(0)
                result = output.read(MAX_REPORT_BYTES + 1)
                if process.returncode or len(result) > MAX_REPORT_BYTES:
                    raise ValidationUnavailable("Offline validation worker failed.")
                try:
                    report = json.loads(result)
                except (ValueError, UnicodeDecodeError) as error:
                    raise ValidationUnavailable(
                        "Invalid offline validation result."
                    ) from error
                if report.get("technical_error"):
                    raise ValidationUnavailable(report["technical_error"])
                return report


class _PinnedResources:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.manifest = json.loads(MANIFEST.read_bytes())

    def verified_bytes(self, name):
        metadata = self.manifest["files"][name]
        try:
            with (self.directory / name).open("rb") as resource:
                content = resource.read(metadata["size"] + 1)
        except OSError as error:
            raise ValidationUnavailable(
                "Required normative resource is unavailable."
            ) from error
        if (
            len(content) != metadata["size"]
            or hashlib.sha256(content).hexdigest() != metadata["sha256"]
        ):
            raise ValidationUnavailable("Normative resource integrity check failed.")
        return content


class _SchemaResolver(etree.Resolver):
    def __init__(self, schemas):
        self.schemas = schemas

    def resolve(self, url, public_id, context):
        if url not in self.schemas:
            raise ValidationUnavailable(
                "Schema dependency is outside the pinned resource set."
            )
        return self.resolve_string(self.schemas[url], context, base_url=url)


def _schema(resources, document_type):
    schemas = {
        UBL_BASE + name.removeprefix("ubl-2.1/"): resources.verified_bytes(name)
        for name in resources.manifest["files"]
        if name.startswith("ubl-2.1/")
    }
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
    parser.resolvers.add(_SchemaResolver(schemas))
    url = UBL_BASE + f"xsd/maindoc/UBL-{document_type}-2.1.xsd"
    # Trusted DSig schema has an internal DTD: preserve its original pinned bytes.
    root = etree.fromstring(schemas[url], parser, base_url=url)
    return etree.XMLSchema(root)


def _saxon_processor():
    from saxonche import PySaxonProcessor

    processor = PySaxonProcessor(license=False)
    feature = "http://saxon.sf.net/feature/"
    properties = {
        "allowedProtocols": "",
        "allow-external-functions": "false",
        "xinclude-aware": "false",
        "validation": "false",
    }
    for name, value in properties.items():
        processor.set_configuration_property(feature + name, value)
    parser_flags = {
        "http://apache.org/xml/features/disallow-doctype-decl": "true",
        "http://xml.org/sax/features/external-general-entities": "false",
        "http://xml.org/sax/features/external-parameter-entities": "false",
        "http://apache.org/xml/features/nonvalidating/load-external-dtd": "false",
    }
    for uri, value in parser_flags.items():
        processor.set_configuration_property(
            feature + "parserFeature?uri=" + quote(uri, safe=""), value
        )
    return processor


def _validate_offline(content, directory):
    root = parse_invoice_xml(content)
    document_type = etree.QName(root).localname
    expected_namespace = (
        f"urn:oasis:names:specification:ubl:schema:xsd:{document_type}-2"
    )
    if (
        document_type not in {"Invoice", "CreditNote"}
        or etree.QName(root).namespace != expected_namespace
    ):
        raise UnsafeInvoiceXml("Unsupported UBL document root.")
    cbc = {
        "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
    }
    customization = root.findtext("cbc:CustomizationID", namespaces=cbc)
    profiles = {
        "urn:peppol:pint:billing-1@ae-1": ("billing", "pint-ae"),
        "urn:peppol:pint:selfbilling-1@ae-1": ("selfbilling", "pint-ae-sb"),
    }
    if customization not in profiles:
        raise UnsafeInvoiceXml("Unsupported PINT-AE customization.")
    profile, package = profiles[customization]
    resources = _PinnedResources(directory)
    schema = _schema(resources, document_type)
    xsd_valid = schema.validate(root)
    report = {
        "valid": False,
        "xsd_valid": xsd_valid,
        "profile": profile,
        "ruleset": resources.manifest["version"],
        "layers": [],
        "xsd_errors": [
            {"line": issue.line, "code": issue.type_name} for issue in schema.error_log
        ],
    }
    if not xsd_valid:
        return report
    transaction = "trn-invoice" if document_type == "Invoice" else "trn-creditnote"
    with _saxon_processor() as processor:
        node = processor.parse_xml(xml_text=etree.tostring(root, encoding="unicode"))
        compiler = processor.new_xslt30_processor()
        for name, filename in [
            ("shared", "PINT-UBL-validation-preprocessed.xslt"),
            ("ae", "PINT-jurisdiction-aligned-rules.xslt"),
        ]:
            stylesheet = resources.verified_bytes(
                f"{package}/{transaction}/schematron/{filename}"
            )
            executable = compiler.compile_stylesheet(
                stylesheet_text=stylesheet.decode("utf-8")
            )
            svrl = executable.transform_to_string(xdm_node=node)
            if not svrl or len(svrl.encode("utf-8")) > MAX_REPORT_BYTES:
                raise ValidationUnavailable("Validation report size limit exceeded.")
            result = parse_invoice_xml(svrl.encode("utf-8"))
            failures = [
                {
                    "code": assertion.get("id"),
                    "location": assertion.get("location"),
                    "message": " ".join(assertion.itertext()).strip(),
                }
                for assertion in result.iter(f"{{{SVRL_NS}}}failed-assert")
            ]
            report["layers"].append({"name": name, "failures": failures})
    report["valid"] = all(not layer["failures"] for layer in report["layers"])
    return report


def _worker():
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
    if sys.platform.startswith("linux"):
        # SaxonC/Graal reserves ~32GiB of virtual address space at startup.
        # This is not an RSS quota; the deployment must limit physical memory.
        resource.setrlimit(resource.RLIMIT_AS, (64 * 1024**3, 64 * 1024**3))
    try:
        report = _validate_offline(
            sys.stdin.buffer.read(MAX_XML_BYTES + 1), sys.argv[1]
        )
    except (UnsafeInvoiceXml, ValidationUnavailable) as error:
        report = {"technical_error": str(error)}
    except Exception:  # noqa: BLE001 - redact every native worker failure
        # Never print native diagnostics, input XML or host filesystem paths.
        report = {"technical_error": "Offline validation worker failed."}
    sys.stdout.write(json.dumps(report))


if __name__ == "__main__":
    _worker()
