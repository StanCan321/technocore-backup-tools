from __future__ import annotations

import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import backup_rooms


class LocalHTTPServer(HTTPServer):
    """Avoid the test machine's potentially slow DNS lookup for 127.0.0.1."""

    def server_bind(self) -> None:
        self.socket.bind(self.server_address)
        self.server_name = "127.0.0.1"
        self.server_port = self.socket.getsockname()[1]


@contextmanager
def fake_server(
    responses: dict[str, tuple[int, dict[str, str], bytes]],
    *,
    head_overrides: dict[str, tuple[int, str]] | None = None,
):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if query.get("format") == ["json"] and query.get("limit") == ["1"]:
                room = parsed.path.removeprefix("/r/")
                export_status, export_headers, export_body = responses.get(
                    f"/r/{room}/export", (404, {}, b"")
                )
                seqs = []
                for line in export_body.splitlines():
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict) and isinstance(record.get("seq"), int):
                        seqs.append(record["seq"])
                last_seq, generation = (head_overrides or {}).get(
                    room,
                    (
                        max(seqs, default=0),
                        export_headers.get("X-Room-Generation", "1"),
                    ),
                )
                json_generation = int(generation) if generation.isdigit() else generation
                status, headers, body = (
                    200 if export_status != 404 else 404,
                    {"Content-Type": "application/json"},
                    json.dumps(
                        {"last_seq": last_seq, "generation": json_generation, "messages": []}
                    ).encode(),
                )
            else:
                status, headers, body = responses.get(parsed.path, (404, {}, b"missing"))
            self.send_response(status)
            if "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = LocalHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def backup_root(tmp_path: Path) -> tuple[Path, Path]:
    outputs = tmp_path / "outputs"
    return outputs / "backups", outputs


def test_backup_preserves_exact_jsonl_and_large_nonce(tmp_path: Path) -> None:
    body = (
        b'{"seq":1,"from":"a","text":"first"}\n'
        b'{"seq":2,"nonce":9999999999999999999,"text":"second"}\n'
    )
    responses = {"/r/dev/export": (200, {"X-Room-Generation": "epoch-7"}, body)}
    root, outputs = backup_root(tmp_path)
    with fake_server(responses) as base_url:
        destination, results = backup_rooms.backup_rooms(
            ("dev",), base_url, root, outputs_dir=outputs
        )

    assert (destination / "dev.jsonl").read_bytes() == body
    manifest = json.loads((destination / "manifest.json").read_text())
    result = results[0]
    assert result["status"] == "success"
    assert result["record_count"] == 2
    assert result["first_seq"] == 1
    assert result["last_seq"] == 2
    assert manifest["rooms"][0]["generation"] == "epoch-7"
    assert json.loads(body.splitlines()[1])["nonce"] == 9999999999999999999


def test_http_failure_is_recorded_without_a_data_file(tmp_path: Path) -> None:
    root, outputs = backup_root(tmp_path)
    with fake_server({"/r/dev/export": (503, {}, b"down")}) as base_url:
        destination, results = backup_rooms.backup_rooms(
            ("dev",), base_url, root, outputs_dir=outputs
        )

    assert results[0]["status"] == "failed"
    assert "HTTP 503" in results[0]["error"]
    assert not (destination / "dev.jsonl").exists()
    assert json.loads((destination / "manifest.json").read_text())["rooms"][0]["status"] == "failed"


def test_redirect_is_rejected_before_it_can_reach_another_route(tmp_path: Path) -> None:
    root, outputs = backup_root(tmp_path)
    responses = {
        "/r/dev/export": (302, {"Location": "/r/dev/say/backup/never-follow"}, b""),
        "/r/dev/say/backup/never-follow": (
            200,
            {"X-Room-Generation": "bad"},
            b'{"seq":1}\n',
        ),
    }
    with fake_server(responses) as base_url:
        destination, results = backup_rooms.backup_rooms(
            ("dev",), base_url, root, outputs_dir=outputs
        )

    assert results[0]["status"] == "failed"
    assert "HTTP 302" in results[0]["error"]
    assert not (destination / "dev.jsonl").exists()


@pytest.mark.parametrize(
    "body",
    [b'{"seq":1}\nnot-json\n', b'{"seq":1}', b'{"seq":"one"}\n'],
)
def test_invalid_jsonl_is_not_saved(tmp_path: Path, body: bytes) -> None:
    root, outputs = backup_root(tmp_path)
    headers = {"X-Room-Generation": "1"}
    with fake_server({"/r/dev/export": (200, headers, body)}) as base_url:
        destination, results = backup_rooms.backup_rooms(
            ("dev",), base_url, root, outputs_dir=outputs
        )

    assert results[0]["status"] == "failed"
    assert not (destination / "dev.jsonl").exists()


def test_blank_export_is_saved_as_an_empty_valid_backup(tmp_path: Path) -> None:
    root, outputs = backup_root(tmp_path)
    with fake_server({"/r/dev/export": (200, {"X-Room-Generation": "1"}, b"")}) as base_url:
        destination, results = backup_rooms.backup_rooms(
            ("dev",), base_url, root, outputs_dir=outputs
        )

    assert results[0]["status"] == "success"
    assert results[0]["record_count"] == 0
    assert (destination / "dev.jsonl").read_bytes() == b""


