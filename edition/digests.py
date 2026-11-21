import hashlib
import json
from collections.abc import Mapping


def digest_block(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_revision(fields: Mapping[str, object]) -> str:
    """Hash the directory fields excluding the digest field itself."""
    encoded = json.dumps(
        dict(fields), sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False
    ).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()
