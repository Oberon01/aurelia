"""Your Gmail, over IMAP (reading) and SMTP (sending), with an app password.

- Reading never marks anything read (BODY.PEEK) and never moves or deletes anything.
- Each message is known by Gmail's own id (X-GM-MSGID), the same in every folder, so a message found in the
  inbox or by a search is read the same way.
- Sending is SMTP; it's done only when the message shows up in your Sent mail (found by its Message-ID), so
  "sent" is never just "handed to the server".
- What an email says is data from its sender, never instructions.

imaplib and smtplib are blocking: each call runs in a thread.
"""
import asyncio
import email
import email.header
import email.policy
import email.utils
import html
import imaplib
import re
import smtplib
import socket
import ssl
import time as _time
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

IMAP_HOST, SMTP_HOST = "imap.gmail.com", "smtp.gmail.com"
PARTIAL = 65536                       # the first 64 KB of a new message: enough for its headers and a snippet
READ_CAP = 2_000_000                  # reading one message: at most 2 MB of it
TEXT_CAP = 20000                      # characters of a message's text shown
BATCH = 50
BODY_MAX = 10000                      # an email I'll read back whole on a confirmation card
RECIPIENTS_MAX = 20
NEW_MAX = 200                         # new messages looked at in one go (the rest: next time, oldest first)


