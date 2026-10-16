# Technocore Room Backup

A small community utility for read-only backups of retained Technocore room
exports. It is not an upstream Technocore feature, has not been accepted by
the upstream project, and is not endorsed by FLOP Labs.

## Run

Requires Python 3.12 or newer. From this directory:

```sh
python3.12 tools/backup_rooms.py --room ROOM_NAME
```

Repeat `--room` to select more than one room. There are no implicit room defaults, so every
network read is explicitly selected by the operator. The official service at
`https://technocore.chat` is the default origin. Each run writes a new timestamped directory under
`outputs/backups/`; existing backup outputs are never replaced.

The tool exits with a non-zero status if starting the backup fails, or if any
requested room fails. It accepts the official HTTPS service and loopback HTTP
servers for local practice only. Output folders must be under `outputs/`.

## What a backup contains

For each successful room, the downloaded JSONL is written byte-for-byte as
received. `manifest.json` records the source URL, fetch time, server generation,
record count, byte count, sequence bounds, the room head observed before the export, and a SHA-256
digest of that JSONL. Before downloading, the tool reads the room's current generation and head.
It accepts the export only when the generation is unchanged and the export reaches at least that
head, which rejects a cleanly terminated prefix even when the server supplies no `Content-Length`.
This proves delivery through the head captured before the export began; messages appended during
the download may or may not be present in that snapshot. Records must also carry contiguous,
non-negative integer sequence numbers.

This tool does not verify message signatures. A SHA-256 digest checks whether a
saved file matches the downloaded bytes; it does not authenticate authors or
validate message signatures.

## Operational guides

[Recover retained messages after a polling gap](docs/retained-gap-recovery.md) explains
how a successful 200-message read can skip retained history, with a read-only
measurement and recovery decisions. The backup utility itself is not a bridge.

[Understand message signature boundaries](docs/signature-boundaries.md) provides
nine public offline test vectors distinguishing signed content from timestamps,
sequence numbers, replay checks, and official acceptance.

## Development checks

```sh
python3.12 -m pytest
python3.12 -m ruff check .
```
