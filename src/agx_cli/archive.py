"""Archive naming shared by upgrade snapshots and exports.

Both are tar+zstd archives written by ``tar --zstd`` inside the VM (or
on the host under bare_metal) — the host never compresses or extracts
them itself. The suffix lives here so callers don't hardcode it.
"""

from __future__ import annotations

import re
from typing import Final

ARCHIVE_SUFFIX: Final = ".tar.zst"

# ``YYYYMMDDTHHMMSSZ`` — sorts lexicographically in time order.
UTC_STAMP_RE: Final = r"\d{8}T\d{6}Z"


def archive_name_re(name: str) -> re.Pattern[str]:
    """Match ``<name>-<UTC stamp>`` + the archive suffix, exactly.

    A plain ``<name>-*`` glob would also match agent ``<name>-other``'s
    archives, so retention for ``foo`` could delete ``foo-bar``'s.
    """
    return re.compile(re.escape(name) + "-" + UTC_STAMP_RE + re.escape(ARCHIVE_SUFFIX))
