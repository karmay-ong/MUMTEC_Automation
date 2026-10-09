#!/usr/bin/env python3
"""
Run this ONCE. It's safe to re-run (it just rewrites the same header
text/merge), but it will NOT touch anything in row 5+ -- your actual
request data is never touched by this script.

Usage:
    python setup_header.py
"""

import sys

import config
import sync_asana_to_sheets as sync


def main():
    service = sync.get_sheets_service()

    # --- Figure out the sheetId (needed for the merge request; the
    # Sheets API identifies tabs by a numeric ID, not the name).
    spreadsheet = service.spreadsheets().get(spreadsheetId=config.SPREADSHEET_ID).execute()
    sheet_id = None
    for sheet in spreadsheet.get("sheets", []):
        if sheet["properties"]["title"] == config.SHEET_NAME:
            sheet_id = sheet["properties"]["sheetId"]
            break
    if sheet_id is None:
        print(f"Could not find a tab named '{config.SHEET_NAME}' in this spreadsheet.", file=sys.stderr)
        sys.exit(1)

    headers = config.SHEET_HEADERS
    details_col = sync.header_col_letter("Details of Purchase")
    first_club_col = sync.header_col_letter(f"Amounts for {config.CLUB_COLUMNS[0]}")
    last_club_col = sync.header_col_letter(f"Amounts for {config.CLUB_COLUMNS[-1]}")

    # --- Row 1: every plain column header, except the club columns
    # (which get merged into one "Amounts" cell instead of individual
    # "Amounts for X" labels).
    row1 = []
    for header in headers:
        if header.startswith("Amounts for"):
            row1.append("")  # part of the merge; only the first cell gets text
        else:
            row1.append(header)
    row1[headers.index(f"Amounts for {config.CLUB_COLUMNS[0]}")] = "Amounts"

    # --- Row 2: "Clubs" label + each club's name across its column.
    row2 = [""] * len(headers)
    row2[headers.index("Details of Purchase")] = "Clubs"
    for club in config.CLUB_COLUMNS:
        row2[headers.index(f"Amounts for {club}")] = club

    # --- Row 3 / Row 4: just the row labels. You fill in the numbers
    # (or your own formulas) yourself -- this script never writes budget
    # or balance values, only these two labels.
    row3 = [""] * len(headers)
    row3[headers.index("Details of Purchase")] = "Total budget"

    row4 = [""] * len(headers)
    row4[headers.index("Details of Purchase")] = "Balance"

    values_update = {
        "valueInputOption": "USER_ENTERED",
        "data": [
            {"range": f"{config.SHEET_NAME}!A1", "values": [row1]},
            {"range": f"{config.SHEET_NAME}!A2", "values": [row2]},
            {"range": f"{config.SHEET_NAME}!A3", "values": [row3]},
            {"range": f"{config.SHEET_NAME}!A4", "values": [row4]},
        ],
    }
    service.spreadsheets().values().batchUpdate(spreadsheetId=config.SPREADSHEET_ID, body=values_update).execute()
    print("Wrote header text to rows 1-4.")

    # --- Merge the club columns in row 1 into a single "Amounts" cell.
    merge_request = {
        "requests": [
            {
                "mergeCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,  # row 1 (0-indexed)
                        "endRowIndex": 1,
                        "startColumnIndex": headers.index(f"Amounts for {config.CLUB_COLUMNS[0]}"),
                        "endColumnIndex": headers.index(f"Amounts for {config.CLUB_COLUMNS[-1]}") + 1,
                    },
                    "mergeType": "MERGE_ALL",
                }
            }
        ]
    }
    service.spreadsheets().batchUpdate(spreadsheetId=config.SPREADSHEET_ID, body=merge_request).execute()
    print(f"Merged {first_club_col}1:{last_club_col}1 into a single 'Amounts' cell.")

    print()
    print("Done! Your header now matches:")
    print(f"  Row 1: ... | Details of Purchase | Amounts (merged {first_club_col}1:{last_club_col}1)")
    print(f"  Row 2: ... | Clubs                | {' | '.join(config.CLUB_COLUMNS)}")
    print("  Row 3: ... | Total budget          | (fill in your own budgets)")
    print("  Row 4: ... | Balance               | (fill in your own formulas)")
    print()
    print("NOTE: 'Total (MYR)' is entirely yours to add -- put it OUTSIDE")
    print("the columns this script manages (e.g. to the right of the hidden")
    print("Task GID column), not immediately after the last club column,")
    print("or every column after it will be misaligned.")
    print()
    print("Now go fill in Total budget (row 3) and Balance (row 4) yourself,")
    print("then run sync_asana_to_sheets.py as normal.")


if __name__ == "__main__":
    main()