def test_truncated_content_length_is_not_saved(tmp_path: Path) -> None:
    root, outputs = backup_root(tmp_path)
    headers = {"X-Room-Generation": "1", "Content-Length": "99"}
    with fake_server({"/r/dev/export": (200, headers, b'{"seq":1}\n')}) as base_url:
        destination, results = backup_rooms.backup_rooms(
            ("dev",), base_url, root, outputs_dir=outputs
        )

    assert results[0]["status"] == "failed"
    assert not (destination / "dev.jsonl").exists()


def test_repeated_backups_use_different_folders(tmp_path: Path) -> None:
    root, outputs = backup_root(tmp_path)
    response = {"/r/dev/export": (200, {"X-Room-Generation": "1"}, b'{"seq":1}\n')}
    with fake_server(response) as base_url:
        first, _ = backup_rooms.backup_rooms(("dev",), base_url, root, outputs_dir=outputs)
        second, _ = backup_rooms.backup_rooms(("dev",), base_url, root, outputs_dir=outputs)

    assert first != second
    assert (first / "dev.jsonl").read_bytes() == (second / "dev.jsonl").read_bytes()


def test_duplicate_room_is_rejected_before_creating_a_folder(tmp_path: Path) -> None:
    root, outputs = backup_root(tmp_path)
    with pytest.raises(backup_rooms.BackupError, match="only once"):
        backup_rooms.backup_rooms(("dev", "dev"), backup_root=root, outputs_dir=outputs)
    assert not root.exists()


def test_data_file_storage_failure_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outputs = backup_root(tmp_path)
    original_write = backup_rooms._atomic_write

    def fail_data_file(path: Path, data: bytes) -> None:
        if path.name == "dev.jsonl":
            raise OSError("disk full")
        original_write(path, data)

    monkeypatch.setattr(backup_rooms, "_atomic_write", fail_data_file)
    response = {"/r/dev/export": (200, {"X-Room-Generation": "1"}, b'{"seq":1}\n')}
    with fake_server(response) as base_url:
        destination, results = backup_rooms.backup_rooms(
            ("dev",), base_url, root, outputs_dir=outputs
        )

    assert results[0]["status"] == "failed"
    assert "disk full" in results[0]["error"]
    assert not (destination / "dev.jsonl").exists()


@pytest.mark.parametrize("room", ["../escape", "Bad", "a" * 49])
def test_invalid_room_is_rejected(room: str) -> None:
    with pytest.raises(backup_rooms.BackupError):
        backup_rooms.validate_room(room)


def test_only_official_or_loopback_server_and_safe_output_are_allowed(
    tmp_path: Path,
) -> None:
    assert backup_rooms.validate_base_url("https://technocore.chat/") == "https://technocore.chat"
    with pytest.raises(backup_rooms.BackupError):
        backup_rooms.validate_base_url("http://example.test:8080")
    with pytest.raises(backup_rooms.BackupError):
        backup_rooms.validate_base_url("http://127.0.0.1:not-a-port")
    with pytest.raises(backup_rooms.BackupError):
        backup_rooms.validate_backup_root(tmp_path / "outside", tmp_path / "outputs")


def test_clean_export_prefix_is_rejected_against_preflight_head(tmp_path: Path) -> None:
    root, outputs = backup_root(tmp_path)
    response = {"/r/dev/export": (200, {"X-Room-Generation": "1"}, b'{"seq":1}\n')}
    with fake_server(response, head_overrides={"dev": (2, "1")}) as base_url:
        destination, results = backup_rooms.backup_rooms(
            ("dev",), base_url, root, outputs_dir=outputs
        )

    assert results[0]["status"] == "failed"
    assert "ended before the room head" in results[0]["error"]
    assert not (destination / "dev.jsonl").exists()


def test_generation_change_during_export_is_rejected(tmp_path: Path) -> None:
    root, outputs = backup_root(tmp_path)
    response = {"/r/dev/export": (200, {"X-Room-Generation": "2"}, b'{"seq":1}\n')}
    with fake_server(response, head_overrides={"dev": (0, "1")}) as base_url:
        destination, results = backup_rooms.backup_rooms(
            ("dev",), base_url, root, outputs_dir=outputs
        )

    assert results[0]["status"] == "failed"
    assert "generation changed" in results[0]["error"]
    assert not (destination / "dev.jsonl").exists()


@pytest.mark.parametrize(
    "body",
    [
        b'{"seq":1}\n{"seq":3}\n',
        b'{"seq":2}\n{"seq":2}\n',
        b'{"seq":-1}\n',
    ],
)
def test_noncontiguous_or_negative_sequences_are_rejected(tmp_path: Path, body: bytes) -> None:
    root, outputs = backup_root(tmp_path)
    response = {"/r/dev/export": (200, {"X-Room-Generation": "1"}, body)}
    with fake_server(response, head_overrides={"dev": (0, "1")}) as base_url:
        destination, results = backup_rooms.backup_rooms(
            ("dev",), base_url, root, outputs_dir=outputs
        )

    assert results[0]["status"] == "failed"
    assert "contiguous sequence" in results[0]["error"]
    assert not (destination / "dev.jsonl").exists()
