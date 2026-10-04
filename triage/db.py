import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  message_id TEXT PRIMARY KEY,
  thread_id TEXT,
  ts INTEGER,
  from_addr TEXT,
  subject TEXT,
  category TEXT,
  confidence REAL,
  action TEXT,          -- keep | filter | newsletter
  decided_by TEXT,      -- rule:<name> | model
  mode TEXT,            -- shadow | live
  applied INTEGER,      -- 1 if labels were changed on the message
  probs TEXT
);
CREATE TABLE IF NOT EXISTS importance (
  message_id TEXT PRIMARY KEY, ts INTEGER, from_addr TEXT, subject TEXT,
  important INTEGER, reason TEXT, p REAL, corrected INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS customers (
  domain TEXT PRIMARY KEY, label TEXT, label_id TEXT, source TEXT, ts INTEGER, synced_at INTEGER
);
CREATE TABLE IF NOT EXISTS not_customers (domain TEXT PRIMARY KEY, ts INTEGER);
CREATE TABLE IF NOT EXISTS customer_candidates (
  domain TEXT PRIMARY KEY, n_positive INTEGER, n_assessed INTEGER, best_p REAL, sample_from TEXT, sample_subject TEXT, ts INTEGER
);
CREATE TABLE IF NOT EXISTS customer_labeled (message_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS needs_reply (
  thread_id TEXT PRIMARY KEY, message_id TEXT, needs INTEGER, p REAL, last_ts INTEGER, labeled TEXT, updated INTEGER
);
CREATE TABLE IF NOT EXISTS topics (
  message_id TEXT PRIMARY KEY, topic TEXT, p REAL, escalation REAL, ts INTEGER
);
CREATE TABLE IF NOT EXISTS urgency (
  message_id TEXT PRIMARY KEY, p REAL, urgent INTEGER, ts INTEGER
);
CREATE TABLE IF NOT EXISTS contacts (addr TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS sent_threads (thread_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS learned (addr TEXT PRIMARY KEY, kind TEXT, ts INTEGER, source TEXT);
CREATE TABLE IF NOT EXISTS feedback (message_id TEXT PRIMARY KEY, kind TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)

    def seen(self, message_id):
        return self.c.execute("SELECT 1 FROM decisions WHERE message_id=?", (message_id,)).fetchone() is not None

    def record(self, m, category, confidence, action, decided_by, mode, applied, probs=None):
        self.c.execute(
            "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (m.id, m.thread_id, int(time.time()), m.from_addr, m.subject[:300], category, confidence,
             action, decided_by, mode, int(applied), json.dumps(probs) if probs else None),
        )
        self.c.commit()

    def set_applied(self, message_id, applied=1):
        self.c.execute("UPDATE decisions SET applied=? WHERE message_id=?", (applied, message_id))
        self.c.commit()

    def importance_seen(self, message_id):
        return self.c.execute("SELECT 1 FROM importance WHERE message_id=?", (message_id,)).fetchone() is not None

    def record_importance(self, m, important, reason, p):
        self.c.execute("INSERT OR REPLACE INTO importance (message_id, ts, from_addr, subject, important, reason, p) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (m.id, int(time.time()), m.from_addr, m.subject[:300], int(important), reason, p))
        self.c.commit()

    def set_applied_many(self, message_ids):
        self.c.executemany("UPDATE decisions SET applied=1 WHERE message_id=?", [(i,) for i in message_ids])
        self.c.commit()

    # customers
    def customers(self):
        return {r["domain"]: dict(r) for r in self.c.execute("SELECT * FROM customers")}

    def add_customer(self, domain, label, label_id, source):
        self.c.execute("INSERT OR REPLACE INTO customers (domain, label, label_id, source, ts, synced_at) VALUES (?,?,?,?,?,NULL)",
                       (domain, label, label_id, source, int(time.time())))
        self.c.execute("DELETE FROM customer_candidates WHERE domain=?", (domain,))
        self.c.commit()

    def remove_customer(self, domain, reject=True):
        self.c.execute("DELETE FROM customers WHERE domain=?", (domain,))
        if reject:
            self.c.execute("INSERT OR REPLACE INTO not_customers VALUES (?,?)", (domain, int(time.time())))
        self.c.commit()

    def not_customers(self):
        return {r[0] for r in self.c.execute("SELECT domain FROM not_customers")}

    def forgive_customer(self, domain):
        self.c.execute("DELETE FROM not_customers WHERE domain=?", (domain,))
        self.c.commit()

    def mark_customer_synced(self, domain):
        self.c.execute("UPDATE customers SET synced_at=? WHERE domain=?", (int(time.time()), domain))
        self.c.commit()

    def mark_customer_labeled(self, ids):
        self.c.executemany("INSERT OR IGNORE INTO customer_labeled VALUES (?)", [(i,) for i in ids])
        self.c.commit()

    def customer_labeled(self):
        return {r[0] for r in self.c.execute("SELECT message_id FROM customer_labeled")}

    def save_candidate(self, domain, n_pos, n_assessed, best_p, sample_from, sample_subject):
        self.c.execute("INSERT OR REPLACE INTO customer_candidates VALUES (?,?,?,?,?,?,?)",
                       (domain, n_pos, n_assessed, best_p, sample_from, sample_subject[:200], int(time.time())))
        self.c.commit()

    def candidates(self):
        return self.c.execute("SELECT * FROM customer_candidates ORDER BY n_positive DESC, best_p DESC").fetchall()

    # needs-reply tracking
    def get_needs_reply(self, thread_id):
        return self.c.execute("SELECT * FROM needs_reply WHERE thread_id=?", (thread_id,)).fetchone()

    def set_needs_reply(self, thread_id, message_id, needs, p, last_ts, labeled_json):
        self.c.execute("INSERT OR REPLACE INTO needs_reply VALUES (?,?,?,?,?,?,?)",
                       (thread_id, message_id, int(needs), p, last_ts, labeled_json, int(time.time())))
        self.c.commit()

    def open_needs_reply(self):
        return self.c.execute("SELECT * FROM needs_reply WHERE needs=1").fetchall()

    # topics and urgency
    def topic_seen(self, message_id):
        return self.c.execute("SELECT 1 FROM topics WHERE message_id=?", (message_id,)).fetchone() is not None

    def record_topic(self, message_id, topic, p, escalation):
        self.c.execute("INSERT OR REPLACE INTO topics VALUES (?,?,?,?,?)", (message_id, topic, p, escalation, int(time.time())))
        self.c.commit()

    def urgency_seen(self, message_id):
        return self.c.execute("SELECT 1 FROM urgency WHERE message_id=?", (message_id,)).fetchone() is not None

    def record_urgency(self, message_id, p, urgent):
        self.c.execute("INSERT OR REPLACE INTO urgency VALUES (?,?,?,?)", (message_id, p, int(urgent), int(time.time())))
        self.c.commit()

    # contacts (people I have emailed)
    def add_contacts(self, addrs):
        self.c.executemany("INSERT OR IGNORE INTO contacts VALUES (?)", [(a,) for a in addrs])
        self.c.commit()

    def add_sent_threads(self, ids):
        self.c.executemany("INSERT OR IGNORE INTO sent_threads VALUES (?)", [(i,) for i in ids])
        self.c.commit()

    def sent_threads(self):
        return {r[0] for r in self.c.execute("SELECT thread_id FROM sent_threads")}

    def contacts(self):
        return {r[0] for r in self.c.execute("SELECT addr FROM contacts")}

    # learned corrections
    def learn(self, addr, kind, source):
        self.c.execute("INSERT OR REPLACE INTO learned VALUES (?,?,?,?)", (addr, kind, int(time.time()), source))
        self.c.commit()

    def learned(self, kind):
        return {r[0] for r in self.c.execute("SELECT addr FROM learned WHERE kind=?", (kind,))}

    def add_feedback(self, message_id, kind):
        self.c.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?)", (message_id, kind, int(time.time())))
        self.c.commit()

    def get_meta(self, key, default=None):
        r = self.c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key, value):
        self.c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
        self.c.commit()
