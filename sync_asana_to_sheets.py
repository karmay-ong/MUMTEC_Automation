#!/usr/bin/env python3
"""
Asana -> Google Sheets sync for club financial requests (one-way:
Asana -> Sheet only; the sheet never writes back to Asana).

What it does:
  1. Fetches all tasks from a specific Asana project.
  2. Parses each task's custom fields AND its description text (Name,
     Vendor Name, Total Requested Amount, etc.).
  3. Works out Expense Type, Submission Status, Claimant/Payee, and which
     club's amount column to fill, following the club's rules.
  4. For tasks not yet in the sheet: appends a new row.
  5. For tasks already in the sheet: recomputes the row and updates it
     in place if anything changed (e.g. Submission Status progressed,
     an invoice got attached). Untouched rows are left alone.
  6. Re-sorts the sheet by "Date Added" (oldest first) when done.

Every row is tracked via a hidden "Task GID" column added after
"Total (MYR)" -- don't delete or edit that column.

For "automatic" syncing, run this on a schedule (every few minutes) via
cron / Task Scheduler / any host -- see README.md. That gives near
real-time updates without needing to host an always-on server. (True
instant push would require a public webhook server, which is a
different, heavier project -- ask if you want that instead.)

Setup: see README.md
"""

import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import config


# ---------------------------------------------------------------------------
# Asana helpers
# ---------------------------------------------------------------------------

ASANA_API_BASE = "https://app.asana.com/api/1.0"


def asana_headers():
    return {"Authorization": f"Bearer {config.ASANA_ACCESS_TOKEN}"}


TASK_OPT_FIELDS = [
    "name",
    "notes",
    "created_at",
    "permalink_url",
    "custom_fields.name",
    "custom_fields.display_value",
    "memberships.project",
]


def fetch_asana_tasks(project_gid):
    """Fetch all tasks in a project, including custom fields and notes."""
    tasks = []
    url = f"{ASANA_API_BASE}/projects/{project_gid}/tasks"
    params = {"opt_fields": ",".join(TASK_OPT_FIELDS), "limit": 100}

    while url:
        resp = requests.get(url, headers=asana_headers(), params=params)
        resp.raise_for_status()
        payload = resp.json()
        tasks.extend(payload.get("data", []))

        next_page = payload.get("next_page")
        if next_page and next_page.get("uri"):
            url = next_page["uri"]
            params = None  # next_page URI already includes query params
        else:
            url = None

    return tasks


def fetch_single_task(task_gid):
    """Fetch one task by GID, with the same fields fetch_asana_tasks uses."""
    url = f"{ASANA_API_BASE}/tasks/{task_gid}"
    resp = requests.get(url, headers=asana_headers(), params={"opt_fields": ",".join(TASK_OPT_FIELDS)})
    resp.raise_for_status()
    return resp.json().get("data")


def task_in_target_project(task):
    """True if the task belongs to config.ASANA_PROJECT_GID (webhooks can
    fire for tasks in other projects too, if the workspace has multiple)."""
    for membership in task.get("memberships", []) or []:
        project = membership.get("project") or {}
        if project.get("gid") == config.ASANA_PROJECT_GID:
            return True
    return False


def fetch_attachment_count(task_gid):
    """Returns the number of attachments on a task."""
    url = f"{ASANA_API_BASE}/tasks/{task_gid}/attachments"
    resp = requests.get(url, headers=asana_headers(), params={"limit": 1})
    resp.raise_for_status()
    # We only need to know if there's at least one; Asana returns up to
    # `limit` items, so len() here is 0 or 1.
    return len(resp.json().get("data", []))


def custom_field_map(task):
    """Turn Asana's custom_fields list into a simple {name: display_value} dict."""
    result = {}
    for field in task.get("custom_fields", []) or []:
        result[field.get("name", "")] = field.get("display_value", "") or ""
    return result


# ---------------------------------------------------------------------------
# Description parsing
# ---------------------------------------------------------------------------

_LABEL_RE = re.compile(r"^(.{1,60}):\s*$")


