"""Conversation type for customer email: one topic label per message, plus a separate escalation flag."""
from . import rules

# AI topic -> label suffix. "general_follow_up" gets no label (it is the catch-all).
SUFFIX = {
    "poc_or_pilot": "POC",
    "rfi_or_rfp": "RFP",
    "pricing_or_contract": "Pricing-Contract",
    "technical_question": "Technical",
    "scheduling_or_meeting": "Scheduling",
}


def label_names(cfg):
    prefix = cfg["topics"]["prefix"]
    return [f"{prefix}/{s}" for s in SUFFIX.values()] + [f"{prefix}/Escalation"]


def assess(cfg, clf, m, me, known=(True, False)):
    """-> (label names to add, topic, topic probability, escalation probability). Empty if not applicable."""
    t = cfg["topics"]
    if not t["enabled"] or not rules.eligible_for_task_check(m, me):
        return [], None, None, None
    probs = clf.topic_probs(m, me, *known)
    topic = max(probs, key=probs.get)
    names = []
    if topic in SUFFIX and probs[topic] >= t["min_conf"]:
        names.append(f"{t['prefix']}/{SUFFIX[topic]}")
    esc = clf.escalation_prob(m, me, *known)
    if esc >= t["escalation_threshold"]:
        names.append(f"{t['prefix']}/Escalation")
    return names, topic, probs[topic], esc
