"""Rules run before the model. They only ever decide 'keep' (never reach the model) or 'filter' (blocklist)."""
import re
from dataclasses import dataclass

# Looks like a hotel: in the sender's name or domain (citizenM and similar chains are on the allowlist).
HOTEL_RE = re.compile(r"hotel|\binn\b|resort|suites|lodge|hospitality|hostel|motel|citizenm", re.I)
# Travel reservation: a travel word AND either a confirmation-number pattern or a booking-event subject.
TRAVEL_RE = re.compile(r"hotel|\bflights?\b|airline|\broom\b|\bstay\b|itinerary|boarding|passenger|departure|arrival|"
                       r"check-?in|check-?out|reservation|booking|e-?ticket|\bPNR\b", re.I)
CONFNUM_RE = re.compile(r"(?:confirmation|booking|reservation|record|ticket|reference|itinerary)\s*"
                        r"(?:number|no\.?|#|code|ref(?:erence)?\.?|locator|id)\s*[:#\-]?\s*(?-i:[A-Z0-9]{5,12})\b|\bPNR\b\s*[:#\-]?\s*(?-i:[A-Z0-9]{5,8})\b", re.I)
_BOOK = r"(?:reservation|booking|itinerary|e-?ticket|boarding pass|flight|stay)"
_EVENT = r"(?:confirm\w*|modif\w*|chang\w*|cancel\w*|receipt|details|reminder|updat\w*|number|reference)"
BOOKING_SUBJECT_RE = re.compile(rf"\b{_BOOK}\b.*\b{_EVENT}\b|\b{_EVENT}\b.*\b{_BOOK}\b", re.I)
INVOICE_RE = re.compile(r"\binvoices?\b", re.I)


@dataclass
class Verdict:
    action: str      # keep | filter
    reason: str      # e.g. rule:own_domain


def _domain_match(domain, entry_domain):
    return domain == entry_domain or domain.endswith("." + entry_domain)


def _match_list(m, entries, need_dmarc):
    """Match sender against a list of '@domain' / full-address entries."""
    for e in entries:
        if e.startswith("@"):
            if _domain_match(m.domain, e[1:]) and (not need_dmarc or m.dmarc == "pass"):
                return e
        elif e == m.from_addr:
            return e
    return None


def evaluate(m, ctx):
    """ctx: me (my addresses), keep_domains, own_domains, allow (set), learned_allow, block, learned_block, contacts, thread_has_sent(fn)."""
    if "STARRED" in m.labels:
        return Verdict("keep", "rule:starred")
    # Anything from my own company is kept unless Gmail says it is an impersonation (dmarc=fail).
    if any(_domain_match(m.domain, d) for d in ctx["own_domains"]) and m.dmarc != "fail":
        return Verdict("keep", "rule:own_domain")
    if _match_list(m, ctx["allow"] | ctx["learned_allow"], need_dmarc=True):
        return Verdict("keep", "rule:allowlist")
    if m.dmarc != "fail" and any(_domain_match(m.domain, d) for d in ctx.get("customer_domains", ())):
        return Verdict("keep", "rule:customer")
    if m.dmarc != "fail" and any(_domain_match(m.domain, d) for d in ctx["keep_domains"]):
        return Verdict("keep", "rule:keep_domain")
    if _match_list(m, ctx["block"] | ctx["learned_block"], need_dmarc=False):
        return Verdict("filter", "rule:blocklist")
    # A colleague (anyone at my own company's domain, other than me) on the message means it is a real conversation.
    me = m.mine(ctx["me"])
    colleagues = {a for a in m.participants if a not in me
                  and any(_domain_match(a.rsplit("@", 1)[-1], d) for d in ctx["own_domains"])}
    if colleagues:
        return Verdict("keep", "rule:colleague_on_message")
    # Hotels and anything that says "invoice" are mail I need (expenses, bookings). Impersonation (dmarc=fail) still
    # goes to the model, because fake invoices are a classic phishing trick.
    if m.dmarc != "fail":
        if HOTEL_RE.search(m.from_name) or HOTEL_RE.search(m.domain):
            return Verdict("keep", "rule:hotel")
        if INVOICE_RE.search(m.subject) or INVOICE_RE.search(m.body[:300]):
            return Verdict("keep", "rule:invoice")
        text = m.subject + "\n" + m.body[:3000]
        if BOOKING_SUBJECT_RE.search(m.subject) or (TRAVEL_RE.search(text) and CONFNUM_RE.search(text)):
            return Verdict("keep", "rule:travel_reservation")
    if m.from_addr in ctx["contacts"] and m.dmarc != "fail":
        return Verdict("keep", "rule:known_contact")
    if m.has_ics:
        return Verdict("keep", "rule:calendar_invite")
    if ctx["thread_has_sent"](m.thread_id):
        return Verdict("keep", "rule:thread_i_replied_in")
    return None


AUTOMATED_SENDER_RE = re.compile(r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?|alerts?|mailer|bounces?|"
                                 r"updates?|news|newsletter|info-noreply|support-noreply)\b", re.I)


def eligible_for_task_check(m, me):
    """Only personal, non-automated mail is checked for a question or task aimed at me."""
    if m.from_addr in m.mine(me) or m.has_ics:
        return False
    if AUTOMATED_SENDER_RE.match(m.from_addr.split("@")[0]):
        return False
    h = m.headers
    if h.get("list-unsubscribe") or h.get("list-id"):
        return False
    if h.get("precedence", "").lower() in ("bulk", "list", "junk"):
        return False
    if h.get("auto-submitted", "no").lower() != "no":
        return False
    return True


SURVEY_RE = re.compile(r"tell us about|survey|feedback|how was your|rate your|review your|how did we do", re.I)


def travel_reason(m, travel_domains):
    """'travel_booking' / 'travel_sender' / None: is this a hotel, flight or travel email I should not miss?

    A booking or reservation counts whoever sends it. A hotel-looking or known travel sender counts unless it is a
    mass mailing (survey, promotion). Impersonation (dmarc=fail) never counts."""
    if m.dmarc == "fail" or SURVEY_RE.search(m.subject):
        return None
    text = m.subject + "\n" + m.body[:3000]
    if BOOKING_SUBJECT_RE.search(m.subject) or (TRAVEL_RE.search(text) and CONFNUM_RE.search(text)):
        return "travel_booking"
    from_travel = (HOTEL_RE.search(m.from_name) or HOTEL_RE.search(m.domain)
                   or any(_domain_match(m.domain, d) for d in travel_domains))
    bulk = m.headers.get("list-unsubscribe") or m.headers.get("list-id")
    if from_travel and not bulk:
        return "travel_sender"
    return None
