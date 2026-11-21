"""IMAP backend for the Gmail sender-triage UI.

UI-agnostic helpers: connect, discover the Trash mailbox, build per-sender
counts from Inbox From-headers, and delete (move-to-trash or permanent) all
Inbox mail from given senders.
"""

import imaplib
import re
from collections import Counter
from email.header import decode_header, make_header
from email.utils import parseaddr

IMAP_HOST = "imap.gmail.com"
FETCH_CHUNK = 500
UID_BATCH = 100
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
SENDER_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
FROM_HDR_RE = re.compile(rb"(?im)^From:[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*")
CT_HDR_RE = re.compile(rb"(?im)^Content-Type:[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*")


class CancelledError(Exception):
    """Raised inside a long-running operation when the user cancels."""


def connect(username, password):
    """Log in to Gmail IMAP and select the Inbox. Returns the IMAP object."""
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        imap.login(username, password)
        imap.select("INBOX")
    except Exception:
        try:
            imap.logout()
        except Exception:
            pass
        raise
    return imap


def disconnect(imap):
    try:
        imap.close()
    except Exception:
        pass
    try:
        imap.logout()
    except Exception:
        pass


def _parse_list_line(line):
    """Return (flags, mailbox_name) from one LIST response line."""
    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
    m = re.match(r"\(([^)]*)\)\s+\"?[^\" ]*\"?\s+(.+)$", text)
    if not m:
        return "", text.strip().strip('"')
    flags, name = m.group(1), m.group(2).strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"')
    return flags, name


def find_trash_mailbox(imap):
    """Find the server's Trash mailbox via the \\Trash special-use flag.

    Falls back to "[Gmail]/Trash" if no flagged folder is found.
    """
    typ, boxes = imap.list()
    if typ == "OK":
        for box in boxes:
            flags, name = _parse_list_line(box)
            if "\\trash" in flags.lower():
                return name
    return "[Gmail]/Trash"


def extract_sender(raw_header):
    """Pull a clean, lowercased email address out of a raw 'From:' header."""
    if isinstance(raw_header, bytes):
        text = raw_header.decode("utf-8", errors="replace")
    else:
        text = str(raw_header)
    if ":" in text:
        text = text.split(":", 1)[1]
    text = " ".join(text.split())  # collapse header folding/whitespace
    try:
        text = str(make_header(decode_header(text)))
    except Exception:
        pass
    addr = parseaddr(text)[1]
    if "@" in addr:
        return addr.strip().lower()
    m = EMAIL_RE.search(text)
    return m.group(0).lower() if m else None


def _has_attachment(blob):
    """Heuristic: does this message carry an attachment?

    Based on the top-level Content-Type: multipart/mixed, or a non-text,
    non-multipart single part (e.g. application/pdf, message/rfc822).
    Matches what users mean by "has attachment" in the vast majority of
    mail; pure inline images in multipart/related are not counted.
    """
    if isinstance(blob, str):
        blob = blob.encode("utf-8", errors="replace")
    m = CT_HDR_RE.search(blob)
    if not m:
        return False
    value = m.group(0).split(b":", 1)[1].decode("utf-8", errors="replace")
    base = value.split(";", 1)[0].strip().lower()
    if base.startswith("multipart/mixed"):
        return True
    return bool(base) and "/" in base and \
        not base.startswith(("text/", "multipart/"))


def fetch_sender_counts(imap, progress_cb=None, cancel=None):
    """Count Inbox messages per sender via batched header fetches.

    Returns (sender counts, per-sender attachment counts, total messages)
    as (Counter, Counter, int). progress_cb(done, total) is called after
    every chunk; cancel() -> True aborts.
    """
    typ, data = imap.search(None, "ALL")
    if typ != "OK":
        raise RuntimeError("SEARCH ALL failed")
    ids = data[0].split() if data and data[0] else []
    total = len(ids)
    counts, att_counts = Counter(), Counter()
    for start in range(0, total, FETCH_CHUNK):
        if cancel and cancel():
            raise CancelledError()
        chunk = b",".join(ids[start:start + FETCH_CHUNK])
        typ, data = imap.fetch(
            chunk, "(BODY.PEEK[HEADER.FIELDS (FROM CONTENT-TYPE)])")
        if typ != "OK":
            raise RuntimeError(f"FETCH failed for messages starting at {start}")
        for item in data:
            if isinstance(item, tuple) and len(item) > 1 and item[1]:
                blob = item[1]
                m = FROM_HDR_RE.search(blob)
                sender = extract_sender(m.group(0) if m else blob)
                if sender:
                    counts[sender] += 1
                    if _has_attachment(blob):
                        att_counts[sender] += 1
        if progress_cb:
            progress_cb(min(start + FETCH_CHUNK, total), total)
    return counts, att_counts, total


def _mailbox_arg(name):
    """Quote an IMAP mailbox name if it contains specials."""
    if re.search(r'[ ()"]', name):
        return '"{}"'.format(name.replace('"', '\\"'))
    return name


