"""Pet-pure config stub.

The retired project's config.py (2486 lines) carried app-home discovery plus
the whole settings object. The vendored closure needs exactly two things
from it: APP_DIR_NAME for external character-dir discovery, and the two pure
helpers already inlined into slot_manager. Everything else (settings file,
profile manager) is intentionally absent — a pure pet has no settings.

If the closure's surface ever grows beyond APP_DIR_NAME, grow THIS file
rather than re-importing the retired project's config (that would drag the
whole product layer back in).
"""

from __future__ import annotations

import os
import sys

#: Data-dir app name used by the discovery chain in catalog.
APP_DIR_NAME = os.environ.get("DSH_PET_PURE_APP_DIR") or "dsh-pet-pure"


def _bool_or_default(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return bool(default)
