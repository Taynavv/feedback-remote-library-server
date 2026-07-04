# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import hashlib


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()