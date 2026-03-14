"""
Gmail Attachment Downloader
----------------------------
Downloads attachments from Gmail in two modes:

  Invoice mode (default)
      Reads each attachment's content and saves it only when invoice/receipt
      keywords are found inside the file.

      python gmail_downloader.py --since 2024-01-01 --output ./invoices

  Label mode (--label)
      Downloads every attachment from emails carrying the given Gmail label.
      No content filtering is applied — the label is already the classifier.

      python gmail_downloader.py --since 2024-01-01 --label "CV Actiris" \\
          --output ./cvs --token token_cv.json

      The --token option lets you authenticate a separate Gmail account.
      On first use a browser window opens for OAuth; afterwards the token
      is reused from the specified file.

Requirements:
    1. A Google Cloud project with the Gmail API enabled.
    2. An OAuth 2.0 credential file (credentials.json) in the same directory.
    3. Run `pip install -r requirements.txt` before first use.
"""

import argparse
import base64
import io
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
DEFAULT_TOKEN_FILE = "token.json"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate(token_file: str = DEFAULT_TOKEN_FILE) -> Credentials:
    """Return valid Gmail API credentials, refreshing or re-authorising as needed."""
    creds = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

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

        with open(token_file, "w") as f:
            f.write(creds.to_json())
        print(f"[INFO] Credentials saved to '{token_file}'.")

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


def get_label_id(service, label_name: str) -> str:
    """Return the Gmail label ID for the given display name, or exit with an error."""
    result = service.users().labels().list(userId="me").execute()
    for label in result.get("labels", []):
        if label["name"].lower() == label_name.lower():
            return label["id"]
    available = ", ".join(f'"{lb["name"]}"' for lb in result.get("labels", []))
    sys.exit(
        f"[ERROR] Label '{label_name}' not found.\n"
        f"Available labels: {available}"
    )


def list_messages(service, query: str, label_ids: list[str] | None = None) -> list[dict]:
    """Return all message stubs matching *query* (handles pagination)."""
    messages = []
    kwargs: dict = {"userId": "me", "q": query}
    if label_ids:
        kwargs["labelIds"] = label_ids
    response = service.users().messages().list(**kwargs).execute()

    while True:
        messages.extend(response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
        response = (
            service.users()
            .messages()
            .list(**kwargs, pageToken=page_token)
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


# Keywords that identify invoice/receipt content, across common languages.
# EN=English  FR=French  NL=Dutch  DE=German  ES=Spanish  IT=Italian  PT=Portuguese
_INVOICE_KEYWORDS = re.compile(
    r"invoice|facture|factuur|rechnung|factura|fattura|fatura"   # invoice
    r"|receipt|reçu|recu|quittung|recibo|ricevuta"               # receipt
    r"|total\s*(due|amount|ht|ttc|excl|incl)"                    # totals
    r"|montant\s*(total|ht|ttc)"                                  # FR totals
    r"|btw|tva|mwst|vat|iva"                                     # tax labels
    r"|bill\s*to|ship\s*to|sold\s*to"                            # EN billing
    r"|bon de commande|devis",                                    # FR order/quote
    re.IGNORECASE,
)


def _extract_text(data: bytes, filename: str) -> str | None:
    """
    Try to extract plain text from *data*.

    Returns the extracted text, or None if the format is not supported /
    a required library is missing.
    """
    lower = filename.lower()

    # --- PDF ---
    if lower.endswith(".pdf"):
        try:
            from pypdf import PdfReader  # type: ignore
            reader = PdfReader(io.BytesIO(data))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return None

    # --- Raster images (OCR) ---
    if lower.endswith((".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".gif", ".webp")):
        try:
            import pytesseract          # type: ignore
            from PIL import Image       # type: ignore
            return pytesseract.image_to_string(Image.open(io.BytesIO(data)))
        except Exception:
            return None

    return None  # unsupported format


def is_invoice_content(data: bytes, filename: str) -> bool:
    """
    Return True if the file *looks* like an invoice based on its content.

    For formats we cannot parse (e.g. .zip, .xml), we return True so that
    potentially relevant files are never silently dropped.
    """
    text = _extract_text(data, filename)
    if text is None:
        # Can't read the content — keep the file to be safe.
        return True
    return bool(_INVOICE_KEYWORDS.search(text))


def download_attachments(
    service, message: dict, output_dir: Path, check_content: bool = True
) -> int:
    """
    Download attachments in *message* to *output_dir*.

    When *check_content* is True (invoice mode) each file's content is read
    and must match invoice keywords to be saved.  When False (label mode) all
    attachments are saved — the Gmail label is already the filter.

    Returns the number of attachments saved.
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

        # Fetch the attachment data
        attachment = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=msg_id, id=attachment_id)
            .execute()
        )

        data = base64.urlsafe_b64decode(attachment["data"])

        # In invoice mode, verify content before saving
        if check_content and not is_invoice_content(data, filename):
            print(f"  [SKIP] {filename!r} (content does not match invoice keywords)")
            continue

        # Build a safe output path, creating per-message subdirectories
        safe_subject = "".join(c if c.isalnum() or c in " _-" else "_" for c in subject)[:60].strip()
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
        description=(
            "Download Gmail attachments received on or after a given date.\n\n"
            "Invoice mode (default): checks attachment content for invoice keywords.\n"
            "Label mode (--label): downloads all attachments from emails with that label."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        help="Restrict search to emails from a specific sender.",
    )
    parser.add_argument(
        "--label",
        default=None,
        metavar="LABEL",
        help=(
            "Download all attachments from emails carrying this Gmail label "
            "(e.g. 'CV Actiris'). Skips content-keyword filtering."
        ),
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN_FILE,
        metavar="FILE",
        help=(
            f"OAuth token file to use (default: {DEFAULT_TOKEN_FILE}). "
            "Use a different file to authenticate a second Gmail account."
        ),
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

    print("[INFO] Authenticating with Gmail API…")
    creds = authenticate(args.token)
    service = build("gmail", "v1", credentials=creds)

    # Resolve label filter (if requested)
    label_ids: list[str] | None = None
    if args.label:
        label_id = get_label_id(service, args.label)
        label_ids = [label_id]
        print(f"[INFO] Using label '{args.label}' (id={label_id})")

    query = build_query(since, sender=args.sender)
    print(f"[INFO] Searching Gmail with query: {query!r}")

    try:
        messages = list_messages(service, query, label_ids=label_ids)
    except HttpError as err:
        sys.exit(f"[ERROR] Gmail API error: {err}")

    if not messages:
        print("[INFO] No messages with attachments found for the given criteria.")
        return

    # In label mode we trust the label; in invoice mode we read each file.
    check_content = args.label is None

    print(f"[INFO] Found {len(messages)} message(s). Downloading attachments…\n")

    total_attachments = 0
    for i, stub in enumerate(messages, start=1):
        message = get_message(service, stub["id"])
        subject = extract_subject(message)
        print(f"[{i}/{len(messages)}] {subject}")
        count = download_attachments(service, message, output_dir, check_content=check_content)
        if count == 0:
            print("  (no downloadable attachments)")
        total_attachments += count

    print(f"\n[DONE] {total_attachments} attachment(s) saved to '{output_dir.resolve()}'.")


if __name__ == "__main__":
    main()
