import argparse
import fcntl
import logging
import logging.handlers
import re
import sys
import time
from collections import Counter

from . import customers, gmail, pause, rules
from .classify import Classifier, decide, junk_mass
from .config import load, read_list
from .db import DB

log = logging.getLogger("triage")


class Paused(Exception):
    pass
FEEDBACK_DAYS = 7
CONTACTS_REFRESH_SECS = 300
FEEDBACK_REFRESH_SECS = 30 * 60
CHUNK = 100   # emails fetched, classified and tagged at a time (keeps Gmail quota use gentle)


def setup_logging(cfg):
    cfg["log_dir"].mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.TimedRotatingFileHandler(cfg["log_dir"] / "triage.log", when="midnight", backupCount=14)
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


IMPORTANT_COLOR = {"textColor": "#ffffff", "backgroundColor": "#cc3a21"}   # red chip in the inbox list


def label_ids(svc, cfg):
    L = cfg["labels"]
    names, colors = [L["filtered"], L["would_filter"], L["newsletters"]], {}
    imp = cfg["important"]
    if imp["enabled"]:
        names.append(imp["label"])
        colors[imp["label"]] = IMPORTANT_COLOR
    if cfg["customers"]["enabled"]:
        names.append(cfg["customers"]["parent_label"])
        colors[cfg["customers"]["parent_label"]] = customers.COLOR
    return gmail.ensure_labels(svc, names, colors)


def important_labels(cfg, lab):
    """Labels added to an important message."""
    imp = cfg["important"]
    out = [lab[imp["label"]]]
    if imp["also_mark_gmail_important"]:
        out.append("IMPORTANT")
    if imp["star"]:
        out.append("STARRED")
    return out


def assess_importance(cfg, m, action, clf, me, probs=None, known=(False, False)):
    """-> (important, reason, probability). Only mail that is being kept can be important.

    VIP senders always are. Otherwise the AI must find a question/task aimed at me, AND the mail must look like a
    real person writing to me (not cold sales, marketing or an automated notice), which cuts out vendor pitches that
    happen to contain a question."""
    imp = cfg["important"]
    if not imp["enabled"] or action != "keep":
        return False, None, None
    if m.from_addr in {a.lower() for a in imp["vip"]} and m.dmarc != "fail":
        return True, "vip", None
    travel = rules.travel_reason(m, imp["travel_domains"])
    if travel:
        return True, travel, None
    if not rules.eligible_for_task_check(m, me):
        return False, None, None
    p = clf.action_prob(m, me, *known)
    if p < imp["threshold"]:
        return False, None, p
    if probs is None:
        probs = clf.classify(m, me, *known)
    if junk_mass(probs, cfg) >= imp["max_junk"] or probs.get("real_work_or_personal", 0) < imp["min_personal"]:
        return False, "not_personal", p
    return True, "question_or_task", p


# ---------- contacts (people I've emailed) ----------

def refresh_contacts(svc, db, force=False):
    last = float(db.get_meta("contacts_synced_at", 0))
    if not force and time.time() - last < CONTACTS_REFRESH_SECS:
        return
    q = "in:sent newer_than:365d" if last == 0 else f"in:sent after:{int(last) - 3600}"
    t = time.time()
    addrs, threads, n = gmail.sent_recipients(svc, q)
    db.add_contacts(addrs)
    db.add_sent_threads(threads)
    db.set_meta("contacts_synced_at", int(t))
    log.info("contacts: scanned %d sent messages, %d addresses (total %d)", n, len(addrs), len(db.contacts()))


# ---------- learning from corrections ----------

