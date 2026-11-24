"""Gmail API client (OAuth desktop flow, scopes readonly + send).

Every caller must be approval-gated (see ``gmail_*`` tools); this module never decides policy.
Email text is attacker-controlled, so ``read_message`` wraps it in untrusted-data markers.
Google libraries are imported lazily so the app starts without them.
"""
import base64
import threading
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config.log import get_logger

logger = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"]
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CREDENTIALS_FILE = DATA_DIR / "gmail_credentials.json"
TOKEN_FILE = DATA_DIR / "gmail_token.json"
MAX_BODY_CHARS = 8000
MAX_RESULTS_CAP = 25
UNTRUSTED_OPEN = "<untrusted_email_content>"
UNTRUSTED_CLOSE = "</untrusted_email_content>"


class GmailError(Exception):
    """User-presentable Gmail failure (never contains tokens)."""


def status() -> Dict[str, bool]:
    return {"credentials_present": CREDENTIALS_FILE.is_file(), "authorized": TOKEN_FILE.is_file()}


def _credentials(interactive: bool = False):
    """Load the saved token, refreshing it when expired. Browser login only if ``interactive``."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES) if TOKEN_FILE.is_file() else None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive:
        from google_auth_oauthlib.flow import InstalledAppFlow
        if not CREDENTIALS_FILE.is_file():
            raise GmailError(f"Missing {CREDENTIALS_FILE.name} in the data folder.")
        creds = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES).run_local_server(port=0)
    else:
        raise GmailError("Gmail is not authorized yet. Run: python -m src.services.gmail_service")
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def authorize() -> None:
    """One-time browser login; a no-op when a usable token already exists."""
    _credentials(interactive=True)


def _header(msg: Dict[str, Any], name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _plain_text(payload: Dict[str, Any]) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = _plain_text(part)
        if text:
            return text
    return ""


def _clean(value: str) -> str:
    return " ".join(str(value).split())  # no CR/LF: blocks header injection


class GmailService:
    def __init__(self, api: Optional[Any] = None):
        self._api = api  # injected in tests
        self._lock = threading.Lock()

    def _users(self):
        with self._lock:
            if self._api is None:
                from googleapiclient.discovery import build
                self._api = build("gmail", "v1", credentials=_credentials(), cache_discovery=False)
            return self._api.users()

    def list_messages(self, query: str = "", max_results: int = 10) -> List[Dict[str, str]]:
        n = max(1, min(int(max_results), MAX_RESULTS_CAP))
        users = self._users()
        refs = users.messages().list(userId="me", q=query, maxResults=n).execute().get("messages", [])
        out = []
        for ref in refs:
            m = users.messages().get(userId="me", id=ref["id"], format="metadata",
                                     metadataHeaders=["From", "Subject", "Date"]).execute()
            out.append({"id": m["id"], "from": _header(m, "From"), "subject": _header(m, "Subject"),
                        "date": _header(m, "Date"), "snippet": m.get("snippet", "")})
        return out

    def read_message(self, message_id: str) -> Dict[str, str]:
        m = self._users().messages().get(userId="me", id=str(message_id), format="full").execute()
        body = _plain_text(m.get("payload", {})) or m.get("snippet", "")
        truncated = len(body) > MAX_BODY_CHARS
        body = body[:MAX_BODY_CHARS] + ("\n[truncated]" if truncated else "")
        return {"id": m["id"], "from": _header(m, "From"), "to": _header(m, "To"),
                "subject": _header(m, "Subject"), "date": _header(m, "Date"),
                "body": f"{UNTRUSTED_OPEN}\n{body.replace(UNTRUSTED_CLOSE, '')}\n{UNTRUSTED_CLOSE}"}

    def send_email(self, to: str, subject: str, body: str, reply_to_message_id: Optional[str] = None) -> Dict[str, str]:
        addr = parseaddr(_clean(to))[1]
        if "@" not in addr:
            raise GmailError("Invalid recipient address.")
        msg = EmailMessage()
        msg["To"], msg["Subject"] = addr, _clean(subject)
        msg.set_content(str(body), charset="utf-8")
        send: Dict[str, Any] = {}
        if reply_to_message_id:
            orig = self._users().messages().get(userId="me", id=str(reply_to_message_id), format="metadata",
                                                metadataHeaders=["Message-ID"]).execute()
            if mid := _header(orig, "Message-ID"):
                msg["In-Reply-To"] = msg["References"] = _clean(mid)
            send["threadId"] = orig.get("threadId")
        send["raw"] = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        res = self._users().messages().send(userId="me", body=send).execute()
        logger.info("Gmail message sent (id=%s)", res.get("id"))
        return {"id": res.get("id", ""), "to": addr}


_service: Optional[GmailService] = None


def get_gmail_service() -> GmailService:
    global _service
    if _service is None:
        _service = GmailService()
    return _service


if __name__ == "__main__":
    authorize()
    print("Gmail authorized.")  # CLI helper, not runtime code
