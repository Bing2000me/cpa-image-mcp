#!/usr/bin/env python3
"""Prepare and validate a private service environment without exposing its values."""

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import sys
import tempfile
import time
import urllib.request


_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_AUTH = re.compile(r"[A-Za-z0-9_-]{24,}\Z")
_DOMAIN = re.compile(r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def parse_env(path):
    """Read simple dotenv assignments literally; never evaluate shell syntax."""
    values = {}
    for number, line in enumerate(Path(path).read_text("utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if not match:
            raise ValueError(f"Invalid environment syntax at line {number}")
        name, raw = match.groups()
        try:
            words = shlex.split(raw, comments=True, posix=True)
        except ValueError:
            raise ValueError(f"Invalid environment syntax for {name}") from None
        if len(words) > 1:
            raise ValueError(f"Invalid environment syntax for {name}")
        if name in values:
            raise ValueError(f"Duplicate environment field: {name}")
        values[name] = words[0] if words else ""
    return values


def _placeholder(value):
    value = value.strip().lower()
    return not value or any(marker in value for marker in ("replace", "your", "changeme", "change-me", "change_me", "example", "<", ">"))


def _positive_integer(value):
    return bool(re.fullmatch(r"[0-9]+", value)) and int(value) > 0


def validate_env(path):
    values = parse_env(path)
    invalid = []
    if _placeholder(values.get("CPA_API_KEY", "")):
        invalid.append("CPA_API_KEY")
    host = values.get("PUBLIC_HOST", "")
    if not _DOMAIN.fullmatch(host) or host.lower() == "images.example.com":
        invalid.append("PUBLIC_HOST")
    token, secret = values.get("MCP_TOKEN", ""), values.get("MCP_SECRET", "")
    if not token and not secret:
        invalid.extend(("MCP_TOKEN", "MCP_SECRET"))
    else:
        for name, value in (("MCP_TOKEN", token), ("MCP_SECRET", secret)):
            if value and not _AUTH.fullmatch(value):
                invalid.append(name)
    signing = values.get("DOWNLOAD_SIGNING_KEY", "")
    if len(signing) < 32 or _placeholder(signing):
        invalid.append("DOWNLOAD_SIGNING_KEY")
    for name in ("DOWNLOAD_TTL_HOURS", "IMAGE_RETENTION_DAYS"):
        if not _positive_integer(values.get(name, "")):
            invalid.append(name)
    port = values.get("PORT", "")
    if not _positive_integer(port) or not 1 <= int(port) <= 65535:
        invalid.append("PORT")
    if invalid:
        raise ValueError("Fields requiring configuration: " + ", ".join(invalid))
    return values


def _write_private(path, text):
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            output.write(text)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def prepare_env(template, path):
    template, path = Path(template), Path(path)
    if path.is_symlink():
        raise ValueError("Environment file must not be a symbolic link")
    created = not path.exists()
    source = template if created else path
    text = source.read_text("utf-8")
    values = parse_env(source)
    updates = {}
    if not values.get("MCP_TOKEN") and not values.get("MCP_SECRET"):
        updates["MCP_TOKEN"] = secrets.token_hex(32)
    if not values.get("DOWNLOAD_SIGNING_KEY"):
        updates["DOWNLOAD_SIGNING_KEY"] = secrets.token_hex(32)
    for name, default in (("DOWNLOAD_TTL_HOURS", "72"), ("IMAGE_RETENTION_DAYS", "3")):
        if not values.get(name):
            updates[name] = default
    lines = text.splitlines(keepends=True)
    remaining = dict(updates)
    for index, line in enumerate(lines):
        match = _ASSIGNMENT.match(line.rstrip("\r\n"))
        if match and match.group(1) in remaining:
            name = match.group(1)
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            lines[index] = f"{name}={shlex.quote(remaining.pop(name))}{newline}"
    prepared = "".join(lines)
    if remaining:
        if prepared and not prepared.endswith("\n"):
            prepared += "\n"
        prepared += "".join(f"{name}={shlex.quote(value)}\n" for name, value in remaining.items())
    changed = created or prepared != text
    backup = None
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not created:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = path.with_name(path.name + ".bak-" + stamp)
            # Create with 0600 from the outset, including before copying data.
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                with path.open("rb") as original:
                    shutil.copyfileobj(original, output)
            backup.chmod(0o600)
        _write_private(path, prepared)
    return {"created": created, "changed": changed, "backup": str(backup) if backup else None, "path": str(path)}


def health(path):
    values = parse_env(path)
    port = values.get("PORT", "8787")
    if not _positive_integer(port) or not 1 <= int(port) <= 65535:
        raise ValueError("Fields requiring configuration: PORT")
    deadline = time.monotonic() + 10
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=min(1, max(0.01, deadline - time.monotonic()))) as response:
                if response.status == 200 and response.read(1024).strip() == b"ok":
                    print("ok")
                    return True
        except (OSError, ValueError):
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.2, remaining))
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--template", required=True)
    prepare.add_argument("--env-file", required=True)
    for command in ("validate", "health"):
        commands.add_parser(command).add_argument("--env-file", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_env(args.template, args.env_file)
            print(f"Environment file: {result['path']}")
            print("Status: " + ("created" if result["created"] else "updated" if result["changed"] else "unchanged"))
            if result["backup"]:
                print(f"Backup: {result['backup']}")
            try:
                validate_env(args.env_file)
            except ValueError as error:
                print(str(error))
        elif args.command == "validate":
            validate_env(args.env_file)
            print(f"Environment file: {args.env_file}")
            print("Status: valid")
        elif not health(args.env_file):
            print("Health check failed", file=sys.stderr)
            return 1
        return 0
    except ValueError as error:
        print(str(error), file=sys.stderr)
    except OSError:
        print("Unable to access environment file", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