def sync_feedback(svc, db, cfg, lab, force=False):
    last = float(db.get_meta("feedback_synced_at", 0))
    if not force and time.time() - last < FEEDBACK_REFRESH_SECS:
        return
    now = int(time.time())
    # Tagged/moved mail is checked for a week (I may correct it any time); kept mail only for 2 days (misses
    # are spotted quickly). This keeps the number of Gmail lookups, and so the quota use, small.
    rows = db.c.execute(
        """SELECT d.* FROM decisions d LEFT JOIN feedback f ON f.message_id = d.message_id
           WHERE f.message_id IS NULL
             AND ((d.action != 'keep' AND d.ts >= ?) OR (d.action = 'keep' AND d.ts >= ?))
           ORDER BY d.ts DESC LIMIT 1500""", (now - FEEDBACK_DAYS * 86400, now - 2 * 86400)).fetchall()
    if not rows:
        db.set_meta("feedback_synced_at", int(time.time()))
        return
    L = cfg["labels"]
    cur = gmail.get_labels(svc, [r["message_id"] for r in rows])
    f_id, w_id, n_id = lab[L["filtered"]], lab[L["would_filter"]], lab[L["newsletters"]]
    mistakes = misses = 0
    for r in rows:
        labels = cur.get(r["message_id"])
        if labels is None:
            continue
        moved = r["action"] in ("filter", "newsletter") and r["applied"]
        if moved and r["mode"] == "live" and "INBOX" in labels:
            # I moved it back to the inbox: this sender is not junk.
            gmail.modify(svc, [r["message_id"]], remove=[f_id, n_id])
            db.learn(r["from_addr"], "allow", "moved back to inbox")
            db.add_feedback(r["message_id"], "mistake")
            mistakes += 1
            log.info("feedback: MISTAKE %s (moved back) -> allowlisted", r["from_addr"])
        elif moved and r["mode"] == "shadow" and w_id not in labels:
            db.learn(r["from_addr"], "allow", "removed WouldFilter label")
            db.add_feedback(r["message_id"], "mistake")
            mistakes += 1
            log.info("feedback: MISTAKE %s (label removed) -> allowlisted", r["from_addr"])
        elif r["action"] == "keep" and f_id in labels:
            # I filed it myself: the model missed it.
            if cfg["mode"] == "live":
                gmail.modify(svc, [r["message_id"]], remove=["INBOX"])
            db.learn(r["from_addr"], "block", "filed to AI/Filtered by hand")
            db.add_feedback(r["message_id"], "miss")
            misses += 1
            log.info("feedback: MISS %s (filed by hand) -> blocklisted", r["from_addr"])
    db.set_meta("feedback_synced_at", int(time.time()))
    if mistakes or misses:
        log.info("feedback: %d mistakes, %d misses learned", mistakes, misses)


# ---------- main loop ----------

def apply_action(cfg, lab, action):
    """-> (labels to add, labels to remove) for an action in the current mode."""
    L = cfg["labels"]
    if action == "keep":
        return [], []
    if cfg["mode"] == "shadow":
        return [lab[L["would_filter"]]], []
    if action == "newsletter":
        return [lab[L["newsletters"]]], ["INBOX"]
    return [lab[L["filtered"]]], ["INBOX"]


def consider_customer(svc, db, cfg, m, action, clf, me, probs, known, state):
    """Does this email look like a customer/prospect conversation? Proposes (or adds) the company it is with."""
    cc = cfg["customers"]
    if not (cc["enabled"] and cc["ai_detect"]) or action != "keep" or not rules.eligible_for_task_check(m, me):
        return
    dom = customers.attribute_domain(cfg, m)
    if not dom or dom in db.customers() or dom in state["rejected"]:
        return
    seen = db.c.execute("SELECT n_positive, n_assessed, best_p FROM customer_candidates WHERE domain=?", (dom,)).fetchone()
    n_pos, n_ass, best = (seen["n_positive"], seen["n_assessed"], seen["best_p"]) if seen else (0, 0, 0.0)
    if n_pos == 0 and n_ass >= 4:
        return   # assessed several times, never looked like a customer (e.g. a vendor that keeps pitching)
    p = clf.customer_prob(m, me, *known)
    thr = cc["ai_threshold"] - (0.10 if customers.colleague_on(cfg, m, me) else 0.0)
    positive = False
    if p >= thr:
        if probs is None:
            probs = clf.classify(m, me, *known)
        positive = junk_mass(probs, cfg) < cfg["important"]["max_junk"] and probs.get("real_work_or_personal", 0) >= cfg["important"]["min_personal"]
    db.save_candidate(dom, n_pos + positive, n_ass + 1, max(best, p), m.from_addr, m.subject)
    if positive:
        log.info("customer?  %-26s p=%.2f | %s", dom, p, m.subject[:60])
        if cc["auto_add_ai"] and state["added"] < cc["max_new_per_run"]:
            customers.add_customer(svc, db, cfg, dom, "ai")
            state["added"] += 1


def build_ctx(db, cfg):
    sent_threads = db.sent_threads()
    me = {a.lower() for a in cfg["my_addresses"]} | cfg.get("_me", set())
    return {
        "me": me,
        "own_domains": cfg["own_domains"],
        "keep_domains": cfg["keep_domains"],
        "customer_domains": set(db.customers()),
        "allow": read_list("allowlist.txt"), "learned_allow": db.learned("allow"),
        "block": read_list("blocklist.txt"), "learned_block": db.learned("block"),
        "contacts": db.contacts(),
        "thread_has_sent": lambda tid: tid in sent_threads,
    }


