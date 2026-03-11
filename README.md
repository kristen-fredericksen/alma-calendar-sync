# Alma Calendar Sync

Push centrally-managed holidays (exceptions) and semester dates (events) to CUNY Alma IZ calendars via the Open Hours API.

**Why this exists:** Alma's NZ-to-IZ calendar distribution is additive — it adds entries but never removes old ones. When a date changes, you end up with duplicates in every IZ. This script replaces NZ distribution with direct API updates that cleanly replace your entries without touching each campus's opening hours.

## How It Works

1. You define your events and exceptions in `calendar_entries.json`
2. The script reads the config and, for each IZ:
   - **GETs** the institution-level calendar
   - **Removes** existing entries that match your config entries (by date or description)
   - **Adds** the current versions of your entries
   - **Leaves opening hours untouched** (WEEK entries are never modified)
   - **PUTs** the updated calendar back
3. Dry-run by default — use `--apply` to commit changes

## Setup

### 1. Create a virtual environment

```bash
cd alma-calendar-sync
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Add API keys

Copy the example and fill in your keys:

```bash
cp api_keys.csv.example api_keys.csv
```

Each IZ needs an API key with **"Configuration - Production Read/Write"** permission.

### 3. Create your calendar config

Copy the example and edit your dates:

```bash
cp calendar_entries.json.example calendar_entries.json
```

See [Config File Format](#config-file-format) below.

## Usage

### Dry run (default) — see what would change

```bash
python3 src/sync_calendar.py
```

### Apply changes

```bash
python3 src/sync_calendar.py --apply
```

### Test with one IZ first

```bash
python3 src/sync_calendar.py --iz 01CUNY_QC
python3 src/sync_calendar.py --iz 01CUNY_QC --apply
```

### All options

```
--apply           Actually push changes (default: dry-run)
--iz CODE         Process only this IZ code (e.g., 01CUNY_QC)
--config PATH     Path to calendar_entries.json (default: ./calendar_entries.json)
--keys PATH       Path to api_keys.csv (default: ./api_keys.csv)
```

## Config File Format

`calendar_entries.json` has three sections:

### Groups

Define which IZs belong to which calendar group. An IZ can be in one group.

```json
"groups": {
  "default": {
    "description": "Most CUNY schools",
    "iz_codes": ["01CUNY_BB", "01CUNY_BC", ...]
  },
  "alt_calendar": {
    "description": "Guttman, Kingsborough, LaGuardia",
    "iz_codes": ["01CUNY_NC", "01CUNY_KB", "01CUNY_LG"]
  }
}
```

### Exceptions (holidays and closures)

```json
"exceptions": [
  {
    "desc": "Independence Day 2026",
    "from_date": "2026-07-04",
    "to_date": "2026-07-04",
    "status": "CLOSED",
    "applies_to": "all"
  }
]
```

- `applies_to`: `"all"` for every IZ, or a group name like `"default"` or `"alt_calendar"`
- `status`: Always `"CLOSED"` for full-day closures

### Events (semester dates)

```json
"events": [
  {
    "desc": "End of Spring Semester 2026",
    "from_date": "2026-06-16",
    "to_date": "2026-06-16",
    "applies_to": "default"
  }
]
```

Events are informational dates that appear on the calendar but don't affect open/closed status.

## How Matching Works

When the script decides which existing entries to remove, it uses two rules:

1. **Date overlap** — if an existing entry's dates overlap with any config entry's dates, it gets removed
2. **Description match** — if an existing entry has the same description as any config entry, it gets removed

This handles both cases:
- You change the name of a holiday → the old entry is caught by date overlap
- You move a date (e.g., semester end June 15 → June 16) → the old entry is caught by description match

Anything that doesn't match either rule is left alone (campus-managed entries, opening hours).

## Safety Features

- **Dry-run by default** — nothing changes unless you pass `--apply`
- **Backup before every PUT** — saved to `data/backups/`
- **Verification after PUT** — confirms WEEK entries weren't lost
- **Abort on error** — stops processing remaining IZs if one fails

## API Key Permissions

Each IZ needs an API key with: **Configuration - Production Read/Write**

Generate keys at: https://developers.exlibrisgroup.com/
