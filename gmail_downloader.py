"""
Gmail Attachment Downloader
----------------------------
Downloads invoice attachments from Gmail messages received on or after a given date.
Only files whose name or the email subject contains an invoice-related keyword are saved.

Usage:
    python gmail_downloader.py --since 2024-01-01 --output ./downloads

Requirements:
    1. A Google Cloud project with the Gmail API enabled.
    2. An OAuth 2.0 credential file (credentials.json) in the same directory.
    3. Run `pip install -r requirements.txt` before first use.

First run will open a browser window for Google OAuth authentication.
A token.json file is saved locally so you only authenticate once.
"""

import argparse
import base64
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Read-only scope is sufficient for downloading attachments.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate() -> Credentials:
    """Return valid Gmail API credentials, refreshing or re-authorising as needed."""
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                sys.exit(
                    f"[ERROR] '{CREDENTIALS_FILE}' not found.\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as token_file:
            token_file.write(creds.to_json())
        print(f"[INFO] Credentials saved to '{TOKEN_FILE}'.")

    return creds


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def build_query(since: datetime, sender: str | None = None) -> str:
    """Build a Gmail search query string."""
    # Gmail date format: YYYY/MM/DD
    date_str = since.strftime("%Y/%m/%d")
    query = f"after:{date_str} has:attachment"
    if sender:
        query += f" from:{sender}"
    return query


def list_messages(service, query: str) -> list[dict]:
    """Return all message stubs matching *query* (handles pagination)."""
    messages = []
    response = service.users().messages().list(userId="me", q=query).execute()

    while True:
        messages.extend(response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
        response = (
            service.users()
            .messages()
            .list(userId="me", q=query, pageToken=page_token)
            .execute()
        )

    return messages


def get_message(service, msg_id: str) -> dict:
    """Fetch a full message by ID."""
    return (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="full")
        .execute()
    )


def extract_subject(message: dict) -> str:
    """Pull the Subject header from a message, falling back to the message ID."""
    headers = message.get("payload", {}).get("headers", [])
    for header in headers:
        if header["name"].lower() == "subject":
            return header["value"]
    return message["id"]


def get_parts(payload: dict) -> list[dict]:
    """Recursively collect all parts of a MIME message."""
    parts = []
    if "parts" in payload:
        for part in payload["parts"]:
            parts.extend(get_parts(part))
    else:
        parts.append(payload)
    return parts


# Keywords that identify invoice-related files/emails, across common languages.
# EN=English  FR=French  NL=Dutch  DE=German  ES=Spanish  IT=Italian  PT=Portuguese
_INVOICE_KEYWORDS = re.compile(
    r"invoice|facture|factuur|rechnung|factura|fattura|fatura"   # invoice
    r"|receipt|reçu|recu|quittung|recibo|ricevuta|recibo"        # receipt
    r"|bill|bon de|nota ",                                        # bill / misc
    re.IGNORECASE,
)


def is_invoice(filename: str, subject: str) -> bool:
    """Return True if the attachment or its email looks like an invoice."""
    return bool(
        _INVOICE_KEYWORDS.search(filename)
        or _INVOICE_KEYWORDS.search(subject)
    )


def download_attachments(service, message: dict, output_dir: Path) -> int:
    """
    Download invoice attachments in *message* to *output_dir*.

    Only attachments whose filename or email subject matches an invoice-related
    keyword are saved. Returns the number of attachments saved.
    """
    saved = 0
    msg_id = message["id"]
    subject = extract_subject(message)
    payload = message.get("payload", {})

    for part in get_parts(payload):
        filename = part.get("filename")
        body = part.get("body", {})
        attachment_id = body.get("attachmentId")

        # Skip parts that are not file attachments
        if not filename or not attachment_id:
            continue

        # Skip attachments that don't look like invoices
        if not is_invoice(filename, subject):
            print(f"  [SKIP] {filename!r} (not an invoice)")
            continue

        # Fetch the attachment data
        attachment = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=msg_id, id=attachment_id)
            .execute()
        )

        data = base64.urlsafe_b64decode(attachment["data"])

        # Build a safe output path, creating per-message subdirectories
        safe_subject = "".join(c if c.isalnum() or c in " _-" else "_" for c in subject)[:60]
        msg_dir = output_dir / f"{msg_id}_{safe_subject}"
        msg_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize filename: replace path separators and other illegal Windows
        # characters so they never create unexpected subdirectories.
        safe_filename = "".join(
            c if c.isalnum() or c in " ._-" else "_"
            for c in filename
        )
        dest = msg_dir / safe_filename
        dest.write_bytes(data)
        print(f"  [SAVED] {dest}")
        saved += 1

    return saved


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Gmail attachments received on or after a given date."
    )
    parser.add_argument(
        "--since",
        required=True,
        metavar="YYYY-MM-DD",
        help="Download attachments from emails received on or after this date.",
    )
    parser.add_argument(
        "--output",
        default="./downloads",
        metavar="DIR",
        help="Directory where attachments will be saved (default: ./downloads).",
    )
    parser.add_argument(
        "--sender",
        default=None,
        metavar="EMAIL",
        help="Optional: restrict search to emails from a specific sender.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Validate date
    try:
        since = datetime.strptime(args.since, "%Y-%m-%d")
    except ValueError:
        sys.exit(f"[ERROR] Invalid date '{args.since}'. Use YYYY-MM-DD format.")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] Authenticating with Gmail API…")
    creds = authenticate()
    service = build("gmail", "v1", credentials=creds)

    query = build_query(since, sender=args.sender)
    print(f"[INFO] Searching Gmail with query: {query!r}")

    try:
        messages = list_messages(service, query)
    except HttpError as err:
        sys.exit(f"[ERROR] Gmail API error: {err}")

    if not messages:
        print("[INFO] No messages with attachments found for the given criteria.")
        return

    print(f"[INFO] Found {len(messages)} message(s). Downloading attachments…\n")

    total_attachments = 0
    for i, stub in enumerate(messages, start=1):
        message = get_message(service, stub["id"])
        subject = extract_subject(message)
        print(f"[{i}/{len(messages)}] {subject}")
        count = download_attachments(service, message, output_dir)
        if count == 0:
            print("  (no downloadable attachments)")
        total_attachments += count

    print(f"\n[DONE] {total_attachments} attachment(s) saved to '{output_dir.resolve()}'.")


if __name__ == "__main__":
    main()
