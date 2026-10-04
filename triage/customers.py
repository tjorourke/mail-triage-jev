"""Customer and prospect labels: one Gmail label per customer domain ("Customers/<domain>"), nested under a parent.

Teach it by putting the parent label on any email from a new company (or by creating "Customers/<domain>" by hand).
Delete a customer's label to remove that customer."""
import logging
import time
from email.utils import getaddresses

import tldextract

from . import gmail

log = logging.getLogger("triage")
_TLD = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)   # bundled list, no network
COLOR = {"textColor": "#ffffff", "backgroundColor": "#4986e7"}      # blue
RESYNC_SECS = 7 * 86400


def registrable(host):
    """mail.acme.example -> acme.example, www.globex.co.uk -> globex.co.uk"""
    e = _TLD(host)
    return (getattr(e, "top_domain_under_public_suffix", None) or e.registered_domain or host).lower()


def domains_of(addresses):
    return {registrable(a.rsplit("@", 1)[-1]) for a in addresses if "@" in a}


def external_domains(cfg, domains):
    """Drop my own company, free-mail providers and anything on the ignore list."""
    skip = {registrable(d) for d in cfg["own_domains"]} | {d.lower() for d in cfg["customers"]["ignore_domains"]}
    return {d for d in domains if d and d not in skip}


def label_name(cfg, domain):
    return f'{cfg["customers"]["parent_label"]}/{domain}'


def ensure_parent(svc, cfg):
    parent = cfg["customers"]["parent_label"]
    return gmail.ensure_labels(svc, [parent], {parent: COLOR})[parent]


def add_customer(svc, db, cfg, domain, source):
    """Learn a customer domain: create its label now (the first mailbox sweep happens in sync())."""
    name = label_name(cfg, domain)
    lid = gmail.ensure_labels(svc, [name], {name: COLOR})[name]
    db.add_customer(domain, name, lid, source)
    log.info("customers: added %s (%s)", domain, source)


def labels_for(m, customers, parent_id):
    """Labels to add to a message because someone on it is at a customer: parent + one per customer domain."""
    hit = [customers[d]["label_id"] for d in domains_of(m.participants) if d in customers]
    return [parent_id] + hit if hit else []


def _header_domains(cfg, h):
    """Domains to learn from a message the user labelled: the sender's if external, otherwise the recipients'."""
    def addrs(*names):
        return [a.lower() for n in names for _, a in getaddresses([h.get(n, "")]) if a]
    sender = external_domains(cfg, domains_of(addrs("from")))
    return sender or external_domains(cfg, domains_of(addrs("to", "cc")))


def sweep(svc, db, cfg, domain, parent_id, limit=5000):
    """Label every message in the mailbox to or from `domain` (and record what was labelled)."""
    row = db.customers()[domain]
    q = f"(from:@{domain} OR to:@{domain} OR cc:@{domain})"
    ids = set(gmail.list_ids(svc, q, limit))
    have = set(gmail.list_ids_by_label(svc, row["label_id"]))
    todo = sorted(ids - have)
    if todo:
        gmail.modify(svc, todo, add=[parent_id, row["label_id"]])
    db.mark_customer_labeled(ids | have)
    db.mark_customer_synced(domain)
    log.info("customers: %s: %d messages in the mailbox, %d newly labelled", domain, len(ids), len(todo))
    return len(todo)


def sync(svc, db, cfg):
    """Learn new customers from my labels, drop deleted ones, sweep new/old domains. Needs no AI model."""
    cc = cfg["customers"]
    if not cc["enabled"]:
        return
    parent = cc["parent_label"]
    parent_id = ensure_parent(svc, cfg)
    gl = {l["name"]: l["id"] for l in gmail.backoff(lambda: svc.users().labels().list(userId="me").execute())["labels"]}
    customers = db.customers()
    rejected = db.not_customers()

    # 1. a customer whose label I deleted is no longer a customer (and the AI will not re-add it)
    for d, row in list(customers.items()):
        if row["label"] not in gl:
            db.remove_customer(d, reject=True)
            customers.pop(d)
            rejected.add(d)
            log.info("customers: %s removed (its label was deleted)", d)

    # 2. seeds from config, and labels created by hand ("Customers/<domain>")
    for d in cc["seed_domains"]:
        d = registrable(d)
        if d not in customers and d not in rejected:
            add_customer(svc, db, cfg, d, "seed")
    for name in list(gl):
        if name.startswith(parent + "/"):
            d = name[len(parent) + 1:].lower()
            if "." in d and d not in customers and d not in rejected:
                db.add_customer(d, name, gl[name], "label")
                log.info("customers: added %s (label created by hand)", d)
    customers = db.customers()

    # 3. the parent label put on an email I labelled myself: learn that email's company
    mine = [i for i in gmail.list_ids_by_label(svc, parent_id) if i not in db.customer_labeled()]
    if mine:
        known_label = {}   # existing customer's label id -> messages that should also carry it
        for mid, h in gmail.get_headers(svc, mine).items():
            for d in _header_domains(cfg, h):
                if d in customers:
                    known_label.setdefault(customers[d]["label_id"], []).append(mid)
                elif d not in rejected:
                    add_customer(svc, db, cfg, d, "label-on-email")
                    customers = db.customers()
        for lid, ids in known_label.items():
            gmail.modify(svc, ids, add=[lid])
        # (these messages get their domain label in the sweep below; remember them either way)
        db.mark_customer_labeled(mine)

    # 4. sweep new domains, and re-sweep old ones weekly so new mail to/from them is never missed
    now = time.time()
    for d, row in db.customers().items():
        if not row["synced_at"] or now - row["synced_at"] > RESYNC_SECS:
            sweep(svc, db, cfg, d, parent_id)


def attribute_domain(cfg, m):
    """The external company this email is with, or None if unclear.

    If the sender is external it is the sender's company. If the sender is a colleague it is the one external
    company among the recipients (ambiguous mixes, e.g. a customer plus a partner on the same mail, are skipped)."""
    sender = external_domains(cfg, {registrable(m.domain)})
    if sender:
        return next(iter(sender))
    others = external_domains(cfg, domains_of(m.participants))
    return next(iter(others)) if len(others) == 1 else None


def colleague_on(cfg, m, me):
    """Is a colleague (anyone at my own company's domain, other than me) on the message?"""
    own = {d.lower() for d in cfg["own_domains"]}
    mine = m.mine(me)
    return any(a not in mine and a.rsplit("@", 1)[-1] in own for a in m.participants if "@" in a)
