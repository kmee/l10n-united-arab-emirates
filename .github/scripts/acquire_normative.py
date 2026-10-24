#!/usr/bin/env python3
"""Acquire pinned official resources separately from invoice validation.

Original normative resources stay in ignored runtime storage, never in Git.
A digest mismatch requires review; this command cannot refresh pinned hashes.
"""

import argparse
import hashlib
import io
import json
import os
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "l10n_ae_edi_pint/tools/normative.json"
DEFAULT_RULESET = "kmee-pint-ae-1.0.4-ubl-2.1"
RULESETS = {
    DEFAULT_RULESET: MANIFEST,
    "kmee-pint-ae-1.0.3-ubl-2.1": MANIFEST.with_name("normative-1.0.3.json"),
}
MAX_ARCHIVE = 70 * 1024 * 1024


def acquire(cache, *, archive_directory=None, ruleset=DEFAULT_RULESET):
    if not isinstance(ruleset, str) or ruleset not in RULESETS:
        raise ValueError("Unknown trusted UAE normative ruleset.")
    manifest = json.loads(RULESETS[ruleset].read_bytes())
    if manifest["version"] != ruleset:
        raise ValueError("Normative manifest identity mismatch.")
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    count = 0
    for archive in manifest["archives"]:
        if archive_directory:
            content = (
                Path(archive_directory) / (archive["name"] + ".zip")
            ).read_bytes()
        else:
            with urllib.request.urlopen(archive["url"], timeout=30) as response:
                if not response.url.startswith("https://"):
                    raise ValueError("Normative acquisition requires HTTPS.")
                content = response.read(MAX_ARCHIVE + 1)
        if (
            len(content) != archive["size"]
            or len(content) > MAX_ARCHIVE
            or hashlib.sha256(content).hexdigest() != archive["sha256"]
        ):
            raise ValueError("Normative archive integrity check failed.")
        with zipfile.ZipFile(io.BytesIO(content)) as package:
            names = package.namelist()
            for member, destination in archive["members"].items():
                path = PurePosixPath(destination)
                if path.is_absolute() or ".." in path.parts or "\\" in destination:
                    raise ValueError("Unsafe normative resource path.")
                if names.count(member) != 1:
                    raise ValueError("Normative archive member missing or duplicated.")
                info = package.getinfo(member)
                metadata = manifest["files"][destination]
                if (
                    stat.S_ISLNK(info.external_attr >> 16)
                    or info.file_size != metadata["size"]
                ):
                    raise ValueError("Unexpected normative archive member.")
                resource = package.read(info)
                if hashlib.sha256(resource).hexdigest() != metadata["sha256"]:
                    raise ValueError("Normative resource integrity check failed.")
                target = cache / destination
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    dir=target.parent, delete=False
                ) as output:
                    output.write(resource)
                    temporary = Path(output.name)
                temporary.chmod(0o444)
                os.replace(temporary, target)
                count += 1
    return {"ruleset": manifest["version"], "verified_files": count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--ruleset", choices=RULESETS, default=DEFAULT_RULESET)
    parser.add_argument(
        "--archive-directory", type=Path, help="Previously acquired name.zip archives"
    )
    args = parser.parse_args()
    default_cache = (
        ROOT
        / ".runtime"
        / ("normative" if args.ruleset == DEFAULT_RULESET else "normative-1.0.3")
    )
    print(
        json.dumps(
            acquire(
                args.cache or default_cache,
                archive_directory=args.archive_directory,
                ruleset=args.ruleset,
            )
        )
    )


if __name__ == "__main__":
    main()
