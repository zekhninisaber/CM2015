# Gmail Attachment Downloader

A simple Python script that connects to your Gmail account via the official
Gmail API and downloads all email attachments received **on or after a given
date**.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | Uses the `str \| None` union syntax |
| Google account | The Gmail account you want to read |
| Google Cloud project | Free tier is sufficient |

---

## 1. Enable the Gmail API and get credentials

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or select an existing one).
3. Navigate to **APIs & Services → Library** and enable the **Gmail API**.
4. Go to **APIs & Services → Credentials** and click **Create Credentials → OAuth client ID**.
   - Application type: **Desktop app**
   - Give it a name (e.g. `gmail-downloader`)
5. Click **Download JSON** and save the file as **`credentials.json`** in the
   same directory as `gmail_downloader.py`.
6. Go to **APIs & Services → OAuth consent screen**, add your Gmail address as
   a **Test user** (required while the app is in "Testing" mode).

---

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

---

## 3. Run the script

```bash
# Download all attachments from emails received on or after 2024-01-01
python gmail_downloader.py --since 2024-01-01

# Save to a custom directory
python gmail_downloader.py --since 2024-01-01 --output /path/to/my/folder

# Filter by sender as well
python gmail_downloader.py --since 2024-01-01 --sender invoices@example.com
```

The **first run** opens a browser window for Google OAuth consent.
After you approve, a `token.json` file is saved locally — subsequent runs will
not prompt you again unless the token expires.

---

## CLI options

| Option | Required | Description |
|---|---|---|
| `--since YYYY-MM-DD` | Yes | Only process emails from this date onward |
| `--output DIR` | No | Destination folder (default: `./downloads`) |
| `--sender EMAIL` | No | Restrict to emails from a specific address |

---

## Output structure

```
downloads/
└── <message_id>_<subject>/
    ├── invoice.pdf
    └── receipt.png
```

Each message gets its own sub-folder named after its Gmail message ID and
(truncated) subject line, so files from different emails never collide.

---

## Security notes

- `credentials.json` and `token.json` contain sensitive OAuth data.
  **Do not commit them to version control.**
  Add them to your `.gitignore`:
  ```
  credentials.json
  token.json
  ```
- The script requests the **read-only** Gmail scope
  (`https://www.googleapis.com/auth/gmail.readonly`), so it cannot send,
  delete, or modify any email.
