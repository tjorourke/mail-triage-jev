"""Clef-flash wrapper. The model returns a probability for each category in one forward pass (no text generation)."""
import sys
import time

CATEGORIES = {
    "real_work_or_personal": (
        "A real person who already knows the recipient, or a message the recipient is expecting: "
        "a colleague, customer, partner, friend or family member, or a genuine reply in an existing conversation."),
    "transactional_or_account": (
        "Automated mail about the recipient's own accounts or purchases from a service they use: receipts, "
        "invoices, bookings, flights, shipping, security alerts, password resets, billing notices, renewals."),
    "work_notification": (
        "Automated notifications from work tools: GitHub, Jira, Slack, Google Docs/Drive, calendar, CI, "
        "monitoring, support-ticket updates."),
    "newsletter": (
        "Editorial content the recipient plausibly subscribed to: newsletters, digests, blogs, "
        "industry roundups, community updates."),
    "event_or_webinar": (
        "Invitations, RSVPs, reminders and recordings for events the recipient may want to attend: industry "
        "conferences (KubeCon, vendor and cloud events), meetups, roundtables, dinners, "
        "Eventbrite events and webinars, including vendor webinars. The recipient wants these."),
    "marketing": (
        "Promotional mail from a company: offers, discounts, sales, product announcements, "
        "sponsored content, 'we thought you might like'. Not events or webinars."),
    "cold_sales_outreach": (
        "Unsolicited message from a stranger, vendor or agency who wants to sell something or book a call or "
        "demo. This includes sequences of 'follow-ups' ('last follow-up', 'bumping this', 'should we set up a "
        "quick call?'), free trials/evaluations/pilots, offers to review a dataset or codebase, lead-generation, "
        "outsourcing, staffing, recruiting-agency and data-annotation pitches. It is cold outreach even when it "
        "uses the recipient's name or job title, looks personal, or pastes a fake quoted 'On <date> ... wrote:' history."),
    "spam_or_phishing": (
        "Scams, phishing, fake invoices or delivery notices, impersonation, crypto or investment schemes, "
        "adult content, lottery or prize messages, malware."),
}

def instructions(cfg):
    """Context for the junk-vs-real judgement, built from the [profile] section of config.toml."""
    p = cfg["profile"]
    return (
        f"The recipient is a {p['role']} at {p['company']}, {p['company_summary']}. "
        "Decide what kind of email this is. Mail from large, well-known companies the recipient does business with "
        f"({p['trusted_examples']}) about their own account is transactional, "
        "not spam. A stranger offering a service, a free trial or a call is cold_sales_outreach. "
        "Mail addressed only to the recipient, with nobody else in To or Cc, from someone they have never "
        "corresponded with, is more likely to be cold outreach or spam; mail that also names colleagues is rarely spam."
    )


ACTION_QUESTION = {
    "type": "noul",
    "instructions": (
        "Does this email contain a direct question for the recipient to answer, or ask the recipient to do "
        "something (reply, decide, review, approve, send, sign, attend, fix, prepare) or to meet a deadline? "
        "It must be aimed at the recipient personally. Greetings, rhetorical questions, thanks, and general "
        "calls to action sent to many people do not count."),
}


def customer_question(cfg):
    """Is this a conversation with a customer or prospect of the recipient's company? Built from [profile]."""
    p = cfg["profile"]
    c = p["company"]
    return {
        "type": "noul",
        "instructions": (
            f"Is this email part of a conversation between {c} and an external company that is a customer or prospect "
            f"of {c}, where the external company is evaluating, buying or using {c}'s products or services? Signs: "
            "a proof of concept (POC) or pilot; an RFI or RFP; an evaluation; technical or architecture questions about "
            f"{c}'s products ({p['products']}) or asking {c} for help or support; workshops, demos, enablement sessions; "
            "pricing, licences, contracts or procurement; arranging meetings with the sales or engineering team. "
            f"The email may be written by the external company or by a {c} employee writing to them. It is NOT a "
            f"customer if the external company is trying to sell something TO {c} (vendors, agencies, recruiters, lead "
            "generation, outsourcing, tools offering to evaluate or help), or if the mail is automated, a newsletter or "
            "a notification."),
    }