def process(svc, db, cfg, lab, mails, clf, dry_run=False):
    ctx = build_ctx(db, cfg)
    me = ctx["me"]
    cust = db.customers()
    cstate = {"rejected": db.not_customers(), "added": 0}
    cparent = lab.get(cfg["customers"]["parent_label"])
    counts = Counter()
    pending = {}   # (labels to add, labels to remove) -> message ids not yet changed in Gmail

    def flush():
        """Apply the queued label changes in Gmail, then mark those decisions as applied."""
        for (add, remove), ids in pending.items():
            try:
                gmail.modify(svc, ids, add=add, remove=remove)
            except Exception:
                # Forget these decisions so the next run retries them rather than skipping them as 'seen'.
                db.c.executemany("DELETE FROM decisions WHERE message_id=?", [(i,) for i in ids])
                db.c.executemany("DELETE FROM importance WHERE message_id=?", [(i,) for i in ids])
                db.c.commit()
                raise
            db.set_applied_many(ids)
        pending.clear()

    for n, m in enumerate(mails, 1):
        v = rules.evaluate(m, ctx)
        if v:
            action, conf, cat, by, probs = v.action, 1.0, v.reason, v.reason, None
        else:
            probs = clf.classify(m, me)
            thr = cfg["threshold_only_to_me"] if m.only_to_me(me) else cfg["threshold"]
            action, conf, cat = decide(probs, cfg, thr)
            conf = junk_mass(probs, cfg)   # what is compared with the threshold; shown for kept mail too
            by = "model"
        add, remove = apply_action(cfg, lab, action)
        known = (m.from_addr in ctx["contacts"], ctx["thread_has_sent"](m.thread_id))
        important, why, p_act = assess_importance(cfg, m, action, clf, me, probs, known)
        if important:
            add = list(add) + important_labels(cfg, lab)
            counts["important"] += 1
        if cust and cparent:
            cl = customers.labels_for(m, cust, cparent)
            if cl:
                add = list(add) + cl
                counts["customer"] += 1
                if not dry_run:
                    db.mark_customer_labeled([m.id])
        consider_customer(svc, db, cfg, m, action, clf, me, probs, known, cstate)
        counts[action] += 1
        log.info("%-10s %.2f %-24s %-34s | %s%s", action.upper(), conf, cat, m.from_addr[:34], m.subject[:60],
                 f"   *** IMPORTANT ({why}{'' if p_act is None else f' {p_act:.2f}'})" if important else "")
        if not dry_run:
            db.record(m, cat, conf, action, by, cfg["mode"], False, probs)
            db.record_importance(m, important, why, p_act)
            if add or remove:
                pending.setdefault((tuple(add), tuple(remove)), []).append(m.id)
            if n % 20 == 0:
                flush()   # tags show up in Gmail as the run goes, not only at the end
                if cfg.get("_respect_pause") and pause.reason(cfg, in_run=True):
                    raise Paused
    if not dry_run:
        flush()
    counts["customers_added"] = cstate["added"]
    return counts


def cmd_run(args, cfg, clf=None):
    """One check, holding an exclusive lock for exactly its duration (released even if it fails)."""
    lock = open(cfg["root"] / "logs" / "run.lock", "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another run is in progress; exiting")
            return
        try:
            _run_once(args, cfg, clf)
        except Paused:
            # Anything not yet processed is picked up on the next run.
            log.info("stopped part-way: %s", pause.reason(cfg, in_run=True))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        lock.close()


def _run_once(args, cfg, clf=None):
    if args.mode:
        cfg["mode"] = args.mode
    if args.days:
        cfg["lookback_days"] = args.days
    if args.limit:
        cfg["max_per_run"] = args.limit
    db = DB(cfg["db_path"])
    svc = gmail.get_service(interactive=False)
    lab = label_ids(svc, cfg)
    cfg["_me"] = gmail.my_addresses(svc)
    refresh_contacts(svc, db)
    if not args.dry_run:
        sync_feedback(svc, db, cfg, lab)
        sync_importance_feedback(svc, db, cfg, lab)
        try:
            customers.sync(svc, db, cfg)
        except Exception:
            log.exception("customer label sync failed; the junk filter carries on")
    ids = gmail.list_ids(svc, f"in:inbox newer_than:{cfg['lookback_days']}d", cfg["max_per_run"])
    new = [i for i in ids if not db.seen(i)]
    if not new:
        log.info("run [%s]: %d in inbox window, nothing new", cfg["mode"], len(ids))
        return
    clf = clf or Classifier(cfg)
    total, done = Counter(), 0
    for i in range(0, len(new), CHUNK):
        if cfg.get("_respect_pause") and pause.reason(cfg, in_run=True):
            raise Paused
        mails = gmail.get_full(svc, new[i:i + CHUNK])
        total += process(svc, db, cfg, lab, mails, clf, dry_run=args.dry_run)
        done += len(mails)
        if len(new) > CHUNK:
            log.info("progress: %d / %d emails", done, len(new))
    if total["customers_added"] and not args.dry_run:
        customers.sync(svc, db, cfg)   # sweep the newly added domains
    log.info("run [%s%s]: %d new -> keep %d, filter %d, newsletter %d, important %d, customer mail %d", cfg["mode"],
             " DRY-RUN" if args.dry_run else "", done, total["keep"], total["filter"], total["newsletter"],
             total["important"], total["customer"])