def parse_description(notes):
    """
    Parses Asana task descriptions of the form:

        Label:
        Value

        Label2:
        Value2

    into a dict {"Label": "Value", "Label2": "Value2"}. Tolerant of blank
    lines (including the invisible \xa0 lines Asana sometimes inserts).
    """
    data = {}
    current_label = None
    buffer = []

    for raw_line in (notes or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.strip().replace("\xa0", "")
        match = _LABEL_RE.match(line) if line else None
        if match:
            if current_label is not None:
                data[current_label] = " ".join(buffer).strip()
            current_label = match.group(1).strip()
            buffer = []
        elif line and current_label is not None and not buffer:
            # Values in this form are always a single line directly under
            # the label -- capture only the first line so we don't bleed
            # into unrelated footer text (e.g. the "submitted through..."
            # signature block, which has no label of its own).
            buffer.append(line)

    if current_label is not None:
        data[current_label] = " ".join(buffer).strip()

    return data


def parse_amount(text):
    """Extracts a numeric amount from a string like 'RM 1,989.20' -> 1989.20."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def strip_task_name_prefix(name):
    """
    '[2026, MUMTEC x MUMEC] SLN Bootcamp- Food 2' -> 'SLN Bootcamp- Food 2'
    Falls back to the full name if there's no leading [...] block.
    """
    return re.sub(r"^\[[^\]]*\]\s*", "", name or "").strip() or name


def sheets_escape(text):
    """Escape double quotes for embedding inside a Sheets formula string."""
    return (text or "").replace('"', "'")


# ---------------------------------------------------------------------------
# Business logic: turning one Asana task into one sheet row
# ---------------------------------------------------------------------------


def determine_expense_type(nature_of_request):
    normalized = (nature_of_request or "").strip().lower()
    if not normalized:
        return config.DEFAULT_EXPENSE_TYPE_BUCKET
    # Substring match (not exact) so variants like "Vouchers", "Gift Voucher",
    # "Merchandise Purchase" etc. still bucket correctly. Longer keys are
    # checked first so more specific phrases win over shorter ones.
    for key in sorted(config.EXPENSE_TYPE_BUCKETS, key=len, reverse=True):
        if key in normalized:
            return config.EXPENSE_TYPE_BUCKETS[key]
    return config.DEFAULT_EXPENSE_TYPE_BUCKET


def determine_submission_no(expense_type, pr_number, po_number, znp_number):
    if expense_type == "PR/PO":
        parts = [p for p in (pr_number, po_number) if p]
        return "/".join(parts)  # "PR123/PO456", or just "PR123" if PO not raised yet, or "" if neither
    if expense_type == "Claims":
        return pr_number
    if expense_type == "Debit Note":
        return "N/A"
    # Others: no PR/PO/claim number applies -- defaulting to N/A like Debit Note.
    return "N/A"


def determine_status(expense_type, pr_number, po_number, znp_number, task_progress):
    normalized_progress = (task_progress or "").strip().lower()

    # Rejected/Cancelled overrides everything else, regardless of expense
    # type -- kept as a distinct status in case you use it in your own
    # sheet formulas (e.g. excluding these from your own Balance calc).
    for keyword, label in config.TASK_PROGRESS_TERMINAL_STATUSES.items():
        if keyword in normalized_progress:
            return label

    completed = normalized_progress == config.TASK_PROGRESS_COMPLETED_VALUE.lower()

    if expense_type == "PR/PO":
        if znp_number:
            return "PO Closed"
        if po_number:
            return "PO Released"
        if pr_number:
            return "PR Raised"
        return "PR Not Raised"

    if expense_type == "Claims":
        if completed:
            return "Approved"
        if pr_number:
            return "Waiting Approval"
        return "To be submitted"

    # Debit Note, Others (covers Voucher / Merchandise)
    return "Approved" if completed else "Waiting Approval"


def determine_claimant(expense_type, desc):
    if expense_type == "PR/PO":
        return desc.get("Vendor Name", "")
    if expense_type == "Claims":
        return desc.get("Name", "")
    if expense_type == "Debit Note":
        return "Monash"
    # Others: best guess, flag for manual review
    return desc.get("Name", "")


def match_club_column(club_value):
    """Case/whitespace-insensitive match against CLUB_COLUMNS, checking
    CLUB_ALIASES first (e.g. Asana's "MUSA" or "SOIT" both -> sheet's
    combined "MUSA SOIT" column)."""
    if not club_value:
        return None
    normalized = club_value.strip().lower()

    if normalized in config.CLUB_ALIASES:
        return config.CLUB_ALIASES[normalized]

    for club in config.CLUB_COLUMNS:
        if club.strip().lower() == normalized:
            return club
    return None


def task_to_row(task, attachment_count=None):
    fields = custom_field_map(task)
    desc = parse_description(task.get("notes", ""))

    nature_of_request = fields.get(config.FIELD_NATURE_OF_REQUEST, "")
    expense_type = determine_expense_type(nature_of_request)

    pr_number = fields.get(config.FIELD_PR_NUMBER, "").strip()
    po_number = fields.get(config.FIELD_PO_NUMBER, "").strip()
    znp_number = fields.get(config.FIELD_ZNP_NUMBER, "").strip()
    task_progress = fields.get(config.FIELD_TASK_PROGRESS, "")

    submission_no = determine_submission_no(expense_type, pr_number, po_number, znp_number)
    status = determine_status(expense_type, pr_number, po_number, znp_number, task_progress)
    claimant = determine_claimant(expense_type, desc)

    if expense_type != "PR/PO":
        do_invoice = "N/A"
    elif config.CHECK_ATTACHMENTS and attachment_count is not None:
        do_invoice = "Yes" if attachment_count > 0 else "No"
    else:
        do_invoice = ""  # left blank for manual entry

    display_name = strip_task_name_prefix(task.get("name", ""))
    url = task.get("permalink_url", "")
    details_of_purchase = f'=HYPERLINK("{url}", "{sheets_escape(display_name)}")' if url else display_name

    amount = parse_amount(desc.get("Total Requested Amount", ""))
    club_raw = fields.get(config.FIELD_CLUB, "") or desc.get("Club/Team", "")
    matched_club = match_club_column(club_raw)

    created_at = task.get("created_at", "")
    date_added = ""
    if created_at:
        try:
            date_added = datetime.fromisoformat(created_at.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except ValueError:
            date_added = created_at

    row = {
        "Date Added": date_added,
        "Expense Type": expense_type,
        "PRPO/SAP Submission No.": submission_no,
        "Submission Status": status,
        "DO & Invoice received?": do_invoice,
        "Claimant/Payee": claimant,
        "Details of Purchase": details_of_purchase,
        "Total (MYR)": amount if amount is not None else "",
    }
    for club in config.CLUB_COLUMNS:
        col_name = f"Amounts for {club}"
        row[col_name] = amount if (amount is not None and club == matched_club) else ""

    values = [row.get(header, "") for header in config.SHEET_HEADERS]
    values.append(task.get("gid", ""))  # hidden tracking column at the end
    return values


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_sheets_service():
    creds = Credentials.from_service_account_file(config.SERVICE_ACCOUNT_FILE, scopes=SHEETS_SCOPES)
    return build("sheets", "v4", credentials=creds)


def col_letter(n):
    """Converts a 1-indexed column number into A1-style letters."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def gid_column_letter():
    """Column letter for the hidden Task GID column (one past the last header)."""
    return col_letter(len(config.SHEET_HEADERS) + 1)


def header_col_letter(header_name):
    """Column letter for a given entry in config.SHEET_HEADERS."""
    return col_letter(config.SHEET_HEADERS.index(header_name) + 1)


def ensure_header_row(service):
    header_row = config.HEADER_ROW
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=config.SPREADSHEET_ID, range=f"{config.SHEET_NAME}!A{header_row}:A{header_row}")
        .execute()
    )
    if not result.get("values"):
        headers = list(config.SHEET_HEADERS) + ["Task GID"]
        service.spreadsheets().values().update(
            spreadsheetId=config.SPREADSHEET_ID,
            range=f"{config.SHEET_NAME}!A{header_row}",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()


def get_existing_rows(service):
    """
    Returns {task_gid: {"row_number": int, "values": [...]}} for every
    row currently in the sheet, so the caller can both detect duplicates
    AND update a row in place when the underlying Asana task has changed.
    """
    last_col = gid_column_letter()
    data_start = config.HEADER_ROW + 1
    result = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=config.SPREADSHEET_ID,
            range=f"{config.SHEET_NAME}!A{data_start}:{last_col}",
            valueRenderOption="FORMULA",
        )
        .execute()
    )
    values = result.get("values", [])
    existing = {}
    for i, row in enumerate(values):
        if row and row[-1]:
            existing[row[-1]] = {"row_number": data_start + i, "values": row}
    return existing


