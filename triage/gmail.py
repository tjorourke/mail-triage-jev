"""Gmail access. Only label changes are made to messages: no delete, no send."""
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser

import keyring
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import ROOT

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # read + labels; cannot delete or send
log = logging.getLogger("triage")
KEYCHAIN_SERVICE = "mail-triage"
KEYCHAIN_USER = "gmail-token"
CLIENT_SECRET = ROOT / "client_secret.json"


# ---------- auth ----------

def get_service(interactive=True):
    creds = None
    raw = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USER)
    if raw:
        creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USER, creds.to_json())
    if not creds or not creds.valid:
        if not interactive:
            raise SystemExit("Not signed in. Run: uv run triage login")
        if not CLIENT_SECRET.exists():
            raise SystemExit(f"Missing {CLIENT_SECRET}. See SETUP.md, step 1.")
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USER, creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def my_addresses(svc):
    """Primary address plus every send-as alias on the account."""
    addrs = {backoff(lambda: svc.users().getProfile(userId="me").execute())["emailAddress"].lower()}
    try:
        for a in svc.users().settings().sendAs().list(userId="me").execute().get("sendAs", []):
            addrs.add(a["sendAsEmail"].lower())
    except Exception:
        pass   # alias list needs an extra permission on some accounts; config + Delivered-To still cover it
    return addrs


def logout():
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_USER)
    except keyring.errors.PasswordDeleteError:
        pass



# ---------- rate limits ----------

def _is_rate_limit(e):
    msg = str(e)
    return e.resp.status in (429, 500, 503) or (e.resp.status == 403 and "ateLimitExceeded" in msg)


def backoff(call, tries=6):
    """Run call(); if Gmail says 'slow down' (per-minute quota) wait and retry instead of failing."""
    delay = 20
    for i in range(tries):
        try:
            return call()
        except HttpError as e:
            if not _is_rate_limit(e) or i == tries - 1:
                raise
            log.info("gmail rate limit; waiting %ds before retrying", delay)
            time.sleep(delay)
            delay = min(delay * 2, 120)


# ---------- labels ----------

