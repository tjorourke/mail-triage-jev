"""Needs my reply: customer threads where the customer's latest email asks me something and nobody on my side has
answered. Labels the latest message AI/Needs-Reply, adds AI/Reply-Overdue after a while, and clears both when the
thread moves on (I or a colleague reply, or I archive it)."""
import json
import logging
import time
from email.utils import getaddresses

from . import customers, gmail, rules

log = logging.getLogger("triage")


def _addrs(headers, *names):
    return {a.lower() for n in names for _, a in getaddresses([headers.get(n, "")]) if a}


def _own(cfg, addr):
    own = {d.lower() for d in cfg["own_domains"]}
    return addr.rsplit("@", 1)[-1] in own


def label_names(cfg):
    nr = cfg["needs_reply"]
    return [nr["label"], nr["overdue_label"]]


def _state(cfg, thread, me, cust):
    """('replied'|'colleague'|'customer', latest message) for a thread's latest message."""
    latest = thread[-1]
    h = latest["headers"]
    sender = next(iter(_addrs(h, "from")), "")
    if "SENT" in latest["labels"] or sender in me:
        return "replied", latest
    if _own(cfg, sender):
        return "colleague", latest      # a colleague wrote last: it is being handled
    dom = customers.registrable(sender.rsplit("@", 1)[-1]) if "@" in sender else ""
    if dom in cust:
        return "customer", latest
    return "other", latest


def evaluate(svc, db, cfg, lab, clf, me, dry_run=False, verbose=False):
    """Refresh the needs-reply labels. Returns (open count, overdue count, newly added)."""
    nr = cfg["needs_reply"]
    if not nr["enabled"] or not cfg["customers"]["enabled"]:
        return 0, 0, 0
    cust = db.customers()
    parent_id = lab.get(cfg["customers"]["parent_label"])
    nr_id, od_id = lab[nr["label"]], lab[nr["overdue_label"]]
    if not parent_id:
        return 0, 0, 0
    now = int(time.time())
    # customer threads still in the inbox
    pairs = gmail.list_ids_with_labels(svc, [parent_id, "INBOX"], f"newer_than:{nr['max_age_days']}d")
    thread_ids = sorted({t for _, t in pairs})
    threads = gmail.get_threads_meta(svc, thread_ids)
    still_open, overdue_n, added = set(), 0, 0
    to_add, to_remove = {}, {}      # (labels) -> message ids

    for tid, thread in threads.items():
        if not thread:
            continue
        state, latest = _state(cfg, thread, me, cust)
        row = db.get_needs_reply(tid)
        needs, p = False, None
        if state == "customer" and "INBOX" in latest["labels"]:
            h = latest["headers"]
            directed_at_me = bool(_addrs(h, "to", "cc") & (me | _addrs(h, "delivered-to")))
            if row and row["message_id"] == latest["id"]:
                needs, p = bool(row["needs"]), row["p"]          # already judged this exact email
            elif directed_at_me:
                m = gmail.get_full(svc, [latest["id"]])[0]
                if rules.eligible_for_task_check(m, me):
                    p = clf.action_prob(m, me, True, any("SENT" in x["labels"] for x in thread))
                    needs = p >= nr["threshold"]
                    if verbose:
                        log.info("   thread %s: customer wrote last, p(ask)=%.2f -> %s | %s", tid[:8], p,
                                 "NEEDS REPLY" if needs else "no", h.get("subject", "")[:60])
        old = set(json.loads(row["labeled"])) if row and row["labeled"] else set()
        if needs:
            still_open.add(tid)
            age_h = (now - latest["ts"]) / 3600
            want = {latest["id"]}
            add = [nr_id] + ([od_id] if age_h >= nr["overdue_hours"] else [])
            if age_h >= nr["overdue_hours"]:
                overdue_n += 1
            to_add.setdefault(tuple(add), []).append(latest["id"])
            if not (row and row["needs"] and row["message_id"] == latest["id"]):
                added += 1
                log.info("needs reply: %s | %s (%.1fh old)", next(iter(_addrs(latest["headers"], "from")), "?"),
                         latest["headers"].get("subject", "")[:60], age_h)
            for mid in old - want:                              # earlier message of the thread: drop its labels
                to_remove.setdefault((nr_id, od_id), []).append(mid)
            if not dry_run:
                db.set_needs_reply(tid, latest["id"], True, p, latest["ts"], json.dumps(sorted(want)))
        else:
            for mid in old:
                to_remove.setdefault((nr_id, od_id), []).append(mid)
            if not dry_run and (row or p is not None):
                db.set_needs_reply(tid, latest["id"], False, p, latest["ts"], "[]")

    # threads that were open but have left the candidate list (archived, or no longer in the inbox): clear them
    for row in db.open_needs_reply():
        if row["thread_id"] not in threads:
            for mid in json.loads(row["labeled"] or "[]"):
                to_remove.setdefault((nr_id, od_id), []).append(mid)
            if not dry_run:
                db.set_needs_reply(row["thread_id"], row["message_id"], False, row["p"], row["last_ts"], "[]")

    if not dry_run:
        for labels, ids in to_add.items():
            gmail.modify(svc, ids, add=list(labels))
        for labels, ids in to_remove.items():
            gmail.modify(svc, ids, remove=list(labels))
    return len(still_open), overdue_n, added
