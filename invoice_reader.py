"""
Reads attachments on an Asana task and uses Google's Gemini API (free
tier) to actually determine whether an invoice and/or delivery order (DO)
has been submitted -- not just "is there any file attached."

Requires GEMINI_API_KEY in config.py / .env. If it's not set, every
function here is a no-op that returns "nothing detected" so the rest of
the script falls back to its old attachment-count-only behavior.
"""

import base64
import json
import mimetypes
import re
import sys

import requests

import config

ASANA_API_BASE = "https://app.asana.com/api/1.0"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

CLASSIFY_PROMPT = """You are looking at a single file attached to a purchase request. \
Determine two things and respond with ONLY a raw JSON object, no markdown, no code fences, no explanation:

{
  "is_invoice": true or false,
  "is_delivery_order": true or false,
  "amount": <the total amount on the invoice as a plain number, or null if not an invoice or no total found>,
  "currency": "<currency code or symbol as shown, or null>"
}

A document is an invoice if it's a bill/statement of amount owed from a vendor, showing a total amount.
A document is a delivery order (DO) if it's a delivery note / goods received note / proof of delivery \
(it does NOT need to show a monetary amount).
A document can be neither (e.g. a quotation, a photo, a chat screenshot) -- if so, both should be false.
If it's an invoice, "amount" must be the FINAL total amount payable (not a subtotal or line item).
Respond with ONLY the JSON object."""


def _asana_headers():
    return {"Authorization": f"Bearer {config.ASANA_ACCESS_TOKEN}"}


def fetch_task_attachments(task_gid):
    """Returns a list of {gid, name, download_url} for a task's attachments."""
    url = f"{ASANA_API_BASE}/tasks/{task_gid}/attachments"
    resp = requests.get(
        url,
        headers=_asana_headers(),
        params={"opt_fields": "name,download_url,resource_subtype"},
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def _download_file(download_url):
    """Returns (bytes, mime_type) or (None, None) on failure."""
    try:
        resp = requests.get(download_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  Failed to download attachment: {e}", file=sys.stderr)
        return None, None

    mime_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
    if not mime_type or mime_type == "application/octet-stream":
        mime_type = mimetypes.guess_type(download_url)[0] or "application/octet-stream"
    return resp.content, mime_type


def _strip_json_fences(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def classify_document(file_bytes, mime_type, filename=""):
    """
    Sends one file to Gemini and returns a dict:
        {"is_invoice": bool, "is_delivery_order": bool, "amount": float|None, "currency": str|None}
    Returns all-False/None on any failure (network error, unsupported
    file type, bad response) -- never raises, so one bad attachment can't
    break the whole sync run.
    """
    empty = {"is_invoice": False, "is_delivery_order": False, "amount": None, "currency": None}

    # Gemini only handles a specific set of document/image types.
    supported_prefixes = ("image/", "application/pdf")
    if not mime_type.startswith(supported_prefixes):
        return empty

    url = f"{GEMINI_API_BASE}/models/{config.GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(file_bytes).decode()}},
                    {"text": CLASSIFY_PROMPT},
                ]
            }
        ]
    }

    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json", "x-goog-api-key": config.GEMINI_API_KEY},
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(_strip_json_fences(text))
    except Exception as e:
        print(f"  Gemini classification failed for '{filename}': {e}", file=sys.stderr)
        return empty

    amount = parsed.get("amount")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None

    return {
        "is_invoice": bool(parsed.get("is_invoice")),
        "is_delivery_order": bool(parsed.get("is_delivery_order")),
        "amount": amount,
        "currency": parsed.get("currency"),
    }


def analyze_task_documents(task_gid):
    """
    Downloads and classifies every attachment on a task. Returns:
        {"has_invoice": bool, "has_delivery_order": bool, "invoice_amount": float|None}
    Stops downloading further files once both an invoice and a DO have
    been confirmed, to save time/quota.
    """
    result = {"has_invoice": False, "has_delivery_order": False, "invoice_amount": None}

    if not config.INVOICE_READING_ENABLED:
        return result

    try:
        attachments = fetch_task_attachments(task_gid)
    except requests.HTTPError as e:
        print(f"  Failed to list attachments for task {task_gid}: {e}", file=sys.stderr)
        return result

    for attachment in attachments:
        if result["has_invoice"] and result["has_delivery_order"]:
            break  # already confirmed both -- no need to check remaining files

        download_url = attachment.get("download_url")
        if not download_url:
            continue

        file_bytes, mime_type = _download_file(download_url)
        if file_bytes is None:
            continue

        classification = classify_document(file_bytes, mime_type, attachment.get("name", ""))

        if classification["is_invoice"]:
            result["has_invoice"] = True
            if classification["amount"] is not None:
                result["invoice_amount"] = classification["amount"]
        if classification["is_delivery_order"]:
            result["has_delivery_order"] = True

    return result