"""Shared CSV upload parsing for sites.py's and devices.py's bulk-import
routes. Both follow the same two-step shape: POST /import/preview parses
and validates a CSV and renders a per-row preview table (read-only, no DB
writes); POST /import/confirm re-receives the valid rows as resubmitted
hidden form fields and actually creates the records. Row-level validation
differs between sites and devices, so that logic lives in each route file —
this module only has the bit that's genuinely identical: turning an
uploaded file into a list of dicts keyed by canonical field name.
"""
import csv
import io


class CsvImportError(Exception):
    """A file-level problem (unreadable, missing a required column) —
    distinct from a per-row validation error, which the preview route
    collects and renders inline instead of raising."""
    pass


def parse_csv(file_storage, header_aliases, optional_aliases=None):
    """header_aliases: {canonical_name: [acceptable header strings,
    matched case-insensitively]}. optional_aliases: same shape, but a
    missing column just means every row gets "" for that canonical_name
    instead of raising — for a column like Circuits import's Parent,
    which is allowed to be absent from the file entirely. Returns a list
    of dicts keyed by canonical_name, values stripped. Raises
    CsvImportError for anything that makes the whole file unusable (bad
    encoding, empty file, a required column missing entirely)."""
    try:
        text = file_storage.read().decode("utf-8-sig")  # -sig: tolerate Excel's BOM
    except UnicodeDecodeError:
        raise CsvImportError("That file isn't valid UTF-8 text.")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CsvImportError("CSV file is empty.")

    normalized = {h.strip().lower(): h for h in reader.fieldnames if h}
    header_map = {}
    for canonical, aliases in header_aliases.items():
        actual = next((normalized[a] for a in aliases if a in normalized), None)
        if actual is None:
            raise CsvImportError(f"CSV is missing a required column: {aliases[0]}.")
        header_map[canonical] = actual
    optional_map = {
        canonical: next((normalized[a] for a in aliases if a in normalized), None)
        for canonical, aliases in (optional_aliases or {}).items()
    }

    rows = []
    for raw in reader:
        row = {canonical: (raw.get(actual) or "").strip() for canonical, actual in header_map.items()}
        for canonical, actual in optional_map.items():
            row[canonical] = (raw.get(actual) or "").strip() if actual else ""
        rows.append(row)
    if not rows:
        raise CsvImportError("CSV has no data rows.")
    return rows
