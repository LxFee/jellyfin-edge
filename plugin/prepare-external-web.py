#!/usr/bin/python3
"""Prepare a disposable web copy; never download the external script."""
import html
import ipaddress
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import urlsplit


def validate_url(value):
    # Restrict to printable ASCII URI characters, not HTML or quoted attributes.
    if not value.startswith("https://") or re.search(r"[^A-Za-z0-9:/?#\[\]@!$&()*+,;=._~%+-]", value):
        raise ValueError("external script must be an ASCII https URL without quotes/markup/whitespace")
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("invalid URL percent escape")
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None:
        raise ValueError("external script must have an HTTPS host and no credentials")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("invalid URL port")
    host = url.hostname
    if ":" in host:
        ipaddress.IPv6Address(host)
    elif not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
        raise ValueError("invalid URL host")
    return value


def prepare(source, target, url):
    url = validate_url(url)
    source, target = Path(source).resolve(), Path(target)
    if target.is_symlink():
        raise ValueError("web cache must not be a symlink")
    resolved = target.resolve()
    if source == resolved or source in resolved.parents or resolved in source.parents:
        raise ValueError("web source and cache must be separate directories")
    index = (source / "index.html").read_text(encoding="utf-8")
    if 'id="jellyfin-edge-external-script"' in index:
        raise ValueError("web source is not pristine (already injected)")
    closing = list(re.finditer(r"</head\s*>", index, re.IGNORECASE))
    if len(closing) != 1:
        raise ValueError("official index.html must have exactly one closing head tag")
    tag = '<script id="jellyfin-edge-external-script" defer src="' + html.escape(url, quote=True) + '"></script>\n'
    position = closing[0].start()
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".edge-web-", dir=target.parent))
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True)
        # A read-only official source is fine: only the copied index is changed.
        copied_index = staging / "index.html"
        copied_index.chmod(copied_index.stat().st_mode | 0o200)
        copied_index.write_text(index[:position] + tag + index[position:], encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


if __name__ == "__main__":
    try:
        prepare(sys.argv[1], sys.argv[2], os.environ["JELLYFIN_EXTERNAL_SCRIPT_URL"])
    except Exception as error:
        # Do not echo the supplied URL (it could contain sensitive query strings).
        print("jellyfin-edge: external web preparation failed: " + str(error), file=sys.stderr)
        sys.exit(1)
