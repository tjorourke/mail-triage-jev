"""Sample emails for `triage selftest`. (expected action, Mail). All senders and companies are fictional."""
from .gmail import Mail

ME = "me@example.com"


def mk(frm, name, subject, body, bulk=False, dmarc="pass", cat=None, links=(), cc=""):
    to = f"Alex Example <{ME}>"
    h = {"authentication-results": f"mx.google.com; dmarc={dmarc}", "from": f"{name} <{frm}>", "to": to, "cc": cc}
    if bulk:
        h["list-unsubscribe"] = "<mailto:u@x>"
    return Mail(id="x", thread_id="t", labels={"INBOX"} | ({f"CATEGORY_{cat}"} if cat else set()), from_addr=frm,
                from_name=name, to=to, cc=cc, subject=subject, headers=h, body=body, link_domains=list(links))


SAMPLES = [
    # --- junk ---
    ("filter", mk("jordan@brightpath-consulting.example", "Jordan Lee", "Re: Quick call?",
                  "Last follow-up from me.\n\nOn Fri, September 11, 2026 4:07 PM, Jordan Lee wrote:\n> Should we set up a quick call?\n\n"
                  "On Tue, September 8, 2026 3:10 PM, Jordan Lee wrote:")),
    ("filter", mk("sam@datalabel-pro.example", "Sam Carter", "program for Principal Architects",
                  "Hi Alex, we're looking for architects interested in evaluating our AI data annotation solution at no charge.\n"
                  "You'll have access to advanced enterprise-level tagging: object segmentation, LiDAR, radar, and pose estimation.\n"
                  "Send us one of your toughest datasets, we will deliver precise annotations with 99% accuracy.\n"
                  "Would you be interested in seeing how it works?", links=["datalabel-pro.example"])),
    ("filter", mk("sam@growthleadsrv.example", "Sam", "{{First Name}}, 3x your pipeline in 30 days",
                  "Hi Alex, I noticed your company is hiring SDRs. We book 15 qualified meetings a month for B2B SaaS, guaranteed. "
                  "Open to a 15 minute chat Thursday? If not, who is the right person?", bulk=True)),
    ("filter", mk("support@paypa1-secure-login.example", "PayPal", "Your account is limited - verify now",
                  "We noticed unusual activity. Confirm your identity within 24 hours or your account will be closed.",
                  dmarc="fail", links=["paypa1-secure-login.example"])),
    ("filter", mk("deals@shoeoutlet.example", "Shoe Outlet", "48 hours only: 60% off everything",
                  "Our biggest sale of the year. Shop now. Unsubscribe anytime.", bulk=True, cat="PROMOTIONS")),
    # --- events and webinars you want ---
    ("keep", mk("noreply@events.example", "Events Platform", "RSVP: October 20th LONDON",
                "You're invited: an evening of talks on data, context and AI at enterprise scale. Doors 6pm. RSVP now.", bulk=True)),
    ("keep", mk("events@bigconference.example", "Big Conference", "Big Conference 2026: your pass and agenda",
                "Join us in Copenhagen. See the agenda and book your sessions.", bulk=True)),
    ("keep", mk("webinars@somevendor.example", "Vendor Webinars", "Webinar: securing MCP servers in production, Thursday 3pm",
                "Join our live webinar on securing MCP servers. Register to get the recording.", bulk=True)),
    # --- mail to keep ---
    ("keep", mk("no-reply@airline.example", "Example Airlines", "Your e-ticket receipt for EA 1381 on 14 Oct",
                "Thank you for booking. Your booking reference is 7XK2LP. Check-in opens 30 hours before departure.")),
    ("keep", mk("receipts@aivendor.example", "AI Vendor", "Your API receipt #2482-4036-8799",
                "Thanks for your payment of $24.00 USD. This is your receipt for API usage credits.")),
    ("keep", mk("notifications@codehost.example", "CodeHost", "[org/project] PR #4821: fix routing",
                "A reviewer requested changes on your pull request.", bulk=True)),
    ("keep", mk("priya.nair@somebank.example", "Priya Nair", "Re: Thursday's POC review",
                "Hi Alex, thanks for the walkthrough yesterday. Our security team has a few questions about the mTLS setup. "
                "Could you join a call at 3pm Thursday with our platform lead? I've attached the questions.")),
    ("keep", mk("family@example.net", "Family", "Sunday lunch?", "Are you and the kids coming on Sunday? We're doing a roast. x")),
    ("keep", mk("alerts@hosting.example", "Hosting", "Your SSL certificate renews in 7 days",
                "Your SSL certificate for example.org will renew automatically on 10 Oct.")),
]