def on_ac_power():
    import subprocess
    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
    return "AC Power" in out


def cmd_daemon(args, cfg):
    """Wake every few minutes; run a check when `interval_seconds` have passed, on AC power, and not paused."""
    clf = Classifier(cfg)
    args.mode = args.days = args.limit = None
    args.dry_run = False
    cfg["_respect_pause"] = True
    last_run, last_msg = 0.0, None
    while True:
        try:
            if time.time() - last_run >= cfg["interval_seconds"]:
                why = None
                if cfg["only_on_power"] and not on_ac_power():
                    why = "on battery: waiting for power"
                else:
                    why = pause.reason(cfg)
                    if why:
                        why = "standing down: " + why
                if why:
                    if why != last_msg:
                        log.info(why)
                        last_msg = why
                    clf.unload()   # free the ~18 GB while I'm busy
                else:
                    last_msg = None
                    cmd_run(args, cfg, clf)
                    last_run = time.time()
            if clf.model is not None and time.time() - clf.last_used > cfg["idle_unload_minutes"] * 60:
                clf.unload()
        except Exception:
            log.exception("run failed; will retry on the next check")
            last_run = time.time()
        time.sleep(300)


def cmd_reeval(args, cfg):
    """Re-check mail already tagged against the current rules (no model); un-tag whatever the rules now keep."""
    db = DB(cfg["db_path"])
    svc = gmail.get_service(interactive=False)
    lab = label_ids(svc, cfg)
    cfg["_me"] = gmail.my_addresses(svc)
    ctx = build_ctx(db, cfg)
    L = cfg["labels"]
    rows = db.c.execute("SELECT message_id, mode, decided_by FROM decisions WHERE action!='keep' AND applied=1").fetchall()
    by = {r["message_id"]: r["decided_by"] for r in rows}
    clf = Classifier(cfg) if args.model else None
    ids = [r["message_id"] for r in rows]
    modes = {r["message_id"]: r["mode"] for r in rows}
    freed = 0
    for i in range(0, len(ids), CHUNK):
        for m in gmail.get_full(svc, ids[i:i + CHUNK]):
            v = rules.evaluate(m, ctx)
            if not (v and v.action == "keep") and clf and by[m.id] == "model":
                probs = clf.classify(m, ctx["me"])
                thr = cfg["threshold_only_to_me"] if m.only_to_me(ctx["me"]) else cfg["threshold"]
                a, _, cat = decide(probs, cfg, thr)
                v = rules.Verdict("keep", f"model:{cat}") if a == "keep" else None
            if not (v and v.action == "keep"):
                continue
            rm = [lab[L["would_filter"]], lab[L["filtered"]], lab[L["newsletters"]]]
            gmail.modify(svc, [m.id], add=["INBOX"] if modes[m.id] == "live" else [], remove=rm)
            db.c.execute("UPDATE decisions SET action='keep', decided_by=?, category=?, applied=0 WHERE message_id=?",
                         (v.reason, v.reason, m.id))
            db.c.commit()
            freed += 1
            log.info("reeval: un-tagged %s | %s (%s)", m.from_addr, m.subject[:60], v.reason)
    print(f"Checked {len(ids)} tagged emails; {freed} are now protected by the rules and were un-tagged.")


def cmd_learn(args, cfg):
    """Apply my label corrections now (otherwise done at most every 30 min by the background service)."""
    db = DB(cfg["db_path"])
    svc = gmail.get_service(interactive=False)
    sync_feedback(svc, db, cfg, label_ids(svc, cfg), force=True)
    print("learned allow:", sorted(db.learned("allow")) or "-")
    print("learned block:", sorted(db.learned("block")) or "-")