def build_state(m, body_chars, me=frozenset(), known_sender=False, replied_in_thread=False):
    body = m.body[:body_chars]
    return {
        "from_name": m.from_name,
        "from_address": m.from_addr,
        "to": m.to[:200],
        "cc": m.cc[:200],
        "addressed_only_to_recipient": m.only_to_me(me),   # nobody else in To/Cc
        "number_of_other_recipients": len(m.recipients - m.mine(me)),
        "subject": m.subject,
        "sent_to_me_in_bulk": bool(m.headers.get("list-unsubscribe") or m.headers.get("list-id")),
        "gmail_category": next((l.replace("CATEGORY_", "").lower() for l in m.labels if l.startswith("CATEGORY_")), "primary"),
        "sender_authenticated_dmarc": m.dmarc,
        # For mail the junk filter judges this is False/False (the rules already ruled out anyone I know).
        # For mail from people I know (importance check) it is the real answer.
        "recipient_has_ever_emailed_this_sender": known_sender,
        "recipient_has_replied_in_this_thread": replied_in_thread,
        "link_domains": m.link_domains,
        "body": body,
    }


class Classifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.model = self.processor = None
        self.last_used = 0.0

    def _load(self):
        import torch
        from transformers.utils import logging as hf_logging
        hf_logging.disable_progress_bar()
        hf_logging.set_verbosity_error()
        path = str(self.cfg["root"] / self.cfg["model"]["path"])
        sys.path.insert(0, path)
        from joint_schema_model import load_release_model
        t = time.time()
        self.model, self.processor = load_release_model(path, device=self.cfg["model"]["device"])
        self.model.eval()
        print(f"[model] loaded Clef-flash on {self.cfg['model']['device']} in {time.time() - t:.1f}s", file=sys.stderr)

    def action_prob(self, m, me=frozenset(), known_sender=False, replied_in_thread=False):
        """Probability that the email asks the recipient a question or sets them a task."""
        if self.model is None:
            self._load()
        import torch
        from joint_schema_model import systemone
        request = {"model": "clef-flash", "state": build_state(m, self.cfg["model"]["body_chars"], me, known_sender, replied_in_thread),
                   "questions": {"needs_action": ACTION_QUESTION}}
        self.last_used = time.time()
        with torch.inference_mode():
            resp = systemone(self.model, self.processor, request,
                             max_length=self.cfg["model"]["max_state_tokens"] + 1500)
        return resp["answers"]["needs_action"]["noul"]

    def customer_prob(self, m, me=frozenset(), known_sender=False, replied_in_thread=False):
        """Probability that this is a conversation with a customer or prospect."""
        if self.model is None:
            self._load()
        import torch
        from joint_schema_model import systemone
        request = {"model": "clef-flash", "state": build_state(m, self.cfg["model"]["body_chars"], me, known_sender, replied_in_thread),
                   "questions": {"customer": customer_question(self.cfg)}}
        self.last_used = time.time()
        with torch.inference_mode():
            resp = systemone(self.model, self.processor, request,
                             max_length=self.cfg["model"]["max_state_tokens"] + 1500)
        return resp["answers"]["customer"]["noul"]

    def unload(self):
        if self.model is None:
            return
        import gc
        import torch
        self.model = self.processor = None
        gc.collect()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        print("[model] unloaded (idle)", file=sys.stderr)

    def classify(self, m, me=frozenset(), known_sender=False, replied_in_thread=False):
        """Return {category: probability}."""
        if self.model is None:
            self._load()
        import torch
        from joint_schema_model import systemone
        request = {
            "model": "clef-flash",
            "state": build_state(m, self.cfg["model"]["body_chars"], me, known_sender, replied_in_thread),
            "questions": {
                "category": {"type": "choice", "instructions": instructions(self.cfg), "criteria": CATEGORIES},
            },
        }
        self.last_used = time.time()
        with torch.inference_mode():
            resp = systemone(self.model, self.processor, request,
                             max_length=self.cfg["model"]["max_state_tokens"] + 1500)
        return resp["answers"]["category"]["probabilities"]


def decide(probs, cfg, threshold=None):
    """Turn category probabilities into (action, confidence, top_category).

    The probability of an action is the sum over its categories, so 'marketing 0.5 + cold sales 0.45'
    still counts as 0.95 junk. The email is only moved when one non-keep action reaches the threshold.
    """
    mass = {}
    for cat, p in probs.items():
        a = cfg["actions"].get(cat, "keep")
        mass[a] = mass.get(a, 0.0) + p
    top_cat = max(probs, key=probs.get)
    best = max((a for a in mass if a != "keep"), key=lambda a: mass[a], default=None)
    if best and mass[best] >= (threshold if threshold is not None else cfg["threshold"]):
        return best, mass[best], top_cat
    return "keep", mass.get("keep", 0.0), top_cat


def junk_mass(probs, cfg):
    """Highest combined probability of any non-keep action (what the threshold is compared with)."""
    mass = {}
    for cat, p in probs.items():
        a = cfg["actions"].get(cat, "keep")
        if a != "keep":
            mass[a] = mass.get(a, 0.0) + p
    return max(mass.values(), default=0.0)
