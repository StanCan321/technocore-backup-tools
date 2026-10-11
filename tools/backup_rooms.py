#!/usr/bin/env python3
"""Read-only JSONL backups for retained Technocore room exports."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_BASE_URL = "https://technocore.chat"
MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_HEAD_BYTES = 256 * 1024
REQUEST_TIMEOUT_SECONDS = 20
ROOM_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
WORKSPACE = Path(__file__).resolve().parents[1]
OUTPUTS_DIR = WORKSPACE / "outputs"


class BackupError(Exception):
    """An expected backup failure that can be shown to the user."""


class _RejectRedirects(HTTPRedirectHandler):
    """Exports must remain a read of the approved host and route."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def validate_room(room: str) -> str:
    if not ROOM_NAME.fullmatch(room):
        raise BackupError("Invalid room name: " + repr(room))
    return room


def validate_base_url(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise BackupError("Invalid server URL.") from exc
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BackupError("Invalid server URL.")
    if parsed.path not in ("", "/"):
        raise BackupError("The server URL must not contain a path.")
    host = parsed.hostname
    if not host:
        raise BackupError("The server URL has no host.")
    if parsed.scheme == "https" and host.lower() == "technocore.chat":
        if port not in (None, 443):
            raise BackupError("The official server must use the default HTTPS port.")
        return DEFAULT_BASE_URL
    if parsed.scheme == "http" and _is_loopback(host):
        if port is None:
            raise BackupError("A loopback practice server needs an explicit port.")
        return base_url.rstrip("/")
    raise BackupError("Use the official HTTPS server or a loopback HTTP practice server.")


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_backup_root(backup_root: Path, outputs_dir: Path = OUTPUTS_DIR) -> Path:
    """Allow backup folders only below this workspace's outputs directory."""
    root = backup_root.resolve()
    allowed = outputs_dir.resolve()
    try:
        root.relative_to(allowed)
    except ValueError as exc:
        raise BackupError(
            "The backup location must be inside this workspace's outputs directory."
        ) from exc
    if root == allowed:
        raise BackupError("Choose a dedicated folder inside outputs for backups.")
    return root


def make_backup_directory(backup_root: Path) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True)
    # Microseconds make ordinary runs distinct; the suffix also handles a collision.
    stem = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    for number in range(1000):
        name = stem if number == 0 else f"{stem}-{number:03d}"
        candidate = backup_root / name
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise BackupError("Could not create a unique backup folder; try again later.")


def _parse_export(data: bytes) -> tuple[int, int | None, int | None]:
    if not data:
        return 0, None, None
    if not data.endswith(b"\n"):
        raise BackupError("The export ends with an incomplete line.")
    first_seq: int | None = None
    last_seq: int | None = None
    count = 0
    for line_number, line in enumerate(data.splitlines(), start=1):
        if not line:
            raise BackupError(f"Export line {line_number} is empty.")
        try:
            record = json.loads(line.decode("utf-8"), parse_int=int)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError(f"Export line {line_number} is not valid JSON.") from exc
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("seq"), int)
            or isinstance(record["seq"], bool)
        ):
            raise BackupError(f"Export line {line_number} has no integer seq.")
        seq = record["seq"]
        if seq < 0 or (last_seq is not None and seq != last_seq + 1):
            raise BackupError(f"Export line {line_number} is not the next contiguous sequence.")
        if first_seq is None:
            first_seq = seq
        last_seq = seq
        count += 1
    return count, first_seq, last_seq


def _read_head(base_url: str, room: str) -> tuple[int, str]:
    query = urlencode({"format": "json", "limit": 1, "n": time.time_ns()})
    url = f"{base_url}/r/{quote(room, safe='')}?{query}"
    request = Request(
        url, headers={"Accept": "application/json", "User-Agent": "technocore-room-backup/1"}
    )
    try:
        opener = build_opener(_RejectRedirects())
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise BackupError(f"The server returned HTTP {response.status}.")
            data = response.read(MAX_HEAD_BYTES + 1)
            if len(data) > MAX_HEAD_BYTES:
                raise BackupError("The room-head response exceeds 256 KiB.")
    except HTTPError as exc:
        raise BackupError(f"The server returned HTTP {exc.code}.") from exc
    except (HTTPException, URLError, TimeoutError, OSError) as exc:
        detail = exc.reason if isinstance(exc, URLError) else exc
        raise BackupError(f"Could not read the room head: {detail}") from exc
    try:
        view = json.loads(data.decode("utf-8"), parse_int=int)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("The room-head response is not valid JSON.") from exc
    last_seq = view.get("last_seq") if isinstance(view, dict) else None
    generation = view.get("generation") if isinstance(view, dict) else None
    if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0:
        raise BackupError("The room-head response has no non-negative integer last_seq.")
    if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0:
        generation = str(generation)
    if not isinstance(generation, str) or not generation:
        raise BackupError("The room-head response has no generation.")
    return last_seq, generation


