"""Team codes: one per registered team, handed out by the organisers, checked at ingestion."""

import secrets
from pathlib import Path

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I: readable on paper


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def add_codes(event_file: Path, count: int, existing: list[str]) -> list[str]:
    """Append `count` fresh codes under `team_codes:` in the (gitignored) event file, keeping the rest
    of the file, comments included, as it was."""
    taken, codes = set(existing), list[str]()
    while len(codes) < count:
        if (c := new_code()) not in taken:
            taken.add(c)
            codes.append(c)
    text = event_file.read_text()
    items = "".join(f"  - {c}\n" for c in codes)
    if "\nteam_codes:" in "\n" + text:
        lines = text.splitlines(keepends=True)
        at = next(i for i, ln in enumerate(lines) if ln.startswith("team_codes:"))
        if lines[at].strip() != "team_codes:":  # e.g. `team_codes: []`
            lines[at] = "team_codes:\n"
        end = at + 1
        while end < len(lines) and lines[end].startswith("  - "):
            end += 1
        text = "".join(lines[:end]) + items + "".join(lines[end:])
    else:
        text = (
            text.rstrip("\n") + "\n# Team codes (judgebench team-codes). Keep this file private.\nteam_codes:\n" + items
        )
    event_file.write_text(text)
    return codes
