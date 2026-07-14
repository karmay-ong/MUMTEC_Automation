"""
Configuration for the Asana -> Google Sheets financial request log.

Fill in the values below. Don't commit this file with real secrets in it
to a public repo -- see the note at the bottom about environment
variables if you ever host this somewhere.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Asana ------------------------------------------------------------
# Personal Access Token: Asana profile photo -> Settings -> Apps ->
# "Manage Developer Apps" -> "Create new token"
ASANA_ACCESS_TOKEN = os.environ.get("ASANA_TOKEN", "PASTE_YOUR_ASANA_TOKEN_HERE")

# The project to watch for new financial requests, e.g. your
# "MUMTEC Student Activities Request Form" project.
# Find it in the project URL: https://app.asana.com/0/<PROJECT_GID>/list
ASANA_PROJECT_GID = os.environ.get("ASANA_PROJECT_ID", "PASTE_PROJECT_GID_HERE")

# Set to True to check each new task for attachments (used to guess
# "DO & Invoice received?"). This costs one extra API call per NEW task
# only (not per run), so it's fine to leave on.
CHECK_ATTACHMENTS = True

# --- Asana custom field names -------------------------------------------
# Must match exactly what you see in Asana (case-sensitive).
FIELD_CLUB = "Club/Team"
FIELD_NATURE_OF_REQUEST = "Nature of Request"
FIELD_TASK_PROGRESS = "Task Progress"
FIELD_PR_NUMBER = "PR Number"
FIELD_PO_NUMBER = "PO Number"
FIELD_ZNP_NUMBER = "ZNP Number"

# The Task Progress value that means "done" / approved.
TASK_PROGRESS_COMPLETED_VALUE = "Completed"

# Any Task Progress value containing these keywords (case-insensitive)
# is treated as a terminal Rejected/Cancelled state, overriding the
# normal status progression. Handy if you want to exclude these from
# any Balance/expense calculations you set up yourself in the sheet.
TASK_PROGRESS_TERMINAL_STATUSES = {
    "rejected": "Rejected",
    "cancelled": "Cancelled",
    "canceled": "Cancelled",  # US spelling, just in case
}

# --- Expense type bucketing ----------------------------------------------
# Map whatever "Nature of Request" says to one of your 4 sheet categories:
# PR/PO, Claims, Debit Note, Others. Anything not listed here falls back
# to "Others". Keys are matched case-insensitively.
EXPENSE_TYPE_BUCKETS = {
    "pr/po": "PR/PO",
    "claims": "Claims",
    "debit note": "Debit Note",
    "vouchers or merchandise": "Debit Note",  # actual Asana dropdown text
    "merchandise": "Debit Note",
    "voucher": "Debit Note",
}
DEFAULT_EXPENSE_TYPE_BUCKET = "Others"

# --- Clubs -----------------------------------------------------------
# Must match the sheet's club column headers exactly (order matters --
# it determines which physical column each amount gets written to).
# MUSA and SOIT share a single combined column in the sheet.
CLUB_COLUMNS = [
    "MUMTEC",
    "MUSA SOIT",
    "GDG",
    "MBC",
    "MAC",
    "MCC",
    "ACM-W",
    "AWS",
]

# If Asana's "Club/Team" field uses a value that doesn't exactly match
# a CLUB_COLUMNS entry, map it here (lowercase key -> exact CLUB_COLUMNS
# value). Needed because Asana has separate "MUSA" and "SOIT" options
# but the sheet has one combined "MUSA SOIT" column for both.
CLUB_ALIASES = {
    "musa": "MUSA SOIT",
    "soit": "MUSA SOIT",
}

# Set to True to always keep the sheet sorted by "Date Added" (oldest
# first) after each sync run. This also fixes any existing out-of-order
# rows over time, since it re-sorts everything, not just new rows.
AUTO_SORT_BY_DATE = True

# If you have extra title/preamble rows ABOVE the actual column header
# row, set this to the row number where "Date Added, Expense Type, ..."
# actually lives. E.g. if you have 4 custom rows above it, the real
# header is row 5, so set HEADER_ROW = 5. Default (no preamble) is 1.
HEADER_ROW = 1

# --- Exclusions ---------------------------------------------------------
# Tasks in any of these Asana sections (within your project) are skipped
# entirely -- never synced, never counted. Matched case-insensitively.
EXCLUDED_SECTIONS = ["2026"]

# Tasks whose Club/Team field matches any of these are also skipped
# entirely. Matched case-insensitively.
EXCLUDED_CLUBS = ["AI Challenge Cup"]

# --- Google Sheets ------------------------------------------------------
# Path to the service account JSON key file you downloaded from
# Google Cloud Console.
SERVICE_ACCOUNT_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "service_account.json")

# The spreadsheet ID from the sheet's URL:
# https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit
SPREADSHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "PASTE_SPREADSHEET_ID_HERE")

# The tab name within the spreadsheet to write to.
SHEET_NAME = os.environ.get("SHEET_NAME", "Sheet1")

# --- Sheet columns (must match your sheet's header row exactly) ---------
# Note: "Total (MYR)" is intentionally NOT in this list. It's fully
# self-managed by you in the sheet (e.g. a SUM formula across the club
# columns) -- the script never reads or writes it. Just make sure that
# column physically no longer sits between the last club column and the
# hidden Task GID column (delete it, don't just clear it), or every
# column after it will be misaligned.
SHEET_HEADERS = (
    [
        "Date Added",
        "Expense Type",
        "PRPO/SAP Submission No.",
        "Submission Status",
        "Claimant/Payee",
        "Details of Purchase",
    ]
    + [f"{club}" for club in CLUB_COLUMNS]
)

# The script adds ONE extra column after the last club column, called
# "Task GID". It's used internally to detect which tasks are already
# logged, so new runs never create duplicate rows. Feel free to hide
# that column in
# Google Sheets (right-click the column -> Hide column) -- just don't
# delete it or its values.

# ---------------------------------------------------------------------
# Note on hosting: if you run this on GitHub Actions, a server, etc,
# don't commit your .env file with real secrets to a public repo.
# For GitHub Actions specifically, set these as repo Secrets instead
# and export them as environment variables in the workflow -- the
# placeholders above only matter if .env / the environment has nothing set.