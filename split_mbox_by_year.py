#!/usr/bin/env python3
"""Split mbox archives into per-year mbox files.

Year files larger than the configured limit are split into per-month mbox files.
Large attachments are extracted to separate files and replaced with pointers.
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import mimetypes
import mailbox
import re
import shutil
import sys
from pathlib import Path


DEFAULT_MAX_YEAR_SIZE = 2 * 1024 * 1024
DEFAULT_EXTRACT_ATTACHMENT_SIZE = 300 * 1024
SIZE_RE = re.compile(r"^(\d+)([KMGT]?)$", re.IGNORECASE)
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def parse_size(value: str) -> int:
    match = SIZE_RE.match(value)
    if not match:
        raise argparse.ArgumentTypeError(
            "size must be an integer with optional K, M, G, or T suffix"
        )

    amount = int(match.group(1))
    suffix = match.group(2).upper()
    multiplier = {
        "": 1,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
    }[suffix]
    return amount * multiplier


def message_year_month(message: mailbox.mboxMessage) -> tuple[str | None, str | None]:
    date_header = message.get("Date")
    if date_header:
        try:
            parsed = email.utils.parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None and parsed.year:
            return f"{parsed.year:04d}", f"{parsed.month:02d}"

    from_line = message.get_from()
    if from_line:
        match = re.search(r"\b(\d{4})$", from_line)
        if match:
            return match.group(1), None

    return None, None


def clean_generated_output(destination_dir: Path) -> None:
    if not destination_dir.exists():
        return

    for generated_mbox in destination_dir.glob("*.mbox"):
        generated_mbox.unlink()

    attachments_dir = destination_dir / "attachments"
    if attachments_dir.exists():
        shutil.rmtree(attachments_dir)


def safe_filename(filename: str | None, content_type: str, payload: bytes) -> str:
    if filename:
        safe = SAFE_FILENAME_RE.sub("_", Path(filename).name).strip("._")
    else:
        digest = hashlib.sha256(payload).hexdigest()[:12]
        extension = mimetypes.guess_extension(content_type) or ".bin"
        safe = f"attachment-{digest}{extension}"

    return safe or "attachment.bin"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    for counter in range(2, 10000):
        candidate = path.with_name(f"{stem}-{counter}{suffix}")
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"could not find unique filename for {path}")


def extract_large_attachments(
    message: mailbox.mboxMessage,
    destination_dir: Path,
    message_index: int,
    extract_over_size: int,
) -> tuple[int, int]:
    if extract_over_size <= 0:
        return 0, 0

    year, month = message_year_month(message)
    period = year or "unknown"
    if year is not None and month is not None:
        period = f"{year}-{month}"
    elif year is not None:
        period = f"{year}-unknown"

    extracted_count = 0
    extracted_bytes = 0

    for part_index, part in enumerate(message.walk(), start=1):
        if part.is_multipart():
            continue

        payload = part.get_payload(decode=True)
        if payload is None or len(payload) <= extract_over_size:
            continue

        content_type = part.get_content_type()
        original_filename = part.get_filename()
        filename = safe_filename(original_filename, content_type, payload)
        attachment_dir = (
            destination_dir / "attachments" / period / f"message-{message_index:06d}"
        )
        attachment_dir.mkdir(parents=True, exist_ok=True)
        attachment_path = unique_path(attachment_dir / filename)
        attachment_path.write_bytes(payload)

        relative_path = attachment_path.relative_to(destination_dir)
        pointer = (
            "Attachment extracted from this mbox message.\n"
            f"Path: {relative_path}\n"
            f"Original filename: {original_filename or filename}\n"
            f"Original content type: {content_type}\n"
            f"Original decoded size: {len(payload)} bytes\n"
        )
        pointer_filename = f"{filename}.external.txt"

        for header in (
            "Content-Type",
            "Content-Transfer-Encoding",
            "Content-Disposition",
            "Content-ID",
        ):
            if header in part:
                del part[header]

        part.set_payload(pointer)
        part.set_type("text/plain")
        part.set_param("charset", "utf-8")
        part["Content-Transfer-Encoding"] = "7bit"
        part.add_header("Content-Disposition", "attachment", filename=pointer_filename)
        part["X-External-Attachment-Path"] = str(relative_path)
        part["X-External-Attachment-Content-Type"] = content_type
        part["X-External-Attachment-Size"] = str(len(payload))

        extracted_count += 1
        extracted_bytes += len(payload)

    return extracted_count, extracted_bytes


def split_large_year_file(
    year_file: Path, max_year_size: int
) -> tuple[dict[str, int], list[Path]]:
    if year_file.stat().st_size <= max_year_size:
        return {}, []

    year = year_file.stem
    source_mbox = mailbox.mbox(year_file)
    destinations: dict[str, mailbox.mbox] = {}
    counts: dict[str, int] = {}

    try:
        for message in source_mbox:
            _, month = message_year_month(message)
            suffix = month if month is not None else "unknown"
            period = f"{year}-{suffix}"

            destination = destinations.get(period)
            if destination is None:
                destination = mailbox.mbox(year_file.with_name(f"{period}.mbox"))
                destinations[period] = destination

            destination.add(message)
            counts[period] = counts.get(period, 0) + 1
    finally:
        source_mbox.close()
        for destination in destinations.values():
            destination.flush()
            destination.close()

    year_file.unlink()

    oversized_months = [
        year_file.with_name(f"{period}.mbox")
        for period in sorted(counts)
        if year_file.with_name(f"{period}.mbox").stat().st_size > max_year_size
    ]
    return counts, oversized_months


def split_mbox(
    source: Path, output_dir: Path, max_year_size: int, extract_attachment_size: int
) -> tuple[int, dict[str, int], dict[str, int], list[Path], int, int, int]:
    destination_dir = output_dir / source.stem
    destination_dir.mkdir(parents=True, exist_ok=True)
    clean_generated_output(destination_dir)

    source_mbox = mailbox.mbox(source)
    destinations: dict[str, mailbox.mbox] = {}
    counts: dict[str, int] = {}
    unknown_count = 0
    total_count = 0
    extracted_attachment_count = 0
    extracted_attachment_bytes = 0

    try:
        for message in source_mbox:
            total_count += 1
            year, _ = message_year_month(message)
            if year is None:
                year = "unknown"
                unknown_count += 1

            new_count, new_bytes = extract_large_attachments(
                message, destination_dir, total_count, extract_attachment_size
            )
            extracted_attachment_count += new_count
            extracted_attachment_bytes += new_bytes

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

    month_counts: dict[str, int] = {}
    oversized_months: list[Path] = []
    for year in sorted(counts):
        if year == "unknown":
            continue
        new_month_counts, new_oversized_months = split_large_year_file(
            destination_dir / f"{year}.mbox", max_year_size
        )
        month_counts.update(new_month_counts)
        oversized_months.extend(new_oversized_months)

    return (
        total_count,
        counts,
        month_counts,
        oversized_months,
        unknown_count,
        extracted_attachment_count,
        extracted_attachment_bytes,
    )


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
    parser.add_argument(
        "--max-year-size",
        type=parse_size,
        default=DEFAULT_MAX_YEAR_SIZE,
        help="split year files larger than this into month files (default: 2M)",
    )
    parser.add_argument(
        "--extract-attachments-over",
        type=parse_size,
        default=DEFAULT_EXTRACT_ATTACHMENT_SIZE,
        help=(
            "extract MIME parts larger than this to separate files; "
            "use 0 to disable (default: 300K)"
        ),
    )
    args = parser.parse_args()

    failed = False
    for source in args.mbox:
        if not source.is_file():
            print(f"{source}: not a file", file=sys.stderr)
            failed = True
            continue

        (
            total_count,
            counts,
            month_counts,
            oversized_months,
            unknown_count,
            extracted_attachment_count,
            extracted_attachment_bytes,
        ) = split_mbox(
            source,
            args.output_dir,
            args.max_year_size,
            args.extract_attachments_over,
        )
        years = ", ".join(f"{year}: {counts[year]}" for year in sorted(counts))
        print(f"{source}: {total_count} messages -> {args.output_dir / source.stem}")
        print(f"  {years}")
        if extracted_attachment_count:
            print(
                f"  extracted attachments: {extracted_attachment_count} "
                f"({extracted_attachment_bytes} bytes)"
            )
        if month_counts:
            months = ", ".join(
                f"{period}: {month_counts[period]}" for period in sorted(month_counts)
            )
            print(f"  split into months: {months}")
        for oversized_month in oversized_months:
            print(
                f"  warning: {oversized_month} is still larger than "
                f"{args.max_year_size} bytes after month split"
            )
        if unknown_count:
            print(f"  warning: {unknown_count} messages written to unknown.mbox")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