def ensure_labels(svc, names, colors=None):
    existing = {l["name"]: l["id"] for l in backoff(lambda: svc.users().labels().list(userId="me").execute())["labels"]}
    out = {}
    for n in names:
        if n not in existing:
            body = {"name": n, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
            if colors and n in colors:
                body["color"] = colors[n]
            created = backoff(lambda: svc.users().labels().create(userId="me", body=body).execute())
            existing[n] = created["id"]
        out[n] = existing[n]
    return out


def modify(svc, ids, add=(), remove=()):
    """Add/remove labels on many messages at once (batchModify takes up to 1000 ids)."""
    ids = list(ids)
    for i in range(0, len(ids), 1000):
        backoff(lambda: svc.users().messages().batchModify(
            userId="me",
            body={"ids": ids[i:i + 1000], "addLabelIds": list(add), "removeLabelIds": list(remove)},
        ).execute(num_retries=3))


# ---------- reading ----------

def list_ids(svc, query, limit, with_threads=False):
    """Message ids matching query (or (id, threadId) pairs)."""
    out, token = [], None
    while len(out) < limit:
        r = backoff(lambda: svc.users().messages().list(
            userId="me", q=query, maxResults=min(500, limit - len(out)), pageToken=token
        ).execute(num_retries=3))
        out += [(m["id"], m["threadId"]) if with_threads else m["id"] for m in r.get("messages", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    return out


def list_ids_by_label(svc, label_id, limit=20000):
    out, token = [], None
    while len(out) < limit:
        r = backoff(lambda: svc.users().messages().list(
            userId="me", labelIds=[label_id], maxResults=500, pageToken=token).execute(num_retries=3))
        out += [m["id"] for m in r.get("messages", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    return out


def list_ids_with_labels(svc, label_ids, q="", limit=5000):
    """Messages that carry ALL the given labels (optionally narrowed by a search), as (id, threadId) pairs."""
    out, token = [], None
    while len(out) < limit:
        r = backoff(lambda: svc.users().messages().list(
            userId="me", labelIds=list(label_ids), q=q, maxResults=500, pageToken=token).execute(num_retries=3))
        out += [(m["id"], m["threadId"]) for m in r.get("messages", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    return out


def _batch_retry(svc, keys, build_request, on_ok):
    """Run many requests in batches; items refused for rate limits are retried after a pause (never silently dropped)."""
    todo, delay = list(keys), 20
    for attempt in range(6):
        failed = []

        def cb(rid, resp, exc, _failed=failed):
            if exc is None:
                on_ok(rid, resp)
            elif isinstance(exc, HttpError) and _is_rate_limit(exc):
                _failed.append(rid)

        for i in range(0, len(todo), 30):
            b = svc.new_batch_http_request(callback=cb)
            for k in todo[i:i + 30]:
                b.add(build_request(k), request_id=k)
            backoff(b.execute)
        todo = failed
        if not todo:
            return
        log.info("gmail rate limit on %d requests; waiting %ds", len(todo), delay)
        time.sleep(delay)
        delay = min(delay * 2, 120)


def get_threads_meta(svc, thread_ids):
    """thread id -> list of messages (id, labelIds, internalDate in seconds, headers), oldest first."""
    out = {}

    def ok(rid, resp):
        msgs = []
        for m in resp.get("messages", []):
            h = {x["name"].lower(): x["value"] for x in m.get("payload", {}).get("headers", [])}
            msgs.append({"id": m["id"], "labels": set(m.get("labelIds", [])),
                         "ts": int(m.get("internalDate", 0)) // 1000, "headers": h})
        out[resp["id"]] = sorted(msgs, key=lambda x: x["ts"])

    _batch_retry(svc, thread_ids,
                 lambda tid: svc.users().threads().get(userId="me", id=tid, format="metadata",
                                                      metadataHeaders=["From", "To", "Cc", "Subject", "Delivered-To"]), ok)
    return out


def get_headers(svc, ids):
    """message id -> {header name (lowercase): value} for From/To/Cc/Subject (metadata only)."""
    out = {}

    def ok(rid, resp):
        out[resp["id"]] = {h["name"].lower(): h["value"] for h in resp.get("payload", {}).get("headers", [])}

    _batch_retry(svc, list(ids),
                 lambda mid: svc.users().messages().get(userId="me", id=mid, format="metadata",
                                                       metadataHeaders=["From", "To", "Cc", "Subject"]), ok)
    return out


def _batch_get(svc, ids, **kwargs):
    """Fetch many messages; items refused for rate limits are retried after a pause."""
    out, todo, delay = {}, list(ids), 20
    for attempt in range(5):
        failed = []

        def cb(rid, resp, exc, _failed=failed):
            if exc is None:
                out[resp["id"]] = resp
            elif isinstance(exc, HttpError) and _is_rate_limit(exc):
                _failed.append(rid)
            # other errors (e.g. message deleted meanwhile): skip it

        for i in range(0, len(todo), 40):
            b = svc.new_batch_http_request(callback=cb)
            for mid in todo[i:i + 40]:
                b.add(svc.users().messages().get(userId="me", id=mid, **kwargs), request_id=mid)
            backoff(b.execute)
        todo = failed
        if not todo:
            break
        log.info("gmail rate limit on %d messages; waiting %ds", len(todo), delay)
        time.sleep(delay)
        delay = min(delay * 2, 120)
    return out


def get_full(svc, ids):
    raw = _batch_get(svc, ids, format="full")
    return [parse_message(raw[i]) for i in ids if i in raw]


def get_labels(svc, ids):
    """message id -> set of label ids (None if the message no longer exists)."""
    raw = _batch_get(svc, ids, format="minimal")
    return {i: set(raw[i].get("labelIds", [])) if i in raw else None for i in ids}


def sent_recipients(svc, query):
    """(addresses I have written to, thread ids I have sent in, message count) for sent mail matching query."""
    pairs = list_ids(svc, query, 20000, with_threads=True)
    ids = [p[0] for p in pairs]
    threads = {p[1] for p in pairs}
    addrs = set()

    def cb(rid, resp, exc):
        if exc is not None:
            return
        for h in resp.get("payload", {}).get("headers", []):
            if h["name"].lower() in ("to", "cc", "bcc"):
                for _, a in getaddresses([h["value"]]):
                    if a:
                        addrs.add(a.lower())

    for i in range(0, len(ids), 40):
        b = svc.new_batch_http_request(callback=cb)
        for mid in ids[i:i + 40]:
            b.add(svc.users().messages().get(
                userId="me", id=mid, format="metadata", metadataHeaders=["To", "Cc", "Bcc"]))
        backoff(b.execute)
    return addrs, threads, len(ids)


# ---------- parsing ----------

@dataclass
class Mail:
    id: str
    thread_id: str
    labels: set
    from_addr: str
    from_name: str
    to: str
    cc: str
    subject: str
    headers: dict = field(default_factory=dict)
    body: str = ""
    link_domains: list = field(default_factory=list)
    has_ics: bool = False

    @property
    def domain(self):
        return self.from_addr.rsplit("@", 1)[-1] if "@" in self.from_addr else ""

    def _addrs(self, *header_names):
        found = set()
        for n in header_names:
            for _, a in getaddresses([self.headers.get(n, "")]):
                if a:
                    found.add(a.lower())
        return found

    @property
    def recipients(self):
        """Everyone visibly addressed (To + Cc)."""
        return self._addrs("to", "cc")

    @property
    def participants(self):
        """Everyone visible on the message: sender, reply-to, To, Cc."""
        return self._addrs("from", "reply-to", "to", "cc")

    def mine(self, me):
        """My addresses, plus whichever address this message was actually delivered to (covers aliases)."""
        return me | self._addrs("delivered-to", "x-original-to")

    def only_to_me(self, me):
        """True when I am the only visible recipient (no one else in To/Cc)."""
        r = self.recipients
        return bool(r) and r <= self.mine(me)

    @property
    def dmarc(self):
        """'pass' / 'fail' / 'none' as judged by Gmail for the From domain."""
        m = re.search(r"dmarc=(\w+)", self.headers.get("authentication-results", ""), re.I)
        return m.group(1).lower() if m else "none"


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.links, self._skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)
        if tag in ("br", "p", "div", "tr", "li"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def _decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _walk(part, plain, html, flags):
    mt = part.get("mimeType", "")
    if mt == "text/calendar" or part.get("filename", "").lower().endswith(".ics"):
        flags["ics"] = True
    data = part.get("body", {}).get("data")
    if data and mt == "text/plain":
        plain.append(_decode(data))
    elif data and mt == "text/html":
        html.append(_decode(data))
    for p in part.get("parts", []) or []:
        _walk(p, plain, html, flags)


def _domains(urls):
    seen = []
    for u in urls:
        m = re.match(r"https?://([^/:?#]+)", u, re.I)
        if m and m.group(1).lower() not in seen:
            seen.append(m.group(1).lower())
    return seen


def parse_message(raw):
    payload = raw.get("payload", {})
    h = {x["name"].lower(): x["value"] for x in payload.get("headers", [])}
    name, addr = parseaddr(h.get("from", ""))
    plain, html, flags = [], [], {"ics": False}
    _walk(payload, plain, html, flags)
    links = []
    if plain:
        text = "\n".join(plain)
        links = re.findall(r"https?://[^\s<>\"')]+", text)
    else:
        p = _Text()
        p.feed("\n".join(html))
        text, links = "".join(p.out), p.links
    text = re.sub(r"https?://\S+", " ", text)           # raw URLs add noise; domains are listed separately
    text = re.sub(r"[ \t ​͏]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return Mail(
        id=raw["id"], thread_id=raw["threadId"], labels=set(raw.get("labelIds", [])),
        from_addr=addr.lower(), from_name=name, to=h.get("to", ""), cc=h.get("cc", ""),
        subject=h.get("subject", ""), headers=h, body=text, link_domains=_domains(links)[:8],
        has_ics=flags["ics"],
    )
