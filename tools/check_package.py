#!/usr/bin/env python3
"""Verify the distributable wheel contains exactly the reviewed public runtime."""
from pathlib import Path, PurePosixPath
import argparse
import base64
import csv
import io
import re
import stat
import tomllib
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
#: The dependency set CI installs and tests (`pip install -c requirements-full.lock`). A
#: release publishes only the wheel and SHA256SUMS, so the wheel carries the lock for
#: `full update` and for a wheel user's own install; it must be the tested one, byte for byte.
LOCK = "requirements-full.lock"
LOCK_MEMBER = "observatory/engine/" + LOCK
#: A held release's marker (engine_update.RELEASE_MEMBER, LC-16): when the wheel carries
#: one, it must name this very version and say what the person does. One written for an
#: earlier release and left in the tree would otherwise be ignored by the updater in silence.
RELEASE_MEMBER = "observatory/engine/RELEASE.json"


def check(wheel: Path) -> dict:
    failures = []
    prefix = 'observatory/engine/'
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        distribution = re.sub(r"[-_.]+", "_", project["name"])
        metadata_root = distribution + "-" + project["version"] + ".dist-info"
        metadata_names = {metadata_root + "/" + name for name in
                          ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt", "LICENSE", "licenses/LICENSE",
                           "licenses/COMMERCIAL-LICENSE.md")}
        for required in ("METADATA", "WHEEL", "RECORD"):
            if metadata_root + "/" + required not in names:
                failures.append("missing package metadata: " + required)
        for info in z.infolist():
            if stat.S_ISLNK(info.external_attr >> 16):
                failures.append("symbolic link archive entry")
        if len(names) != len(set(names)):
            failures.append('duplicate archive paths')
        for name in names:
            p = PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts or '\\' in name:
                failures.append('unsafe archive path')
        source_manifest = json.loads((ROOT / prefix / 'SOURCE-INVENTORY.json').read_text())
        expected = {prefix + f['path'] for f in source_manifest['files']}
        expected.add(prefix + 'SOURCE-INVENTORY.json')
        expected.update('observatory/' + p.name for p in (ROOT / 'observatory').glob('*.py'))
        for name in expected:
            if name not in names:
                failures.append('missing runtime resource: ' + name)
            elif z.read(name) != (ROOT / name).read_bytes():
                failures.append('wheel differs from reviewed source: ' + name)
        for row in source_manifest['files']:
            name = prefix + row['path']
            if name in names and hashlib.sha256(z.read(name)).hexdigest() != row['export_sha256']:
                failures.append('export inventory digest mismatch: ' + row['path'])
        if LOCK_MEMBER not in names:
            failures.append('the tested dependency lock is missing: ' + LOCK_MEMBER)
        elif z.read(LOCK_MEMBER) != (ROOT / LOCK).read_bytes():
            failures.append('the tested dependency lock differs from ' + LOCK + '; run tools/sync_engine_docs.py')
        if RELEASE_MEMBER in names:
            try:
                marker = json.loads(z.read(RELEASE_MEMBER).decode("utf-8"))
            except (UnicodeError, ValueError):
                marker = None
            if not isinstance(marker, dict) or marker.get("version") != project["version"] \
                    or not isinstance(marker.get("needs_person"), str) or not marker["needs_person"].strip():
                failures.append(RELEASE_MEMBER + ' must be {"version": "' + project["version"]
                                + '", "needs_person": "<the step>"}; delete it when the release needs no person')
        for name in names:
            if name not in expected and name not in metadata_names:
                failures.append('unexpected package resource: ' + name)
        record_name = metadata_root + "/RECORD"
        if record_name in names:
            try:
                rows = list(csv.reader(io.StringIO(z.read(record_name).decode("utf-8"))))
                records = {}
                for row in rows:
                    if len(row) != 3 or row[0] in records:
                        raise ValueError("invalid record")
                    records[row[0]] = row[1:]
                if set(records) != set(names):
                    failures.append("RECORD members differ from archive")
                for name in set(records) & set(names):
                    declared_hash, declared_size = records[name]
                    if name == record_name:
                        if declared_hash or declared_size:
                            failures.append("RECORD must leave its own digest and size empty")
                        continue
                    data = z.read(name)
                    digest = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
                    if declared_hash != digest or declared_size != str(len(data)):
                        failures.append("RECORD digest or size mismatch: " + name)
            except (UnicodeError, ValueError, csv.Error):
                failures.append("malformed RECORD")
    return {'gate': 'wheel-content', 'passed': not failures,
            'runtime_files': len(expected), 'archive_files': len(names), 'failures': failures}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('wheel', type=Path)
    a = p.parse_args()
    try:
        result = check(a.wheel)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        result = {'gate': 'wheel-content', 'passed': False, 'failures': ['unreadable package or inventory']}
    print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
