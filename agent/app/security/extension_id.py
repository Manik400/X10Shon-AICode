"""Deterministic id of an unpacked Chrome extension.

Chrome derives the id of an unpacked extension from the absolute path it was loaded from
(``crx_file::id_util::GenerateIdForPath``): on Windows the path is lower-cased and hashed as its
UTF-16 bytes, elsewhere as UTF-8; the first 16 bytes of SHA-256 are hex-encoded and each hex
digit mapped to the letters a-p. Knowing the id of *our own* ``extension/dist`` folder lets the
agent trust a pairing claim coming from exactly that extension on this machine without a code:
no web page can present a ``chrome-extension://`` origin, and no other extension can have this
id unless it was loaded from this path.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def unpacked_extension_id(path: Path | str) -> str:
    p = Path(path).resolve()
    s = str(p)
    # Verified against a real Chrome 155 install: Windows hashes the UTF-16 bytes of the path in
    # its original case (the lower-casing in older Chromium sources is not applied).
    data = s.encode("utf-16-le") if os.name == "nt" else s.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def repo_extension_dist() -> Path:
    return Path(__file__).resolve().parents[3] / "extension" / "dist"