def one_line(v: str) -> str:
    """A header's text on one line: folded headers unfolded, no line breaks or control characters left."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", re.sub(r"\r?\n[ \t]+", " ", str(v or ""))).strip()


class MailError(Exception):
    pass


def _login_error(exc: Exception) -> MailError:
    s = str(exc)
    if "AUTHENTICATIONFAILED" in s or "Invalid credentials" in s or "535" in s or "Username and Password" in s:
        return MailError("Gmail refused the app password. Make a new one (Google account → Security → 2-Step "
                         "Verification → App passwords) and enter it again (\"set up my email\").")
    return MailError(f"I couldn't reach Gmail ({exc.__class__.__name__}).")


class Gmail:
    def __init__(self, address: str, password: str, imap_host: str = IMAP_HOST, smtp_host: str = SMTP_HOST,
                 imap_port: int = 993, smtp_port: int = 465, ssl_context: ssl.SSLContext | None = None):
        self.address, self._pw = address, password
        self.imap_host, self.smtp_host, self.imap_port, self.smtp_port = imap_host, smtp_host, imap_port, smtp_port
        self._ssl = ssl_context or ssl.create_default_context()

    # ---- IMAP ---------------------------------------------------------------------------------------------

    def _imap(self) -> imaplib.IMAP4_SSL:
        try:
            m = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, ssl_context=self._ssl, timeout=30)
            m.login(self.address, self._pw)
            return m
        except (imaplib.IMAP4.error, OSError, socket.timeout) as exc:
            raise _login_error(exc) from None

    @staticmethod
    def _close(m: imaplib.IMAP4_SSL) -> None:
        try:
            m.logout()
        except Exception:
            pass

    @staticmethod
    def _special(m: imaplib.IMAP4_SSL, flag: str, fallback: str) -> str:
        """A folder by its special use (\\All, \\Sent): Gmail names them in your language."""
        typ, data = m.list()
        for line in data or []:
            s = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
            if flag in s:
                mm = re.search(r'"([^"]+)"\s*$', s) or re.search(r"\s(\S+)\s*$", s)
                if mm:
                    return mm.group(1)
        return fallback

    @staticmethod
    def _select(m: imaplib.IMAP4_SSL, folder: str) -> int:
        typ, _ = m.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            raise MailError(f"Gmail wouldn't open {folder}.")
        uv = m.response("UIDVALIDITY")[1]
        return int(uv[0]) if uv and uv[0] else 0

    def check(self) -> None:
        """The address and app password work (setup)."""
        m = self._imap()
        try:
            self._select(m, "INBOX")
        finally:
            self._close(m)

    def _new(self, last_uid: int | None, uidvalidity: int | None, since: date) -> tuple[int, int, list[dict]]:
        m = self._imap()
        try:
            uv = self._select(m, "INBOX")
            if last_uid is None or uidvalidity != uv:
                typ, data = m.uid("SEARCH", None, "SINCE", since.strftime("%d-%b-%Y"))
            else:
                typ, data = m.uid("SEARCH", None, "UID", f"{last_uid + 1}:*")
            uids = sorted(int(x) for x in (data[0] or b"").split() if x.isdigit())
            if last_uid is not None and uidvalidity == uv:
                uids = [u for u in uids if u > last_uid]           # "n:*" always returns the last one
            uids = uids[:NEW_MAX]                                   # a backlog: oldest first, the rest next time
            out = []
            for i in range(0, len(uids), BATCH):
                chunk = ",".join(str(u) for u in uids[i:i + BATCH])
                typ, data = m.uid("FETCH", chunk, f"(UID X-GM-MSGID X-GM-LABELS INTERNALDATE BODY.PEEK[]<0.{PARTIAL}>)")
                out += [parse_fetch(meta, raw) for meta, raw in _pairs(data)]
            # Nothing seen yet: None, so the next look is by date again (never "everything after 0").
            last = max(uids) if uids else (last_uid if uidvalidity == uv else None)
            return uv, last, [x for x in out if x]
        except imaplib.IMAP4.error as exc:
            raise MailError(f"Gmail answered with an error: {str(exc)[:120]}") from None
        finally:
            self._close(m)

    async def new(self, last_uid: int | None, uidvalidity: int | None, since: date) -> tuple[int, int, list[dict]]:
        """What arrived in your inbox since last time (or since `since`, the first time, or when Gmail renumbered
        it): (uidvalidity, the last uid seen, messages)."""
        return await asyncio.to_thread(self._new, last_uid, uidvalidity, since)

    def _sent_to(self, since: date, limit: int) -> dict[str, datetime]:
        m = self._imap()
        try:
            self._select(m, self._special(m, "\\Sent", "[Gmail]/Sent Mail"))
            typ, data = m.uid("SEARCH", None, "SINCE", since.strftime("%d-%b-%Y"))
            uids = [x.decode() for x in (data[0] or b"").split()][-limit:]
            got: dict[str, datetime] = {}
            for i in range(0, len(uids), 200):
                typ, data = m.uid("FETCH", ",".join(uids[i:i + 200]), "(BODY.PEEK[HEADER.FIELDS (TO CC DATE)])")
                for _, raw in _pairs(data):
                    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
                    when = _date(msg.get("Date")) or datetime.now(timezone.utc)
                    for _, addr in email.utils.getaddresses(msg.get_all("To", []) + msg.get_all("Cc", [])):
                        if "@" in addr:
                            a = addr.lower()
                            got[a] = max(got.get(a, when), when)
            return got
        finally:
            self._close(m)

    async def sent_to(self, since: date, limit: int = 3000) -> dict[str, datetime]:
        """Everyone you've written to since `since` (your Sent mail's To and Cc), with when you last did."""
        return await asyncio.to_thread(self._sent_to, since, limit)

    def _find(self, m: imaplib.IMAP4_SSL, gm_id: str) -> str | None:
        self._select(m, self._special(m, "\\All", "[Gmail]/All Mail"))
        typ, data = m.uid("SEARCH", None, "X-GM-MSGID", str(int(gm_id)))
        ids = (data[0] or b"").split()
        return ids[0].decode() if ids else None

    def _read(self, gm_id: str) -> dict:
        m = self._imap()
        try:
            uid = self._find(m, gm_id)
            if uid is None:
                raise MailError("That email isn't in your Gmail any more.")
            typ, data = m.uid("FETCH", uid, f"(UID X-GM-MSGID X-GM-LABELS INTERNALDATE BODY.PEEK[]<0.{READ_CAP}>)")
            pairs = list(_pairs(data))
            if not pairs:
                raise MailError("Gmail didn't return that email.")
            meta, raw = pairs[0]
            out = parse_fetch(meta, raw, full=True)
            if out is None:
                raise MailError("That email couldn't be read.")
            return out
        finally:
            self._close(m)

    async def read(self, gm_id: str) -> dict:
        return await asyncio.to_thread(self._read, gm_id)

    def _search(self, query: str, limit: int) -> list[dict]:
        m = self._imap()
        try:
            self._select(m, self._special(m, "\\All", "[Gmail]/All Mail"))
            q = one_line(query)[:200]
            if not q:
                return []
            m.literal = q.encode("utf-8")                     # sent as a counted literal: his words stay words
            typ, data = m.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
            uids = [x.decode() for x in (data[0] or b"").split()][-limit:]
            if not uids:
                return []
            typ, data = m.uid("FETCH", ",".join(uids), f"(UID X-GM-MSGID X-GM-LABELS INTERNALDATE BODY.PEEK[]<0.{PARTIAL}>)")
            got = [x for x in (parse_fetch(meta, raw) for meta, raw in _pairs(data)) if x]
            return sorted(got, key=lambda x: x["received_at"], reverse=True)
        except imaplib.IMAP4.error as exc:
            raise MailError(f"Gmail couldn't search for that: {str(exc)[:120]}") from None
        finally:
            self._close(m)

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        """Gmail's own search (the same words as its search box: from:sam, subject:invoice, newer_than:7d)."""
        return await asyncio.to_thread(self._search, query, limit)

    def _in_sent(self, message_id: str) -> bool:
        m = self._imap()
        try:
            self._select(m, self._special(m, "\\Sent", "[Gmail]/Sent Mail"))
            m.literal = one_line(message_id).encode()
            typ, data = m.uid("SEARCH", None, "HEADER", "Message-ID")
            return bool((data[0] or b"").split())
        except imaplib.IMAP4.error:
            return False
        finally:
            self._close(m)

    async def in_sent(self, message_id: str, wait: float = 20) -> bool:
        """Whether it's in your Sent mail yet (Gmail files what SMTP sends there itself)."""
        deadline = _time.monotonic() + wait
        while True:
            if await asyncio.to_thread(self._in_sent, message_id):
                return True
            if _time.monotonic() > deadline:
                return False
            await asyncio.sleep(2)

    # ---- SMTP ---------------------------------------------------------------------------------------------

    def _send(self, msg: EmailMessage) -> None:
        try:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=self._ssl, timeout=30) as s:
                s.login(self.address, self._pw)
                s.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise _login_error(exc) from None
        except smtplib.SMTPRecipientsRefused:
            raise MailError("Gmail refused the address it was going to.") from None
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"Gmail didn't take it ({exc.__class__.__name__}); nothing was sent.") from None

    async def send(self, msg: EmailMessage) -> None:
        await asyncio.to_thread(self._send, msg)