def update_rows(service, updates):
    """updates: list of (row_number, row_values)."""
    if not updates:
        return
    last_col = gid_column_letter()
    data = [
        {"range": f"{config.SHEET_NAME}!A{row_number}:{last_col}{row_number}", "values": [row_values]}
        for row_number, row_values in updates
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=config.SPREADSHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()


def append_rows(service, rows):
    if not rows:
        return
    service.spreadsheets().values().append(
        spreadsheetId=config.SPREADSHEET_ID,
        range=f"{config.SHEET_NAME}!A{config.HEADER_ROW}",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def _parse_sheet_date(date_value):
    """
    Parses a "Date Added" cell value into a datetime, tolerating a few
    different shapes:
      - "2026-06-01" (the string format this script writes)
      - a Google Sheets date serial number (float/int as string), in case
        the cell got auto-formatted as a real Date type by Sheets
      - anything else -> None (caller treats as unparseable)
    """
    if not date_value:
        return None
    text = str(date_value).strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except (ValueError, TypeError):
        pass
    try:
        serial = float(text)
        # Google Sheets date serial epoch is 1899-12-30.
        return datetime(1899, 12, 30) + timedelta(days=serial)
    except (ValueError, TypeError):
        return None


def sort_sheet_by_date_added(service):
    """
    Re-sorts data rows by "Date Added", oldest first. Only touches rows
    BELOW config.HEADER_ROW -- any custom title/preamble rows you've
    added above the real header are never read or moved.

    Uses valueRenderOption=FORMULA when reading so that the "Details of
    Purchase" hyperlink formulas survive the round-trip instead of being
    flattened to plain text.

    Rows with a missing/unparseable date are pushed to the bottom rather
    than dropped, so nothing is ever lost.
    """
    last_col = gid_column_letter()  # last column = hidden Task GID column
    data_start = config.HEADER_ROW + 1
    result = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=config.SPREADSHEET_ID,
            range=f"{config.SHEET_NAME}!A{data_start}:{last_col}",
            valueRenderOption="FORMULA",
        )
        .execute()
    )
    data_rows = result.get("values", [])
    if len(data_rows) < 2:
        return  # nothing meaningful to sort

    date_idx = config.SHEET_HEADERS.index("Date Added")
    num_cols = len(config.SHEET_HEADERS) + 1  # + hidden Task GID column

    def sort_key(row):
        date_str = row[date_idx] if len(row) > date_idx else ""
        parsed = _parse_sheet_date(date_str)
        return (0, parsed) if parsed else (1, datetime.max)

    data_rows.sort(key=sort_key)

    # Pad every row to the same width so columns don't shift when written back.
    normalized = [row + [""] * (num_cols - len(row)) for row in data_rows]

    # Re-write Date Added as clean "YYYY-MM-DD" text. Without this, a cell
    # that Sheets returned as a raw date serial (e.g. 46065) gets written
    # back as that same raw number -- and if the destination row didn't
    # already have Date formatting, it displays as "46065" instead of a
    # date. Writing plain text lets Sheets re-detect and format it fresh.
    for row in normalized:
        parsed = _parse_sheet_date(row[date_idx])
        row[date_idx] = parsed.strftime("%Y-%m-%d") if parsed else row[date_idx]

    service.spreadsheets().values().update(
        spreadsheetId=config.SPREADSHEET_ID,
        range=f"{config.SHEET_NAME}!A{data_start}",
        valueInputOption="USER_ENTERED",
        body={"values": normalized},
    ).execute()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def sync_tasks(tasks, service=None, run_sort=True):
    """
    Core upsert logic, shared by the scheduled full-project sync (main())
    and the webhook server (which calls this with just the 1-2 tasks that
    changed). Appends new tasks, updates existing rows that changed, then
    optionally re-sorts the sheet by Date Added.

    Returns a dict summary: {"appended": int, "updated": int, "unchanged": int}
    """
    if service is None:
        service = get_sheets_service()

    ensure_header_row(service)
    existing = get_existing_rows(service)

    do_invoice_idx = config.SHEET_HEADERS.index("DO & Invoice received?")
    date_idx = config.SHEET_HEADERS.index("Date Added")
    num_cols = len(config.SHEET_HEADERS) + 1

    new_rows = []
    updates = []
    unchanged = 0

    for task in tasks:
        gid = task.get("gid")
        prior = existing.get(gid)

        fields = custom_field_map(task)
        expense_type = determine_expense_type(fields.get(config.FIELD_NATURE_OF_REQUEST, ""))

        attachment_count = None
        if config.CHECK_ATTACHMENTS and expense_type == "PR/PO":
            # Skip the extra API call once we've already confirmed an
            # invoice is attached -- it can't un-attach itself.
            prior_do_invoice = prior["values"][do_invoice_idx] if prior and len(prior["values"]) > do_invoice_idx else None
            if prior_do_invoice != "Yes":
                try:
                    attachment_count = fetch_attachment_count(gid)
                except requests.HTTPError:
                    attachment_count = None

        row = task_to_row(task, attachment_count)

        if prior is None:
            new_rows.append(row)
        else:
            padded_prior = prior["values"] + [""] * (num_cols - len(prior["values"]))
            comparable_new = row[:date_idx] + row[date_idx + 1 :]
            comparable_old = padded_prior[:date_idx] + padded_prior[date_idx + 1 :]
            if comparable_new != comparable_old:
                updates.append((prior["row_number"], row))
            else:
                unchanged += 1

    if new_rows:
        append_rows(service, new_rows)
    if updates:
        update_rows(service, updates)

    if run_sort and (new_rows or updates) and config.AUTO_SORT_BY_DATE:
        sort_sheet_by_date_added(service)

    return {"appended": len(new_rows), "updated": len(updates), "unchanged": unchanged}


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting sync...")

    print("Fetching tasks from Asana...")
    tasks = fetch_asana_tasks(config.ASANA_PROJECT_GID)
    print(f"  Found {len(tasks)} task(s) in the project.")

    print("Connecting to Google Sheets...")
    result = sync_tasks(tasks)

    print(f"Appended {result['appended']} new row(s).")
    print(f"Updated {result['updated']} existing row(s) whose status/details changed.")
    print(f"{result['unchanged']} row(s) unchanged.")
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"Asana API error: {e.response.status_code} {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)