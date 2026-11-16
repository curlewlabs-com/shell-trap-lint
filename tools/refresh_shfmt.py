"""Finish an shfmt bump in CI: rewrite the digests beside its version.

A bump moves `SHFMT_VERSION` in .github/install-shfmt.sh. The sha256 of each
release asset CI installs follows from that version and cannot be typed from
it, so this downloads each asset for the version the file now names and writes
its digest beside it.

    python3 tools/refresh_shfmt.py

It fails, changing nothing, when the script does not name exactly one of each
line it reads or rewrites, or when an asset cannot be downloaded.
"""

from __future__ import annotations

import hashlib
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / ".github/install-shfmt.sh"

_ASSET = (
    "https://github.com/mvdan/sh/releases/download/{version}/shfmt_{version}_{asset}"
)

# Each digest line the installer checks, with the release asset it is the
# digest of.
ASSETS = {
    "SHFMT_SHA256_LINUX_AMD64": "linux_amd64",
    "SHFMT_SHA256_LINUX_ARM64": "linux_arm64",
    "SHFMT_SHA256_DARWIN_ARM64": "darwin_arm64",
}


class RefreshError(Exception):
    """The installer is not in a state this can finish a bump from."""


def _one(text: str, name: str) -> re.Match[str]:
    matches = list(re.finditer(rf"^{name}=(?P<value>\S+)$", text, re.MULTILINE))
    if len(matches) != 1:
        raise RefreshError(
            f"{INSTALLER.name} names {name} {len(matches)} times, not once"
        )
    return matches[0]


def _download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            body: bytes = response.read()
            return body
    except OSError as e:
        raise RefreshError(f"could not download {url}: {e!r}") from e


def refresh(text: str) -> str:
    """`text` with each digest line set to its asset's sha256."""
    version = _one(text, "SHFMT_VERSION")["value"]
    digests: dict[str, str] = {}
    for name, asset in ASSETS.items():
        _one(text, name)
        body = _download(_ASSET.format(version=version, asset=asset))
        digests[name] = hashlib.sha256(body).hexdigest()
    for name, sha256 in digests.items():
        match = _one(text, name)
        text = text[: match.start("value")] + sha256 + text[match.end("value") :]
    return text


def main() -> int:
    try:
        text = INSTALLER.read_text()
        updated = refresh(text)
    except RefreshError as e:
        print(f"refresh_shfmt: {e}", file=sys.stderr)
        return 1
    if updated != text:
        INSTALLER.write_text(updated)
        print(f"refresh_shfmt: rewrote the digests in {INSTALLER.relative_to(ROOT)}")
    else:
        print("refresh_shfmt: every digest already matches its asset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