def _read_export(url: str) -> tuple[bytes, str]:
    request = Request(
        url,
        headers={
            "Accept": "application/x-ndjson",
            "User-Agent": "technocore-room-backup/1",
        },
    )
    try:
        opener = build_opener(_RejectRedirects())
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise BackupError(f"The server returned HTTP {response.status}.")
            content_length = response.headers.get("Content-Length")
            if (
                content_length
                and content_length.isdigit()
                and int(content_length) > MAX_EXPORT_BYTES
            ):
                raise BackupError("The export exceeds the 16 MiB client limit.")
            generation = response.headers.get("X-Room-Generation")
            if generation is None:
                raise BackupError("The export has no X-Room-Generation header.")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_EXPORT_BYTES:
                    raise BackupError("The export exceeds the 16 MiB client limit.")
                chunks.append(chunk)
            data = b"".join(chunks)
            if content_length and content_length.isdigit() and len(data) != int(content_length):
                raise BackupError("The export ended before its declared Content-Length.")
            return data, generation
    except HTTPError as exc:
        raise BackupError(f"The server returned HTTP {exc.code}.") from exc
    except (HTTPException, URLError, TimeoutError, OSError) as exc:
        raise BackupError(
            f"Could not read the export: {exc.reason if isinstance(exc, URLError) else exc}"
        ) from exc


def _atomic_write(path: Path, data: bytes) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def backup_rooms(
    rooms: tuple[str, ...],
    base_url: str = DEFAULT_BASE_URL,
    backup_root: Path | None = None,
    *,
    outputs_dir: Path = OUTPUTS_DIR,
) -> tuple[Path, list[dict[str, Any]]]:
    """Back up rooms and return the created folder plus every result record."""
    base_url = validate_base_url(base_url)
    checked_rooms = tuple(validate_room(room) for room in rooms)
    if not checked_rooms:
        raise BackupError("At least one --room is required.")
    if len(set(checked_rooms)) != len(checked_rooms):
        raise BackupError("A room may be selected only once.")
    root = validate_backup_root(backup_root or outputs_dir / "backups", outputs_dir)
    destination = make_backup_directory(root)
    results: list[dict[str, Any]] = []
    for room in checked_rooms:
        source_url = f"{base_url}/r/{quote(room, safe='')}/export"
        fetched_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            head_seq, head_generation = _read_head(base_url, room)
            data, generation = _read_export(source_url)
            if generation != head_generation:
                raise BackupError("The room generation changed between the head read and export.")
            record_count, first_seq, last_seq = _parse_export(data)
            if last_seq is None:
                if head_seq != 0:
                    raise BackupError("The export is empty but the room head is not.")
            elif last_seq < head_seq:
                raise BackupError("The export ended before the room head captured before download.")
            file_name = f"{room}.jsonl"
            _atomic_write(destination / file_name, data)
            results.append(
                {
                    "room": room,
                    "status": "success",
                    "file": file_name,
                    "source_url": source_url,
                    "fetched_at": fetched_at,
                    "generation": generation,
                    "head_seq_before_export": head_seq,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                    "record_count": record_count,
                    "first_seq": first_seq,
                    "last_seq": last_seq,
                }
            )
        except (BackupError, OSError) as exc:
            results.append(
                {
                    "room": room,
                    "status": "failed",
                    "source_url": source_url,
                    "fetched_at": fetched_at,
                    "error": str(exc),
                }
            )
    manifest = {
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "base_url": base_url,
        "read_only": True,
        "max_export_bytes": MAX_EXPORT_BYTES,
        "rooms": results,
    }
    _atomic_write(
        destination / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )
    return destination, results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Back up retained Technocore room exports without writing to the service."
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help="server URL (default: official service)"
    )
    parser.add_argument(
        "--room", action="append", required=True, help="room to back up (repeatable)"
    )
    parser.add_argument("--output-dir", type=Path, help="dedicated backup folder under outputs")
    args = parser.parse_args(argv)
    try:
        destination, results = backup_rooms(tuple(args.room), args.base_url, args.output_dir)
    except (BackupError, OSError) as exc:
        print("Could not start backup:", exc, file=sys.stderr)
        return 2
    succeeded = [item for item in results if item["status"] == "success"]
    failed = [item for item in results if item["status"] == "failed"]
    print(f"Backup folder: {destination}")
    for item in succeeded:
        print(f"Complete: {item['room']} ({item['record_count']} records)")
    for item in failed:
        print(f"Failed: {item['room']} — {item['error']}", file=sys.stderr)
    print("Manifest: manifest.json")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