def cmd_promote(args, cfg):
    """Move everything currently tagged AI/WouldFilter out of the inbox (into AI/Filtered). Nothing is deleted."""
    db = DB(cfg["db_path"])
    svc = gmail.get_service(interactive=False)
    lab = label_ids(svc, cfg)
    L = cfg["labels"]
    w_id, f_id = lab[L["would_filter"]], lab[L["filtered"]]
    sync_feedback(svc, db, cfg, lab, force=True)          # learn from labels I removed, first
    ids, token = [], None
    while True:
        r = gmail.backoff(lambda: svc.users().messages().list(
            userId="me", labelIds=[w_id], maxResults=500, pageToken=token).execute(num_retries=3))
        ids += [m["id"] for m in r.get("messages", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    if not ids:
        print("Nothing is tagged AI/WouldFilter.")
        return
    if args.dry_run:
        print(f"Would move {len(ids)} emails out of the inbox.")
        return
    gmail.modify(svc, ids, add=[f_id], remove=[w_id, "INBOX"])
    now = int(time.time())
    db.c.executemany("UPDATE decisions SET mode='live', applied=1, ts=? WHERE message_id=? AND action!='keep'",
                     [(now, i) for i in ids])
    db.c.commit()
    print(f"Moved {len(ids)} emails out of the inbox into {L['filtered']}. Undo: uv run triage undo --since 1h")


def cmd_pause(args, cfg):
    pause.set_pause(pause.parse_duration(args.duration) if args.duration else None)
    log.info("pause set%s", f" for {args.duration}" if args.duration else " until resumed")
    print("Paused " + (f"for {args.duration}" if args.duration else "until you run: triage resume") +
          ". The service stands down and frees its memory within 5 minutes.")


def cmd_resume(args, cfg):
    pause.clear_pause()
    log.info("pause cleared")
    print("Resumed. The next check happens within 5 minutes.")


def cmd_status(args, cfg):
    import subprocess
    out = subprocess.run(["launchctl", "print", f"gui/{__import__('os').getuid()}/com.tom.mail-triage"],
                         capture_output=True, text=True).stdout
    state = next((l.split("=")[1].strip() for l in out.splitlines() if l.strip().startswith("state =")), "not installed")
    print(f"service: {state}")
    print(f"mode: {cfg['mode']}   on AC power: {on_ac_power()}")
    print("standing down because: " + (pause.reason(cfg) or "nothing, it will run on the next check"))


def sync_importance_feedback(svc, db, cfg, lab):
    """If I remove the AI/Important label from something, count it as a wrong flag."""
    imp = cfg["important"]
    if not imp["enabled"]:
        return
    rows = db.c.execute("SELECT message_id, from_addr FROM importance WHERE important=1 AND corrected=0 AND ts>=?",
                        (int(time.time()) - 3 * 86400,)).fetchall()
    if not rows:
        return
    cur = gmail.get_labels(svc, [r["message_id"] for r in rows])
    for r in rows:
        labels = cur.get(r["message_id"])
        if labels is not None and lab[imp["label"]] not in labels:
            db.c.execute("UPDATE importance SET corrected=1 WHERE message_id=?", (r["message_id"],))
            db.c.commit()
            log.info("feedback: not important after all: %s", r["from_addr"])


def cmd_flag(args, cfg):
    """Mark important mail among what is ALREADY in the inbox (the hourly run handles new mail)."""
    lock = open(cfg["root"] / "logs" / "run.lock", "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another run is in progress; try again in a few minutes")
        db = DB(cfg["db_path"])
        svc = gmail.get_service(interactive=False)
        lab = label_ids(svc, cfg)
        cfg["_me"] = gmail.my_addresses(svc)
        ctx = build_ctx(db, cfg)
        me = ctx["me"]
        ids = gmail.list_ids(svc, f"in:inbox newer_than:{args.days}d", args.limit)
        todo = ids if args.redo else [i for i in ids if not db.importance_seen(i)]
        # Mail the junk filter removed from the inbox is never important.
        junk = {r[0] for r in db.c.execute("SELECT message_id FROM decisions WHERE action != 'keep'")}
        todo = [i for i in todo if i not in junk]
        log.info("flag: %d inbox emails from the last %d days, %d to assess", len(ids), args.days, len(todo))
        clf, found, pending = Classifier(cfg), [], []
        for i in range(0, len(todo), CHUNK):
            for m in gmail.get_full(svc, todo[i:i + CHUNK]):
                known = (m.from_addr in ctx["contacts"], ctx["thread_has_sent"](m.thread_id))
                important, why, p = assess_importance(cfg, m, "keep", clf, me, None, known)
                if important:
                    found.append(m.id)
                    log.info("IMPORTANT (%s%s) %-34s | %s", why, "" if p is None else f" {p:.2f}", m.from_addr[:34], m.subject[:62])
                elif args.verbose and p is not None:
                    log.info("   not important (%.2f)  %-34s | %s", p, m.from_addr[:34], m.subject[:62])
                if not args.dry_run:
                    db.record_importance(m, important, why, p)
                    if important:
                        pending.append(m.id)
                    if len(pending) >= 20:
                        gmail.modify(svc, pending, add=important_labels(cfg, lab))
                        pending.clear()
            log.info("progress: %d / %d", min(i + CHUNK, len(todo)), len(todo))
        if pending:
            gmail.modify(svc, pending, add=important_labels(cfg, lab))
        log.info("flag%s: %d marked important out of %d assessed", " DRY-RUN" if args.dry_run else "", len(found), len(todo))
    finally:
        lock.close()


def cmd_customers(args, cfg):
    db = DB(cfg["db_path"])
    svc = gmail.get_service(interactive=False)
    act = args.action
    if act in ("add", "remove") and not args.domain:
        raise SystemExit(f"usage: triage customers {act} <domain>")
    if act == "list":
        rows = db.customers()
        for d, r in sorted(rows.items()):
            print(f"{d:30} {r['source']:15} {'swept ' + time.strftime('%d %b', time.localtime(r['synced_at'])) if r['synced_at'] else 'not yet swept'}")
        print(f"\n{len(rows)} customer domains" + (f"; {len(db.not_customers())} rejected" if db.not_customers() else ""))
    elif act == "candidates":
        rows = db.candidates()
        if not rows:
            print("No candidates yet. Run: triage customers discover")
        for r in rows:
            print(f"{r['domain']:30} {r['n_positive']}/{r['n_assessed']} emails look like a customer, best {r['best_p']:.2f}  "
                  f"| {r['sample_from'][:32]:32} | {r['sample_subject'][:50]}")
        if rows:
            print("\nApprove with: triage customers add <domain>   (or put the Customers label on one of their emails)")
    elif act == "add":
        d = customers.registrable(args.domain.split("@")[-1])
        db.forgive_customer(d)
        if d in db.customers():
            print(f"{d} is already a customer.")
        else:
            customers.add_customer(svc, db, cfg, d, "manual")
        customers.sync(svc, db, cfg)
        print(f"Added {d} and labelled its mail.")
    elif act == "approve":   # add every saved candidate
        rows = db.candidates()
        for r in rows:
            if r["domain"] not in db.customers():
                customers.add_customer(svc, db, cfg, r["domain"], "ai")
        customers.sync(svc, db, cfg)
        print(f"Approved {len(rows)} candidate companies and labelled their mail.")
    elif act == "remove":
        d = customers.registrable(args.domain.split("@")[-1])
        row = db.customers().get(d)
        if not row:
            raise SystemExit(f"{d} is not a customer.")
        svc.users().labels().delete(userId="me", id=row["label_id"]).execute()   # removes the label only, never mail
        db.remove_customer(d, reject=True)
        print(f"Removed {d}; its label was deleted (no email was deleted) and it will not be re-added.")
    elif act == "sync":
        customers.sync(svc, db, cfg)
        print("Customer labels are up to date.")
    elif act == "discover":
        lab = label_ids(svc, cfg)
        cfg["_me"] = gmail.my_addresses(svc)
        ctx = build_ctx(db, cfg)
        me = ctx["me"]
        q = (f"newer_than:{args.days}d -in:sent -in:drafts -in:spam -in:trash "
             "-category:promotions -category:social -category:forums")
        ids = gmail.list_ids(svc, q, args.limit)
        log.info("discover: %d emails from the last %d days", len(ids), args.days)
        by_dom = {}
        for i in range(0, len(ids), CHUNK):
            for m in gmail.get_full(svc, ids[i:i + CHUNK]):
                dom = customers.attribute_domain(cfg, m)
                if dom and dom not in ctx["customer_domains"] and dom not in db.not_customers() \
                        and rules.eligible_for_task_check(m, me):
                    by_dom.setdefault(dom, []).append(m)
        log.info("discover: %d external companies to assess", len(by_dom))
        clf, cc = Classifier(cfg), cfg["customers"]
        found = []
        for n, (dom, ms) in enumerate(sorted(by_dom.items(), key=lambda kv: -len(kv[1])), 1):
            pos = ass = 0
            best, sample = 0.0, ms[0]
            for m in ms[:8]:
                known = (m.from_addr in ctx["contacts"], ctx["thread_has_sent"](m.thread_id))
                p = clf.customer_prob(m, me, *known)
                ass += 1
                if p >= cc["ai_threshold"] - (0.10 if customers.colleague_on(cfg, m, me) else 0.0):
                    probs = clf.classify(m, me, *known)
                    if junk_mass(probs, cfg) < cfg["important"]["max_junk"] and probs.get("real_work_or_personal", 0) >= cfg["important"]["min_personal"]:
                        pos += 1
                        if p > best:
                            best, sample = p, m
                        if pos >= 2:
                            break
            if pos:
                found.append((dom, pos, ass, best, sample))
                if not args.dry_run:
                    db.save_candidate(dom, pos, ass, best, sample.from_addr, sample.subject)
            if n % 25 == 0:
                log.info("discover: %d / %d companies", n, len(by_dom))
        print(f"\n{len(found)} companies look like customers or prospects:")
        for dom, pos, ass, best, m in sorted(found, key=lambda x: (-x[1], -x[3])):
            print(f"  {dom:30} {pos}/{ass} emails, best {best:.2f} | {m.from_addr[:32]:32} | {m.subject[:52]}")
        if found and not args.dry_run:
            print("\nSaved as candidates. Approve with: triage customers add <domain>")


def cmd_login(args, cfg):
    svc = gmail.get_service(interactive=True)
    p = svc.users().getProfile(userId="me").execute()
    lab = label_ids(svc, cfg)
    print(f"Signed in as {p['emailAddress']} ({p['messagesTotal']} messages). Labels ready: {', '.join(lab)}")


def cmd_logout(args, cfg):
    gmail.logout()
    print("Token removed from the macOS Keychain. Revoke access too at https://myaccount.google.com/permissions")


def cmd_undo(args, cfg):
    m = re.fullmatch(r"(\d+)([hd])", args.since)
    if not m:
        raise SystemExit("--since must look like 24h or 7d")
    secs = int(m.group(1)) * (3600 if m.group(2) == "h" else 86400)
    db = DB(cfg["db_path"])
    svc = gmail.get_service(interactive=False)
    lab = label_ids(svc, cfg)
    L = cfg["labels"]
    rows = db.c.execute("SELECT message_id FROM decisions WHERE applied=1 AND mode='live' AND action!='keep' AND ts>=?",
                        (int(time.time()) - secs,)).fetchall()
    ids = [r[0] for r in rows]
    if ids:
        gmail.modify(svc, ids, add=["INBOX"], remove=[lab[L["filtered"]], lab[L["newsletters"]]])
        for i in ids:
            db.set_applied(i, 0)
    print(f"Put {len(ids)} messages back in the inbox.")


def cmd_report(args, cfg):
    db = DB(cfg["db_path"])
    cutoff = int(time.time()) - args.days * 86400
    q = lambda sql, *a: db.c.execute(sql, a).fetchall()
    print(f"=== last {args.days} days ===")
    for r in q("SELECT mode, action, COUNT(*) n FROM decisions WHERE ts>=? GROUP BY mode, action ORDER BY mode, action", cutoff):
        print(f"{r['mode']:7} {r['action']:11} {r['n']}")
    print("\nby decider:")
    for r in q("SELECT decided_by, COUNT(*) n FROM decisions WHERE ts>=? GROUP BY decided_by ORDER BY n DESC", cutoff):
        print(f"  {r['decided_by']:32} {r['n']}")
    moved = q("SELECT COUNT(*) FROM decisions WHERE ts>=? AND action!='keep'", cutoff)[0][0]
    mist = q("SELECT COUNT(*) FROM feedback WHERE kind='mistake' AND ts>=?", cutoff)[0][0]
    miss = q("SELECT COUNT(*) FROM feedback WHERE kind='miss' AND ts>=?", cutoff)[0][0]
    kept = q("SELECT COUNT(*) FROM decisions WHERE ts>=? AND action='keep'", cutoff)[0][0]
    print(f"\nfiltered/would-filter: {moved}   wrongly filtered (corrected by you): {mist}   missed junk (filed by you): {miss}")
    if moved:
        print(f"precision so far: {100 * (moved - mist) / moved:.1f}%   (go live at >= 99%)")
    if kept:
        print(f"recall so far:    {100 * moved / (moved + miss):.1f}%" if (moved + miss) else "")
    imp = q("SELECT COUNT(*), SUM(reason='vip'), SUM(reason='question_or_task'), SUM(corrected) FROM importance "
            "WHERE important=1 AND ts>=?", cutoff)[0]
    print(f"\nmarked important: {imp[0]}   (VIPs {imp[1] or 0}, question/task {imp[2] or 0}, "
          f"you un-flagged {imp[3] or 0})")
    print("\nrecently filtered:")
    for r in q("SELECT from_addr, subject, category, confidence FROM decisions WHERE ts>=? AND action!='keep' ORDER BY ts DESC LIMIT ?", cutoff, args.show):
        print(f"  {r['confidence']:.2f} {r['category'][:22]:22} {r['from_addr'][:32]:32} {r['subject'][:50]}")


def cmd_selftest(args, cfg):
    """Run the model on sample emails (no Gmail needed)."""
    from .samples import SAMPLES
    clf = Classifier(cfg)
    ok = 0
    for want, m in SAMPLES:
        t = time.time()
        from .samples import ME
        me = {a.lower() for a in cfg["my_addresses"]} | {ME}
        probs = clf.classify(m, me)
        action, conf, cat = decide(probs, cfg, cfg["threshold_only_to_me"] if m.only_to_me(me) else cfg["threshold"])
        good = action == want
        ok += good
        junk = sum(p for c, p in probs.items() if cfg["actions"].get(c) != "keep")
        top3 = sorted(probs.items(), key=lambda kv: -kv[1])[:2]
        print(f"{'OK ' if good else 'BAD'} want={want:10} got={action:10} junk={junk:.2f} {time.time() - t:4.1f}s  "
              f"{m.from_addr[:30]:30} | {m.subject[:42]:42} | " + ", ".join(f"{k} {v:.2f}" for k, v in top3))
    print(f"\n{ok}/{len(SAMPLES)} as expected")


def main():
    ap = argparse.ArgumentParser(prog="triage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login").set_defaults(fn=cmd_login)
    sub.add_parser("logout").set_defaults(fn=cmd_logout)
    r = sub.add_parser("run")
    r.add_argument("--mode", choices=["shadow", "live"])
    r.add_argument("--days", type=int)
    r.add_argument("--limit", type=int)
    r.add_argument("--dry-run", action="store_true", help="classify and print, change nothing")
    r.set_defaults(fn=cmd_run)
    b = sub.add_parser("backfill", help="clean up existing inbox mail")
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--limit", type=int, default=2000)
    b.add_argument("--mode", choices=["shadow", "live"])
    b.add_argument("--dry-run", action="store_true")
    b.set_defaults(fn=cmd_run)
    u = sub.add_parser("undo")
    u.add_argument("--since", default="24h")
    u.set_defaults(fn=cmd_undo)
    rp = sub.add_parser("report")
    rp.add_argument("--days", type=int, default=7)
    rp.add_argument("--show", type=int, default=15, help="how many recently filtered emails to list")
    rp.set_defaults(fn=cmd_report)
    sub.add_parser("daemon", help="run forever, checking every interval_seconds").set_defaults(fn=cmd_daemon)
    re_ = sub.add_parser("reeval", help="re-check tagged mail against the current rules (and with --model, the AI)")
    re_.add_argument("--model", action="store_true")
    re_.set_defaults(fn=cmd_reeval)
    sub.add_parser("learn", help="apply my label corrections now").set_defaults(fn=cmd_learn)
    fl = sub.add_parser("flag", help="mark important mail already in the inbox")
    fl.add_argument("--days", type=int, default=14)
    fl.add_argument("--limit", type=int, default=1000)
    fl.add_argument("--dry-run", action="store_true")
    fl.add_argument("--redo", action="store_true", help="re-assess mail already assessed (after changing the rules)")
    fl.add_argument("--verbose", action="store_true", help="also show emails judged not important")
    fl.set_defaults(fn=cmd_flag)
    cu = sub.add_parser("customers", help="customer and prospect labels")
    cu.add_argument("action", choices=["list", "candidates", "add", "approve", "remove", "sync", "discover"])
    cu.add_argument("domain", nargs="?")
    cu.add_argument("--days", type=int, default=60)
    cu.add_argument("--limit", type=int, default=800)
    cu.add_argument("--dry-run", action="store_true")
    cu.set_defaults(fn=cmd_customers)
    pr = sub.add_parser("promote", help="move everything tagged AI/WouldFilter out of the inbox")
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(fn=cmd_promote)
    pa = sub.add_parser("pause", help="stop the service running, e.g. before a demo: pause 2h")
    pa.add_argument("duration", nargs="?", help="30m, 2h, 1d; omit to pause until resume")
    pa.set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="let the service run again").set_defaults(fn=cmd_resume)
    sub.add_parser("status", help="is it running, and is it standing down?").set_defaults(fn=cmd_status)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    args = ap.parse_args()
    cfg = load()
    setup_logging(cfg)
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
