# Copyright 2026 KMEE
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Offline validation of invoice bytes against pinned UBL and PINT resources."""

import hashlib
import json
import math
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import quote

from lxml import etree

MAX_XML_BYTES = 10 * 1024 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_BATCH_REQUESTS = 20
_ACTIVE_BATCH = ContextVar("uae_pint_validation_batch", default=None)
UBL_BASE = "https://docs.oasis-open.org/ubl/os-UBL-2.1/"
MANIFEST = Path(__file__).with_name("normative.json")
DEFAULT_RULESET = "kmee-pint-ae-1.0.4-ubl-2.1"
RULESETS = {
    DEFAULT_RULESET: MANIFEST,
    "kmee-pint-ae-1.0.3-ubl-2.1": MANIFEST.with_name("normative-1.0.3.json"),
}
SVRL_NS = "http://purl.oclc.org/dsdl/svrl"


class UnsafeInvoiceXml(ValueError):
    """Rejected input, with diagnostics that do not expose its contents."""


class ValidationUnavailable(RuntimeError):
    """Technical failure; never interpreted as fiscal approval."""


def _manifest_path(ruleset):
    if not isinstance(ruleset, str) or ruleset not in RULESETS:
        raise ValidationUnavailable("Unknown trusted UAE normative ruleset.")
    return RULESETS[ruleset]


def describe_ruleset(ruleset=DEFAULT_RULESET):
    """Identify a shipped trusted package without claiming XML conformance."""
    try:
        content = _manifest_path(ruleset).read_bytes()
    except OSError as error:
        raise ValidationUnavailable(
            "Trusted normative manifest is unavailable."
        ) from error
    try:
        manifest = json.loads(content)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValidationUnavailable("Invalid trusted normative manifest.") from error
    if manifest.get("version") != ruleset:
        raise ValidationUnavailable("Normative manifest identity mismatch.")
    return {
        "ruleset": ruleset,
        "manifest_sha256": hashlib.sha256(content).hexdigest(),
    }


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


def _check_report_identity(report, identity):
    if not isinstance(report, dict):
        raise ValidationUnavailable("Invalid offline validation result.")
    if report.get("technical_error"):
        raise ValidationUnavailable(report["technical_error"])
    resources = report.get("resources")
    if (
        report.get("ruleset") != identity["ruleset"]
        or not isinstance(resources, dict)
        or resources.get("manifest_sha256") != identity["manifest_sha256"]
    ):
        raise ValidationUnavailable("Normative manifest identity mismatch.")


class OfflinePintValidator:
    """Public XML boundary, isolated by document or explicit bounded batch.

    Resource acquisition is separate. A missing/changed resource fails closed.
    CPU and file limits complement the supervisor deadline. Linux also enforces
    an address-space limit; macOS must enforce memory through its runtime.
    """

    def __init__(self, resource_directory, *, timeout=30, ruleset=DEFAULT_RULESET):
        self.resource_directory = Path(resource_directory).resolve()
        _manifest_path(ruleset)
        self.ruleset = ruleset
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
            raise ValueError("Validation deadline must be between zero and 60 seconds.")
        self.timeout = timeout

    def validate(self, content):
        parse_invoice_xml(content)
        batch = _ACTIVE_BATCH.get()
        if batch is not None:
            return batch.validate(self, content)
        identity = describe_ruleset(self.ruleset)
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
                        self.ruleset,
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
                _check_report_identity(report, identity)
                return report


@contextmanager
def validation_batch():
    """Reuse one bounded worker within this serial caller's explicit scope.

    Nested helpers share the outer scope; ordinary validate calls remain isolated.
    No worker, document or compiled artifact is shared with a later batch.
    """
    current = _ACTIVE_BATCH.get()
    if current is not None:
        current.check_owner()
        yield current
        return
    batch = _ValidationBatch()
    token = _ACTIVE_BATCH.set(batch)
    try:
        yield batch
    finally:
        _ACTIVE_BATCH.reset(token)
        batch.close()


class _ValidationBatch:
    def __init__(self):
        self.owner = (os.getpid(), threading.get_ident())
        self.closed = False
        self.worker = None
        self.key = None
        self.worker_requests = 0
        self.worker_compilations = (0, 0)
        self.counters = {
            "worker_starts": 0,
            "requests": 0,
            "schema_compilations": 0,
            "stylesheet_compilations": 0,
        }

    @property
    def statistics(self):
        """Return operational counts separately from unchanged fiscal reports."""
        return dict(self.counters)

    def check_owner(self):
        if self.owner != (os.getpid(), threading.get_ident()):
            raise ValidationUnavailable("Validation batch belongs to another caller.")
        if self.closed:
            raise ValidationUnavailable("Validation batch is closed.")

    def close(self):
        self.check_owner()
        self.closed = True
        self._discard_worker()

    def _discard_worker(self):
        if self.worker is not None:
            self.worker.close()
            self.worker = None
        self.key = None

    def validate(self, validator, content):
        self.check_owner()
        identity = describe_ruleset(validator.ruleset)
        key = (
            validator.resource_directory,
            validator.ruleset,
            identity["manifest_sha256"],
            validator.timeout,
        )
        try:
            if (
                self.worker is None
                or key != self.key
                or self.worker_requests >= MAX_BATCH_REQUESTS
            ):
                self._discard_worker()
                self.worker = _BatchWorker(validator)
                self.key = key
                self.worker_requests = 0
                self.worker_compilations = (0, 0)
                self.counters["worker_starts"] += 1
            self.counters["requests"] += 1
            self.worker_requests += 1
            packet = self.worker.request(content, validator.timeout)
            report = packet["report"]
            counts = packet["compilations"]
            if not isinstance(report, dict) or (
                not isinstance(counts, list)
                or len(counts) != 2
                or any(type(value) is not int for value in counts)
                or not self.worker_compilations[0] <= counts[0] <= 2
                or not self.worker_compilations[1] <= counts[1] <= 8
            ):
                raise ValidationUnavailable("Invalid offline validation result.")
            for name, value, previous in zip(
                ("schema_compilations", "stylesheet_compilations"),
                counts,
                self.worker_compilations,
                strict=True,
            ):
                self.counters[name] += value - previous
            self.worker_compilations = tuple(counts)
            _check_report_identity(report, identity)
            return report
        except (ValidationUnavailable, OSError, KeyError, TypeError) as error:
            self._discard_worker()
            if isinstance(error, ValidationUnavailable):
                raise
            raise ValidationUnavailable("Offline validation worker failed.") from error


class _BatchWorker:
    def __init__(self, validator):
        with ExitStack() as startup:
            self.temporary = tempfile.TemporaryDirectory(prefix="uae-pint-batch-")
            startup.callback(self.temporary.cleanup)
            directory = Path(self.temporary.name)
            # Before Popen succeeds this owned directory contains only the diagnostic.
            # Remove by name: recursive cleanup itself needs descriptors on EMFILE.
            startup.callback(directory.rmdir)
            diagnostic_path = directory / "diagnostic"
            startup.callback(diagnostic_path.unlink, missing_ok=True)
            self.diagnostic = startup.enter_context(diagnostic_path.open("w+b"))
            self.channel, child = socket.socketpair()
            startup.callback(self.channel.close)
            with child:
                self.process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).resolve()),
                        str(validator.resource_directory),
                        validator.ruleset,
                        "--batch",
                    ],
                    stdin=child,
                    stdout=child,
                    stderr=self.diagnostic,
                    cwd=directory,
                    env={
                        "PATH": os.defpath,
                        "LANG": "C.UTF-8",
                        "TMPDIR": self.temporary.name,
                    },
                )
            startup.pop_all()

    def close(self):
        with ExitStack() as cleanup:
            cleanup.callback(self.temporary.cleanup)
            cleanup.callback(self.diagnostic.close)
            cleanup.callback(self.channel.close)
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()

    def _read(self, size, deadline):
        chunks = []
        remaining = size
        while remaining:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError
            self.channel.settimeout(budget)
            chunk = self.channel.recv(remaining)
            if not chunk:
                raise ValidationUnavailable("Offline validation worker failed.")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def request(self, content, timeout):
        deadline = time.monotonic() + timeout
        try:
            self.channel.settimeout(timeout)
            self.channel.sendall(struct.pack("!I", len(content)) + content)
            size = struct.unpack("!I", self._read(4, deadline))[0]
            if not 0 < size <= MAX_REPORT_BYTES:
                raise ValidationUnavailable("Offline validation worker failed.")
            packet = json.loads(self._read(size, deadline))
            if not isinstance(packet, dict):
                raise ValidationUnavailable("Invalid offline validation result.")
            return packet
        except TimeoutError as error:
            raise ValidationUnavailable(
                "Offline validation deadline exceeded."
            ) from error
        except (ValueError, UnicodeDecodeError) as error:
            raise ValidationUnavailable("Invalid offline validation result.") from error


class _PinnedResources:
    def __init__(self, directory, ruleset=DEFAULT_RULESET):
        self.directory = Path(directory)
        manifest_content = _manifest_path(ruleset).read_bytes()
        self.manifest = json.loads(manifest_content)
        if self.manifest["version"] != ruleset:
            raise ValidationUnavailable("Normative manifest identity mismatch.")
        self.manifest_hash = hashlib.sha256(manifest_content).hexdigest()
        self.verified = {}

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
        self.verified[name] = {"sha256": metadata["sha256"], "size": metadata["size"]}
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


class _CompiledValidation:
    """Worker-local trusted compilations; execution state is never reused."""

    def __init__(self):
        self.identity = None
        self.schemas = {}
        self.stylesheets = {}
        self.stack = ExitStack()
        self.processor = None

    @property
    def counts(self):
        return [len(self.schemas), len(self.stylesheets)]

    def schema(self, resources, document_type):
        identity = (resources.manifest["version"], resources.manifest_hash)
        if self.identity is None:
            self.identity = identity
        if self.identity != identity:
            raise ValidationUnavailable("Normative manifest identity mismatch.")
        if document_type not in self.schemas:
            self.schemas[document_type] = _schema(resources, document_type)
        else:
            # Integrity is checked for every document, including cached schemas.
            for name in resources.manifest["files"]:
                if name.startswith("ubl-2.1/"):
                    resources.verified_bytes(name)
        return self.schemas[document_type]

    def saxon(self):
        if self.processor is None:
            self.processor = self.stack.enter_context(_saxon_processor())
        return self.processor

    def executable(self, resources, name):
        stylesheet = resources.verified_bytes(name)
        if name not in self.stylesheets:
            compiler = self.saxon().new_xslt30_processor()
            self.stylesheets[name] = compiler.compile_stylesheet(
                stylesheet_text=stylesheet.decode("utf-8")
            )
        # Saxon executables retain mutable invocation state. The cached template
        # is never executed; each document gets a fresh execution clone.
        return self.stylesheets[name].clone()

    def close(self):
        self.stylesheets.clear()
        self.schemas.clear()
        self.stack.close()


def _schematron_failures(processor, result):
    """Bound actual diagnostics without serializing routine fired-rule traces."""
    if result is None or result.size != 1:
        raise ValidationUnavailable("Invalid offline validation result.")
    query = processor.new_xpath_processor()
    query.declare_namespace("svrl", SVRL_NS)
    query.set_context(xdm_item=result.item_at(0))
    assertions = query.evaluate("//svrl:failed-assert")
    failures = []
    size = 2  # JSON list brackets; the final worker response is bounded as well.
    for assertion in assertions if assertions is not None else []:
        fragment = assertion.to_string().encode("utf-8")
        if len(fragment) > MAX_REPORT_BYTES:
            raise ValidationUnavailable("Validation report size limit exceeded.")
        node = parse_invoice_xml(fragment)
        failure = {
            "code": node.get("id"),
            "location": node.get("location"),
            "message": " ".join(node.itertext()).strip(),
        }
        size += len(json.dumps(failure).encode("utf-8")) + 2
        if size > MAX_REPORT_BYTES:
            raise ValidationUnavailable("Validation report size limit exceeded.")
        failures.append(failure)
    return failures


def _validate_offline(content, directory, ruleset=DEFAULT_RULESET, *, compiled=None):
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
    resources = _PinnedResources(directory, ruleset)
    schema = (
        compiled.schema(resources, document_type)
        if compiled is not None
        else _schema(resources, document_type)
    )
    xsd_valid = schema.validate(root)
    report = {
        "valid": False,
        "xsd_valid": xsd_valid,
        "profile": profile,
        "ruleset": resources.manifest["version"],
        "resources": {
            "manifest_sha256": resources.manifest_hash,
            "files": resources.verified,
        },
        "layers": [],
        "supplemental_checks": [],
        "xsd_errors": [
            {"line": issue.line, "code": issue.type_name} for issue in schema.error_log
        ],
    }
    if not xsd_valid:
        return report
    transaction = "trn-invoice" if document_type == "Invoice" else "trn-creditnote"
    processor_scope = (
        nullcontext(compiled.saxon()) if compiled is not None else _saxon_processor()
    )
    with processor_scope as processor:
        node = processor.parse_xml(xml_text=etree.tostring(root, encoding="unicode"))
        compiler = processor.new_xslt30_processor()
        for name, filename in [
            ("shared", "PINT-UBL-validation-preprocessed.xslt"),
            ("ae", "PINT-jurisdiction-aligned-rules.xslt"),
        ]:
            resource_name = f"{package}/{transaction}/schematron/{filename}"
            if compiled is not None:
                executable = compiled.executable(resources, resource_name)
            else:
                stylesheet = resources.verified_bytes(resource_name)
                executable = compiler.compile_stylesheet(
                    stylesheet_text=stylesheet.decode("utf-8")
                )
            result = executable.transform_to_value(xdm_node=node)
            failures = _schematron_failures(processor, result)
            report["layers"].append(
                {"name": name, "resource": resource_name, "failures": failures}
            )
    # The specific supplier/customer templates in the pinned compiled packages
    # shadow their generic PostalAddress rule. Keep this check explicitly local:
    # neither the official SVRL layers nor their resource identity are rewritten.
    cac = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
    basic = cbc["cbc"]
    # IBR-128 also names delivery terms IBT-079/080, whose UBL binding uses
    # DeliveryLocation/Address rather than its compiled PostalAddress context.
    for name, addresses, message in (
        (
            "kmee-ae-postal-address-v1",
            root.iter(f"{{{cac}}}PostalAddress"),
            "A UAE postal address requires a valid emirate code.",
        ),
        (
            "kmee-ae-delivery-address-v1",
            root.findall(
                f"{{{cac}}}Delivery/{{{cac}}}DeliveryLocation/{{{cac}}}Address"
            ),
            "A UAE delivery address requires a valid emirate code.",
        ),
    ):
        failures = []
        for address in addresses:
            country = address.findtext(
                f"{{{cac}}}Country/{{{basic}}}IdentificationCode"
            )
            subdivision = address.findtext(f"{{{basic}}}CountrySubentity")
            if country == "AE" and subdivision not in {
                "AUH",
                "DXB",
                "SHJ",
                "UAQ",
                "FUJ",
                "AJM",
                "RAK",
            }:
                failures.append(
                    {
                        "code": "ibr-128-ae",
                        "location": root.getroottree().getpath(address),
                        "message": message,
                    }
                )
        report["supplemental_checks"].append(
            {
                "name": name,
                "source": (
                    "https://docs.peppol.eu/poac/ae/pint-ae/trn-invoice/rule/ibr-128-ae/"
                ),
                "failures": failures,
            }
        )
    # IBR-126 exempts ItemPriceExtension from document-currency equality.
    # The UAE semantic terms still require its provided amounts to be in AED.
    # Keep this local check separate from official SVRL and presence/value rules.
    failures = []
    for line_name in ("InvoiceLine", "CreditNoteLine"):
        for extension in root.findall(
            f"{{{cac}}}{line_name}/{{{cac}}}ItemPriceExtension"
        ):
            for path, term in (
                (f"{{{basic}}}Amount", "BTAE-10"),
                (f"{{{cac}}}TaxTotal/{{{basic}}}TaxAmount", "BTAE-08"),
            ):
                for amount in extension.findall(path):
                    if amount.get("currencyID") != "AED":
                        failures.append(
                            {
                                "code": term + "-currency",
                                "location": root.getroottree().getpath(amount),
                                "message": term + " must be expressed in AED.",
                                "source": (
                                    "https://docs.peppol.eu/poac/ae/pint-ae/"
                                    "trn-invoice/semantic-model/" + term.lower() + "/"
                                ),
                            }
                        )
    report["supplemental_checks"].append(
        {"name": "kmee-ae-line-aed-currency-v1", "failures": failures}
    )
    report["valid"] = all(
        not check["failures"]
        for check in report["layers"] + report["supplemental_checks"]
    )
    return report


def _worker_limits(*, batch=False):
    import resource

    cpu = 30 * MAX_BATCH_REQUESTS + 1 if batch else 30
    resource.setrlimit(resource.RLIMIT_CPU, (30, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
    if sys.platform.startswith("linux"):
        # SaxonC/Graal reserves ~32GiB of virtual address space at startup.
        # This is not an RSS quota; the deployment must limit physical memory.
        resource.setrlimit(resource.RLIMIT_AS, (64 * 1024**3, 64 * 1024**3))


def _batch_worker():
    import resource

    _worker_limits(batch=True)
    compiled = _CompiledValidation()
    try:
        for _index in range(MAX_BATCH_REQUESTS):
            header = sys.stdin.buffer.read(4)
            if not header:
                return
            if len(header) != 4:
                return
            size = struct.unpack("!I", header)[0]
            if not 0 < size <= MAX_XML_BYTES:
                return
            content = sys.stdin.buffer.read(size)
            if len(content) != size:
                return
            usage = resource.getrusage(resource.RUSAGE_SELF)
            # CPU limits are cumulative in a reused process. Give this request
            # at most another 30 CPU seconds, within the bounded worker lifetime.
            cpu = math.floor(usage.ru_utime + usage.ru_stime) + 30
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, 30 * MAX_BATCH_REQUESTS + 1))
            try:
                report = _validate_offline(
                    content, sys.argv[1], sys.argv[2], compiled=compiled
                )
            except (UnsafeInvoiceXml, ValidationUnavailable) as error:
                report = {"technical_error": str(error)}
            except Exception:  # noqa: BLE001 - redact every native worker failure
                report = {"technical_error": "Offline validation worker failed."}
            packet = json.dumps(
                {"report": report, "compilations": compiled.counts}
            ).encode("utf-8")
            if len(packet) > MAX_REPORT_BYTES:
                return
            sys.stdout.buffer.write(struct.pack("!I", len(packet)) + packet)
            sys.stdout.buffer.flush()
    finally:
        compiled.close()


def _worker():
    _worker_limits()
    try:
        report = _validate_offline(
            sys.stdin.buffer.read(MAX_XML_BYTES + 1), sys.argv[1], sys.argv[2]
        )
    except (UnsafeInvoiceXml, ValidationUnavailable) as error:
        report = {"technical_error": str(error)}
    except Exception:  # noqa: BLE001 - redact every native worker failure
        # Never print native diagnostics, input XML or host filesystem paths.
        report = {"technical_error": "Offline validation worker failed."}
    sys.stdout.write(json.dumps(report))


if __name__ == "__main__":
    if sys.argv[3:] == ["--batch"]:
        _batch_worker()
    else:
        _worker()