def _uidset_to_list(text):
    """Expand an IMAP UID set string ('1,3:5') to a list of ints."""
    out = []
    for part in text.split(","):
        part = part.strip()
        if ":" in part:
            lo, hi = part.split(":", 1)
            out.extend(range(int(lo), int(hi) + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


def _copy_dst_uids(imap):
    """Destination UIDs from the latest COPYUID untagged response.

    Returns a list of ints, or None when the server does not support
    UIDPLUS (no COPYUID sent).
    """
    responses = imap.untagged_responses.get("COPYUID")
    if not responses:
        return None
    last = responses[-1]
    if isinstance(last, bytes):
        last = last.decode("ascii", "replace")
    m = re.search(r"\d+\s+([\d,:]+)\s+([\d,:]+)", last or "")
    return _uidset_to_list(m.group(2)) if m else None


def _fetch_gmids(imap, uids):
    """X-GM-MSGID of each UID - Gmail's stable id, same across labels."""
    typ, data = imap.uid("FETCH", b",".join(uids), "(X-GM-MSGID)")
    if typ != "OK":
        return []
    ids = []
    for item in data:
        if not isinstance(item, tuple):
            continue
        head = item[0]
        if isinstance(head, bytes):
            head = head.decode("ascii", "replace")
        m = re.search(r"X-GM-MSGID\s+(\d+)", str(head))
        if m:
            ids.append(m.group(1))
    return ids


def _exact_uids(imap, candidates, sender):
    """Keep only UIDs whose From address is exactly `sender`.

    IMAP SEARCH FROM is a substring match on the raw From header (RFC
    3501 6.4.4), so without this check mail from joann@x.com or from a
    display name containing the address would be deleted too.
    """
    exact = []
    for j in range(0, len(candidates), UID_BATCH):
        batch = b",".join(candidates[j:j + UID_BATCH])
        typ, data = imap.uid("FETCH", batch,
                             "(BODY.PEEK[HEADER.FIELDS (FROM)])")
        if typ != "OK":
            raise RuntimeError("UID FETCH headers failed")
        for item in data:
            if not (isinstance(item, tuple) and len(item) > 1 and item[1]):
                continue
            head = item[0]
            if isinstance(head, bytes):
                head = head.decode("ascii", "replace")
            um = re.search(r"UID\s+(\d+)", str(head))
            m = FROM_HDR_RE.search(item[1])
            if um and m and extract_sender(m.group(0)) == sender:
                exact.append(um.group(1).encode())
    return exact


def _purge_from_trash(imap, trash_mailbox, dst_uids, gmids):
    """Expunge the just-copied messages from the Trash mailbox itself.

    Gmail's default IMAP setting archives a message when it is expunged
    from the last visible folder, so \\Deleted+EXPUNGE on INBOX alone
    leaves the mail in All Mail. Expunging the Trash copies removes the
    message permanently regardless of that setting.
    """
    typ, _ = imap.select(_mailbox_arg(trash_mailbox))
    if typ != "OK":
        raise RuntimeError("select Trash failed")
    try:
        uids = dst_uids or []
        if not uids and gmids:
            for gmid in gmids:
                typ, data = imap.uid("SEARCH", None, "X-GM-MSGID", gmid)
                if typ == "OK" and data and data[0]:
                    uids.extend(int(u) for u in data[0].split())
        if uids:
            batch = b",".join(str(u).encode() for u in uids)
            typ, _ = imap.uid("STORE", batch, "+FLAGS", "(\\Deleted)")
            if typ != "OK":
                raise RuntimeError("UID STORE \\Deleted (trash) failed")
        imap.expunge()
    finally:
        imap.select("INBOX")


def delete_senders(imap, senders, permanent=False, trash_mailbox=None,
                   progress_cb=None, cancel=None):
    """Delete all Inbox mail from each sender in `senders`.

    Both modes only touch mail whose From address is exactly the marked
    sender - never substring matches.

    permanent=False -> UID COPY to the trash mailbox, mark \\Deleted in
                       Inbox, expunge (recoverable from Trash ~30 days).
    permanent=True  -> copy to Trash, expunge the Trash copies too -
                       the mail is gone for good.

    Returns ({sender: deleted_count}, [failed_senders]).
    progress_cb(done, total) is called per sender; cancel() -> True aborts.
    """
    if not trash_mailbox:
        trash_mailbox = find_trash_mailbox(imap)
    done, failed = {}, []
    for i, sender in enumerate(senders):
        if cancel and cancel():
            raise CancelledError()
        try:
            if not SENDER_RE.fullmatch(sender.strip()):
                raise ValueError("invalid sender address")
            typ, data = imap.uid("SEARCH", None, "FROM", f'"{sender}"')
            if typ != "OK":
                raise RuntimeError("UID SEARCH failed")
            candidates = data[0].split() if data and data[0] else []
            uids = _exact_uids(imap, candidates, sender)
            moved = 0
            for j in range(0, len(uids), UID_BATCH):
                if cancel and cancel():
                    raise CancelledError()
                batch = uids[j:j + UID_BATCH]
                uidset = b",".join(batch)
                typ, _ = imap.uid("COPY", uidset, _mailbox_arg(trash_mailbox))
                if typ != "OK":
                    raise RuntimeError("UID COPY to trash failed")
                dst_uids = _copy_dst_uids(imap) if permanent else None
                gmids = _fetch_gmids(imap, batch) \
                    if permanent and not dst_uids else []
                typ, _ = imap.uid("STORE", uidset, "+FLAGS", "(\\Deleted)")
                if typ != "OK":
                    raise RuntimeError("UID STORE \\Deleted failed")
                if permanent:
                    # Expunge the Inbox copies FIRST (under Gmail's default
                    # 'archive on expunge' setting this just removes the
                    # Inbox label), then expunge the Trash copies - only
                    # once the last visible copy is gone is the mail
                    # actually deleted rather than archived to All Mail.
                    imap.expunge()
                    _purge_from_trash(imap, trash_mailbox, dst_uids, gmids)
                moved += len(batch)
            done[sender] = moved
        except CancelledError:
            raise
        except Exception:
            failed.append(sender)
        if progress_cb:
            progress_cb(i + 1, len(senders))
    imap.expunge()
    return done, failed
