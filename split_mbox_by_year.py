#!/usr/bin/env python3
"""Split mbox archives into per-year mbox files."""

from __future__ import annotations

import argparse
import email.utils
import mailbox
import re
import sys
from pathlib import Path


def message_year(message: mailbox.mboxMessage) -> str | None:
    date_header = message.get("Date")
    if date_header:
        try:
            parsed = email.utils.parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None and parsed.year:
            return f"{parsed.year:04d}"

    from_line = message.get_from()
    if from_line:
        match = re.search(r"\b(\d{4})$", from_line)
        if match:
            return match.group(1)

    return None


def split_mbox(source: Path, output_dir: Path) -> tuple[int, dict[str, int], int]:
    destination_dir = output_dir / source.stem
    destination_dir.mkdir(parents=True, exist_ok=True)

    source_mbox = mailbox.mbox(source)
    destinations: dict[str, mailbox.mbox] = {}
    counts: dict[str, int] = {}
    unknown_count = 0
    total_count = 0

    try:
        for message in source_mbox:
            total_count += 1
            year = message_year(message)
            if year is None:
                year = "unknown"
                unknown_count += 1

            destination = destinations.get(year)
            if destination is None:
                destination = mailbox.mbox(destination_dir / f"{year}.mbox")
                destinations[year] = destination

            destination.add(message)
            counts[year] = counts.get(year, 0) + 1
    finally:
        source_mbox.close()
        for destination in destinations.values():
            destination.flush()
            destination.close()

    return total_count, counts, unknown_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Split one or more mbox files into per-year mbox files."
    )
    parser.add_argument("mbox", nargs="+", type=Path, help="mbox file to split")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("split-by-year"),
        help="directory for generated per-year mbox files (default: split-by-year)",
    )
    args = parser.parse_args()

    failed = False
    for source in args.mbox:
        if not source.is_file():
            print(f"{source}: not a file", file=sys.stderr)
            failed = True
            continue

        total_count, counts, unknown_count = split_mbox(source, args.output_dir)
        years = ", ".join(f"{year}: {counts[year]}" for year in sorted(counts))
        print(f"{source}: {total_count} messages -> {args.output_dir / source.stem}")
        print(f"  {years}")
        if unknown_count:
            print(f"  warning: {unknown_count} messages written to unknown.mbox")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