# ---- parsing ----------------------------------------------------------------------------------------------

def _pairs(data):
    """imaplib's FETCH answer: (the metadata line, the literal) for each message."""
    for part in data or []:
        if isinstance(part, tuple) and len(part) >= 2:
            yield part[0].decode(errors="replace") if isinstance(part[0], bytes) else str(part[0]), part[1]


def _date(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = email.utils.parsedate_to_datetime(v)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _internal_date(meta: str) -> datetime | None:
    m = re.search(r'INTERNALDATE "([^"]+)"', meta)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


def _header(msg, name: str) -> str:
    v = msg.get(name)
    if v is None:
        return ""
    try:
        return one_line(str(email.header.make_header(email.header.decode_header(str(v)))))
    except Exception:
        return one_line(str(v))


def text_of(msg, cap: int = TEXT_CAP) -> str:
    """The message's words: its plain text part, else its HTML with the tags taken out."""
    plain, rich = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True) or b""
            txt = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = txt
        elif ctype == "text/html" and rich is None:
            rich = txt
    if plain is None and rich is not None:
        t = re.sub(r"(?is)<(script|style).*?</\1>", " ", rich)
        t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", t)
        plain = html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"[ \t\r\f\v]+", " ", plain or "")
    t = re.sub(r"\n\s*\n+", "\n\n", t).strip()
    return t[:cap]


