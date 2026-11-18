"""Artifact envelopes, atomic persistence and revision-scoped identities."""
import hashlib
import json
import os
import tempfile
from pathlib import Path

SCHEMA_VERSION = 'c1-1'
SNAPSHOTS = ('post_rtl_pre_validation', 'post_local_witness', 'post_validation')
STATUSES = ('supported', 'unconfirmed', 'implementation_error', 'implementation_freedom')


def digest(value):
    if not isinstance(value, (str, bytes)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


def file_hash(path):
    return digest(Path(path).read_bytes()) if path and Path(path).is_file() else None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return str(path.resolve())


def envelope(spec_path=None, ir_path=None, rtl_path=None, case_id=None, attempt_id=0,
             snapshot_id=None, tool_config=None):
    return dict(schema_version=SCHEMA_VERSION, case_id=case_id or 'run',
                attempt_id=attempt_id, spec_hash=file_hash(spec_path), ir_hash=file_hash(ir_path),
                rtl_hash=file_hash(rtl_path), snapshot_id=snapshot_id,
                tool_config=tool_config or {})


def ir_id(kind, name, suffix=None):
    result = f'IR:{kind}:{name}'
    return result if suffix is None else f'{result}:{suffix}'
