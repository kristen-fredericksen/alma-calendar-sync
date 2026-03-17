"""
sync_calendar.py — Push centrally-managed events and exceptions to CUNY Alma IZ calendars.

Reads a JSON config file defining holidays (exceptions) and semester dates (events),
then syncs them to each IZ's institution-level calendar via the Alma Open Hours API.
Campus-managed opening hours (WEEK entries) are never touched.

Dry-run by default. Use --apply to commit changes.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE = "https://api-na.hosted.exlibrisgroup.com"
OPEN_HOURS_ENDPOINT = "/almaws/v1/conf/open-hours"

# Entry types in the Alma calendar
TYPE_WEEK = "WEEK"
TYPE_EVENT = "EVENT"
TYPE_EXCEPTION = "EXCEPTION"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_api_keys(keys_file: Path, filter_izs: set[str] | None = None) -> list[dict[str, str]]:
    """Load IZ API keys from a CSV file.

    The CSV should have columns: iz, apikey
    If filter_izs is provided, only include IZs in that set.
    Returns a list of dicts like [{"iz": "01CUNY_QC", "apikey": "l8xx..."}].
    """
    keys: list[dict[str, str]] = []
    with open(keys_file, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            iz = row["iz"].strip()
            apikey = row["apikey"].strip()
            if not apikey:
                continue
            if filter_izs and iz not in filter_izs:
                continue
            keys.append({"iz": iz, "apikey": apikey})
    return keys


def load_calendar_config(config_file: Path) -> dict:
    """Load the calendar entries config file.

    Returns the parsed JSON with 'groups', 'exceptions', and 'events' keys.
    """
    with open(config_file) as f:
        config = json.load(f)

    # Validate required keys
    for key in ("groups", "exceptions", "events"):
        if key not in config:
            print(f"Error: config file missing required key '{key}'")
            sys.exit(1)

    return config


def get_entries_for_iz(config: dict, iz_code: str) -> list[dict]:
    """Return the list of config entries (events + exceptions) that apply to a given IZ.

    Each returned entry is a dict with keys: type, desc, from_date,
    and optionally to_date (for exceptions) and from_hour (for events like Hunter).
    """
    # Figure out which groups this IZ belongs to
    iz_groups: list[str] = []
    for group_name, group_info in config["groups"].items():
        if iz_code in group_info["iz_codes"]:
            iz_groups.append(group_name)

    if not iz_groups:
        return []

    entries: list[dict] = []

    # Collect exceptions that apply
    for exc in config["exceptions"]:
        applies_to = exc["applies_to"]
        if applies_to == "all" or applies_to in iz_groups:
            entries.append({
                "type": TYPE_EXCEPTION,
                "desc": exc["desc"],
                "from_date": exc["from_date"],
                "to_date": exc["to_date"],
            })

    # Collect events that apply
    for evt in config["events"]:
        applies_to = evt["applies_to"]
        if applies_to == "all" or applies_to in iz_groups:
            entry = {
                "type": TYPE_EVENT,
                "desc": evt["desc"],
                "from_date": evt["from_date"],
            }
            if "from_hour" in evt:
                entry["from_hour"] = evt["from_hour"]
            entries.append(entry)

    return entries


# ---------------------------------------------------------------------------
# Alma API communication
# ---------------------------------------------------------------------------

def alma_get(iz: str, apikey: str) -> dict:
    """GET the full institution-level open hours calendar for an IZ.

    The iz parameter is used for error messages only — the API key determines
    which institution's calendar is returned.
    """
    url = f"{API_BASE}{OPEN_HOURS_ENDPOINT}"
    params = {
        "apikey": apikey,
        "format": "json",
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            print(f"  401 Unauthorized — your Alma API key for {iz} may have expired.")
            print("  Renew it at: https://developers.exlibrisgroup.com/")
        if e.response is not None:
            print(f"  HTTP {e.response.status_code}: {e.response.text[:300]}")
        raise

    data = resp.json()

    # Check for Alma-level errors inside the response
    if data.get("errorsExist"):
        errors = data.get("errorList", {}).get("error", [])
        error_msgs = [e.get("errorMessage", "Unknown error") for e in errors]
        raise RuntimeError(f"Alma returned errors for {iz}: {'; '.join(error_msgs)}")

    return data


def alma_put(iz: str, apikey: str, data: dict) -> dict:
    """PUT the full calendar back to Alma, replacing all entries.

    The iz parameter is used for error messages only — the API key determines
    which institution's calendar is updated.
    """
    url = f"{API_BASE}{OPEN_HOURS_ENDPOINT}"
    params = {
        "apikey": apikey,
        "format": "json",
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        resp = requests.put(url, params=params, headers=headers,
                            json=data, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            print(f"  401 Unauthorized — your Alma API key for {iz} may have expired.")
            print("  Renew it at: https://developers.exlibrisgroup.com/")
        if e.response is not None:
            print(f"  HTTP {e.response.status_code}: {e.response.text[:300]}")
        raise

    result = resp.json()

    if result.get("errorsExist"):
        errors = result.get("errorList", {}).get("error", [])
        error_msgs = [e.get("errorMessage", "Unknown error") for e in errors]
        raise RuntimeError(f"Alma PUT errors for {iz}: {'; '.join(error_msgs)}")

    return result


# ---------------------------------------------------------------------------
# Backup and verification
# ---------------------------------------------------------------------------

def save_backup(iz: str, calendar_data: dict) -> Path:
    """Save the full calendar JSON to data/backups/ before making changes.

    Returns the path to the backup file.
    """
    backup_dir = Path("data/backups")
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{iz}_calendar_{timestamp}.json"
    backup_path = backup_dir / filename

    with open(backup_path, "w") as f:
        json.dump(calendar_data, f, indent=2)

    print(f"  Backup saved: {filename}")
    return backup_path


def verify_put(iz: str, apikey: str, week_count: int, backup_path: Path) -> bool:
    """Re-GET the calendar after PUT and verify we didn't lose WEEK entries.

    Args:
        iz: Institution code.
        apikey: API key for this IZ.
        week_count: Number of WEEK entries that should still be there.
        backup_path: Path to the backup file (reported if verification fails).

    Returns:
        True if verification passed, False if something looks wrong.
    """
    try:
        time.sleep(0.05)
        post_put = alma_get(iz, apikey)
    except Exception as e:
        print(f"  WARNING: Could not re-fetch calendar for verification: {e}")
        print(f"  Backup at: {backup_path}")
        return False

    entries = post_put.get("open_hour", [])
    post_week_count = sum(1 for e in entries
                         if e.get("type", {}).get("value") == TYPE_WEEK)

    if post_week_count < week_count:
        print(f"  ERROR: WEEK entries dropped from {week_count} to {post_week_count}!")
        print(f"  Something went wrong. Backup at: {backup_path}")
        return False

    total = len(entries)
    print(f"  Verified: {total} entries ({post_week_count} WEEK, "
          f"{total - post_week_count} events/exceptions)")
    return True


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def parse_date(date_str: str) -> date:
    """Parse a date string like '2026-07-04' or '2026-07-04Z' into a date object."""
    # Alma sometimes returns dates with a trailing 'Z'
    clean = date_str.rstrip("Z")
    return date.fromisoformat(clean)


def dates_overlap(from1: str, to1: str, from2: str, to2: str) -> bool:
    """Check if two date ranges overlap.

    Two ranges [from1, to1] and [from2, to2] overlap if
    from1 <= to2 AND from2 <= to1.
    """
    d_from1 = parse_date(from1)
    d_to1 = parse_date(to1)
    d_from2 = parse_date(from2)
    d_to2 = parse_date(to2)
    return d_from1 <= d_to2 and d_from2 <= d_to1


def should_remove(existing_entry: dict, config_entries: list[dict],
                  all_config_descs: set[str]) -> bool:
    """Decide whether an existing calendar entry should be removed.

    An entry is removed if it matches by:
    1. Description match — the existing entry has the same description as ANY config
       entry across ALL groups (not just this IZ's entries). This catches orphaned
       entries from other groups (e.g., "End of Enhanced Semester Due Date" in a
       default-group school).
    2. Date overlap — the existing entry's date range overlaps with a config entry
       that applies to this IZ.

    This two-pronged approach handles:
    - Same name, different date (e.g., moved semester end) → caught by description
    - Same date, different name → caught by date overlap
    - Orphaned entries from wrong group → caught by description
    """
    existing_desc = existing_entry.get("desc", "")
    existing_from = existing_entry.get("from_date", "")
    # Events may not have to_date; treat as same-day if missing
    existing_to = existing_entry.get("to_date", existing_from)

    # Match by description against ALL config entries (all groups)
    if existing_desc and existing_desc in all_config_descs:
        return True

    # Match by date overlap against this IZ's config entries
    if existing_from:
        for config_entry in config_entries:
            config_from = config_entry["from_date"]
            config_to = config_entry.get("to_date", config_from)
            if dates_overlap(existing_from, existing_to,
                             config_from, config_to):
                return True

    return False


# ---------------------------------------------------------------------------
# Build Alma-format entries from config
# ---------------------------------------------------------------------------

def config_entry_to_alma(entry: dict) -> dict:
    """Convert a config entry dict to the Alma open_hour JSON format.

    Alma expects specific fields depending on entry type:
    - EXCEPTION: type, desc, from_date, to_date, from_hour, to_hour, status (CLOSE not CLOSED)
    - EVENT: type, desc, from_date, status (OPEN), optionally from_hour, no to_date
    """
    if entry["type"] == TYPE_EXCEPTION:
        return {
            "type": {"value": TYPE_EXCEPTION, "desc": "Exception"},
            "inherited": False,
            "desc": entry["desc"],
            "from_date": entry["from_date"] + "Z",
            "to_date": entry["to_date"] + "Z",
            "from_hour": "00:00",
            "to_hour": "23:59",
            "status": {"value": "CLOSE", "desc": "Closed"},
        }
    else:
        # EVENT — optionally includes from_hour (e.g., Hunter needs 12:00)
        alma_entry = {
            "type": {"value": TYPE_EVENT, "desc": "Event"},
            "inherited": False,
            "desc": entry["desc"],
            "from_date": entry["from_date"] + "Z",
            "status": {"value": "OPEN", "desc": "Open"},
        }
        if "from_hour" in entry:
            alma_entry["from_hour"] = entry["from_hour"]
        return alma_entry


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def sync_iz(iz: str, apikey: str, config_entries: list[dict],
            all_config_descs: set[str], apply: bool) -> tuple[bool, bool]:
    """Sync calendar entries for one IZ.

    Args:
        iz: Institution code (e.g., "01CUNY_QC").
        apikey: API key for this IZ.
        config_entries: List of config entries that apply to this IZ.
        all_config_descs: Set of ALL config entry descriptions across all groups,
            used to catch orphaned entries from other groups.
        apply: If True, actually PUT changes. If False, dry-run only.

    Returns:
        (had_changes, had_error) tuple.
    """
    # Step 1: GET current calendar
    try:
        calendar = alma_get(iz, apikey)
    except Exception as e:
        print(f"  Error fetching calendar: {e}")
        return False, True

    time.sleep(0.05)  # Rate limiting

    existing_entries = calendar.get("open_hour", [])

    # Step 2: Separate WEEK entries (never touch) from events/exceptions
    week_entries: list[dict] = []
    other_entries: list[dict] = []

    for entry in existing_entries:
        entry_type = entry.get("type", {}).get("value", "")
        if entry_type == TYPE_WEEK:
            week_entries.append(entry)
        else:
            other_entries.append(entry)

    # Step 3: Decide which events/exceptions to keep vs. remove
    keep_entries: list[dict] = []
    remove_entries: list[dict] = []

    for entry in other_entries:
        if should_remove(entry, config_entries, all_config_descs):
            remove_entries.append(entry)
        else:
            keep_entries.append(entry)

    # Step 4: Build new Alma-format entries from config
    new_alma_entries = [config_entry_to_alma(e) for e in config_entries]

    # Step 5: Check if anything actually changed
    # If we're removing and re-adding the same entries, no PUT is needed.
    remove_descs = {e.get("desc", "") for e in remove_entries}
    add_descs = {e.get("desc", "") for e in new_alma_entries}
    if remove_descs == add_descs and len(remove_entries) == len(new_alma_entries):
        # Same descriptions — check if dates and hours also match
        remove_keys = {(e.get("desc"), e.get("from_date", "").rstrip("Z"),
                        e.get("from_hour", ""))
                       for e in remove_entries}
        add_keys = {(e.get("desc"), e.get("from_date", "").rstrip("Z"),
                     e.get("from_hour", ""))
                    for e in new_alma_entries}
        if remove_keys == add_keys:
            print("  Already in sync. No changes needed.")
            return False, False

    had_changes = bool(remove_entries) or bool(new_alma_entries)

    if remove_entries:
        print(f"  Removing {len(remove_entries)} entry(ies):")
        for entry in remove_entries:
            entry_type = entry.get("type", {}).get("value", "?")
            desc = entry.get("desc", "(no description)")
            from_d = entry.get("from_date", "?")
            to_d = entry.get("to_date", "?")
            print(f"    - [{entry_type}] {desc}  ({from_d} to {to_d})")

    if new_alma_entries:
        print(f"  Adding {len(new_alma_entries)} entry(ies):")
        for entry in new_alma_entries:
            entry_type = entry.get("type", {}).get("value", "?")
            desc = entry.get("desc", "(no description)")
            from_d = entry.get("from_date", "?")
            to_d = entry.get("to_date", "?")
            print(f"    + [{entry_type}] {desc}  ({from_d} to {to_d})")

    if not apply:
        return had_changes, False

    # Step 6: Build the new calendar and PUT it
    calendar["open_hour"] = week_entries + keep_entries + new_alma_entries

    backup_path = save_backup(iz, {"open_hour": existing_entries})

    try:
        alma_put(iz, apikey, calendar)
    except Exception as e:
        print(f"  Error during PUT: {e}")
        print(f"  Backup at: {backup_path}")
        return had_changes, True

    time.sleep(0.05)  # Rate limiting

    # Step 7: Verify
    ok = verify_put(iz, apikey, len(week_entries), backup_path)
    if not ok:
        return had_changes, True

    return had_changes, False


# ---------------------------------------------------------------------------
# CLI and main
# ---------------------------------------------------------------------------

def get_iz_display_name(iz_code: str) -> str:
    """Return a human-readable name for an IZ code."""
    names = {
        "01CUNY_AL": "CUNY Central Office",
        "01CUNY_BB": "Baruch College",
        "01CUNY_BC": "Brooklyn College",
        "01CUNY_BM": "Borough of Manhattan CC",
        "01CUNY_BX": "Bronx CC",
        "01CUNY_CC": "City College",
        "01CUNY_CL": "CUNY School of Law",
        "01CUNY_GC": "CUNY Graduate Center",
        "01CUNY_GJ": "Craig Newmark Journalism",
        "01CUNY_HC": "Hunter College",
        "01CUNY_HO": "Hostos CC",
        "01CUNY_JJ": "John Jay College",
        "01CUNY_KB": "Kingsborough CC",
        "01CUNY_LG": "LaGuardia CC",
        "01CUNY_LE": "Lehman College",
        "01CUNY_ME": "Medgar Evers College",
        "01CUNY_NC": "Guttman CC",
        "01CUNY_NETWORK": "Network Zone",
        "01CUNY_NY": "NYC College of Technology",
        "01CUNY_QB": "Queensborough CC",
        "01CUNY_QC": "Queens College",
        "01CUNY_SI": "College of Staten Island",
        "01CUNY_YC": "York College",
    }
    return names.get(iz_code, iz_code)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync centrally-managed events and exceptions to CUNY Alma IZ calendars."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually push changes to Alma (default: dry-run)"
    )
    parser.add_argument(
        "--iz", type=str, default=None,
        help="Process only these IZ codes, comma-separated "
        "(e.g., 01CUNY_QC or 01CUNY_KB,01CUNY_LG,01CUNY_NC). "
        "Default: all IZs in api_keys.csv.",
    )
    parser.add_argument(
        "--config", type=str, default="calendar_entries.json",
        help="Path to the calendar entries config file (default: calendar_entries.json)"
    )
    parser.add_argument(
        "--keys", type=str, default="api_keys.csv",
        help="Path to the API keys CSV file (default: api_keys.csv)"
    )
    args = parser.parse_args()

    # Load config and keys
    config_path = Path(args.config)
    keys_path = Path(args.keys)

    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        print("Copy calendar_entries.json.example to calendar_entries.json and fill in your dates.")
        sys.exit(1)

    if not keys_path.exists():
        print(f"Error: API keys file not found: {keys_path}")
        print("Copy api_keys.csv.example to api_keys.csv and add your API keys.")
        sys.exit(1)

    config = load_calendar_config(config_path)

    # Parse --iz flag: comma-separated list of IZ codes
    filter_izs = None
    if args.iz:
        filter_izs = {code.strip() for code in args.iz.split(",")}

    institutions = load_api_keys(keys_path, filter_izs=filter_izs)

    if not institutions:
        if filter_izs:
            print(f"Error: None of the specified IZ codes found in {keys_path}")
        else:
            print(f"Error: No API keys found in {keys_path}")
        sys.exit(1)

    # Filter out IZs that aren't in any group
    all_iz_codes: set[str] = set()
    for group_info in config["groups"].values():
        all_iz_codes.update(group_info["iz_codes"])

    institutions = [inst for inst in institutions if inst["iz"] in all_iz_codes]

    if not institutions:
        print("Error: None of the IZs in api_keys.csv appear in the config file groups.")
        sys.exit(1)

    # Print header
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"Mode: {mode}")
    print(f"Config: {config_path}")
    print(f"Institutions: {len(institutions)}")
    print(f"Exceptions defined: {len(config['exceptions'])}")
    print(f"Events defined: {len(config['events'])}")
    print()

    if not args.apply:
        print("This is a DRY RUN. No changes will be made.")
        print("Re-run with --apply to commit changes to Alma.")
        print()

    # Build set of ALL config descriptions (across all groups) for orphan detection
    all_config_descs: set[str] = set()
    for exc in config["exceptions"]:
        all_config_descs.add(exc["desc"])
    for evt in config["events"]:
        all_config_descs.add(evt["desc"])

    # Process each IZ
    change_count = 0
    error_count = 0
    abort = False

    for inst in institutions:
        iz = inst["iz"]
        apikey = inst["apikey"]
        display = get_iz_display_name(iz)

        print("=" * 60)
        print(f"  {iz}  ({display})")
        print("=" * 60)

        # Get the entries that apply to this IZ
        entries = get_entries_for_iz(config, iz)

        if not entries:
            print("  No config entries apply to this IZ. Skipping.")
            print()
            continue

        had_changes, had_error = sync_iz(iz, apikey, entries, all_config_descs,
                                         args.apply)

        if had_changes:
            change_count += 1
        if had_error:
            error_count += 1
            abort = True
            print(f"\n  ABORTING: Error at {iz}. Remaining IZs will not be processed.")
            break

        print()

    # Summary
    print("=" * 60)
    print(f"Done. {change_count} IZ(s) with changes, {error_count} error(s).")

    if not args.apply and change_count > 0:
        print("\nThis was a DRY RUN. Re-run with --apply to commit changes.")

    if abort:
        print("\nProcessing was aborted due to an error. Check output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