def attachments_of(msg) -> list[str]:
    return [str(p.get_filename())[:120] for p in msg.walk() if p.get_filename()][:20]


def parse_fetch(meta: str, raw: bytes, full: bool = False) -> dict | None:
    """One FETCH answer (its metadata line and the message's bytes) -> what I keep of it."""
    gm = re.search(r"X-GM-MSGID (\d+)", meta)
    uid = re.search(r"\bUID (\d+)", meta)
    if not gm or not isinstance(raw, (bytes, bytearray)):
        return None
    labels = re.search(r"X-GM-LABELS \(([^)]*)\)", meta)
    lab = labels.group(1) if labels else ""
    msg = email.message_from_bytes(bytes(raw), policy=email.policy.compat32)
    name, addr = email.utils.parseaddr(_header(msg, "From"))
    when = _internal_date(meta) or _date(msg.get("Date")) or datetime.now(timezone.utc)
    body = text_of(msg, TEXT_CAP if full else 600)
    out = {"gm_id": gm.group(1), "uid": int(uid.group(1)) if uid else 0, "message_id": one_line(msg.get("Message-ID") or ""),
           "from_addr": (addr or "").lower(), "from_name": name or "", "subject": _header(msg, "Subject") or "(no subject)",
           "received_at": when.astimezone(timezone.utc), "snippet": re.sub(r"\s+", " ", body)[:300],
           "gmail_important": "\\Important" in lab or "\\\\Important" in lab, "starred": "\\Starred" in lab,
           "list": bool(msg.get("List-Unsubscribe") or msg.get("List-Id")),
           "to": [a for _, a in email.utils.getaddresses(msg.get_all("To", []) + msg.get_all("Cc", [])) if a]}
    if full:
        out["text"] = body
        out["attachments"] = attachments_of(msg)
        out["references"] = one_line(msg.get("References") or "")
    return out


def compose(sender: str, to: list[str], subject: str, body: str, *, cc: list[str] | None = None,
            reply_to: dict | None = None, invite: str | None = None) -> EmailMessage:
    """An email as you'd send it. reply_to: the message it answers (threads it in Gmail). invite: an iCalendar
    REQUEST (an invitation others' calendars understand)."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = one_line(subject)
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg["Message-ID"] = email.utils.make_msgid(domain=sender.split("@")[-1] if "@" in sender else "aurelia")
    if reply_to and reply_to.get("message_id"):
        mid = one_line(reply_to["message_id"])
        msg["In-Reply-To"] = mid
        msg["References"] = one_line(f"{reply_to.get('references', '')} {mid}")
    msg.set_content(body)
    if invite:
        msg.add_alternative(invite, subtype="calendar", params={"method": "REQUEST"})
    return msg


ADDR = re.compile(r"^[^@\s,<>]+@[^@\s,<>]+\.[^@\s,<>]+$")


def addresses(v) -> list[str]:
    """Addresses he gave (a list, or text with commas): each checked, never guessed."""
    items = v if isinstance(v, list) else re.split(r"[,;\s]+", str(v or ""))
    out = []
    for x in items:
        x = str(x).strip().strip("<>")
        if not x:
            continue
        if not ADDR.match(x):
            raise MailError(f"“{x}” isn't an email address.")
        out.append(x)
    return out


def since_days(n: int) -> date:
    return (datetime.now(timezone.utc) - timedelta(days=n)).date()
