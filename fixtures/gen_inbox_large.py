"""Generate fixtures/inbox_large.json: the five real messages buried in 115 of noise.

Lesson 85 needs an inbox big enough that reading it costs real context. The
noise is deterministic (seeded), long-bodied, and realistic enough that a
classifier has to actually read it: newsletters, promos, CI notifications,
calendar auto-replies, receipts, and six extra human messages that DO need a
reply, scattered through the week so the triage job is not trivial.

Run:  python fixtures/gen_inbox_large.py   (idempotent; output is committed)
"""
from __future__ import annotations

import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
rng = random.Random(85)

base = json.loads((HERE / "inbox.json").read_text())

NEEDS_REPLY = [
    ("lena@brightpath.design", "Logo revisions - which direction?",
     "Hi! Attached are three directions for the refreshed logo. Direction B keeps the current mark but modernises the type; C is the bolder departure we discussed. I need a pick by Thursday to stay on schedule for the print run. Any direction is fine, I just need a decision. Thanks, Lena"),
    ("coach.ramos@example.org", "Saturday practice moved to 9am - can you still drive?",
     "Hi all, the field is double-booked so Saturday practice moves from 10 to 9am. I have you down for carpool that morning. Can you confirm you can still make the earlier time? If not I will ask one of the other parents. Thanks, Coach Ramos"),
    ("devi@neighbourhood-watch.example", "Quick question about the shared fence",
     "Hello neighbour, the fence between our yards lost two boards in the storm. I got a quote for $380 to replace the section. Happy to split it 50/50 if that works for you. Let me know and I will book them for next week. Devi"),
    ("hr-benefits@corp.example", "ACTION REQUIRED: confirm beneficiary by 9/12",
     "Our records show your beneficiary designation has not been reviewed this year. Please log in to the benefits portal and confirm or update it by September 12. This takes about two minutes. If no action is taken the current designation stays in place. HR Benefits Team"),
    ("marcus@local-news.example", "Interview request - 15 minutes this week?",
     "Hi, I am writing a short piece on neighbourhood volunteer groups and your name came up twice. Would you have 15 minutes for a phone call this week? Any afternoon works. If not, a couple of sentences by email would also be great. Marcus"),
    ("tenant.j@example.com", "Heating making a clicking sound",
     "Hi, the heating unit has started making a clicking noise when it kicks on, maybe every 20 minutes. It still works fine. Not urgent but wanted to flag it before it gets colder. Should I call someone or will you arrange it? Thanks, J"),
]

NEWSLETTERS = [
    ("weekly@techdigest.example", "This week in AI: five things that mattered"),
    ("editor@longform.example", "Sunday Longreads: the quiet history of the shipping container"),
    ("news@citycouncil.example", "Council newsletter - September edition"),
    ("hello@recipebox.example", "7 weeknight dinners under 30 minutes"),
    ("team@productivityapp.example", "What's new: templates, dark mode, and faster sync"),
    ("digest@industrynews.example", "Procurement Weekly: tariffs, timelines and three case studies"),
]
PROMOS = [
    ("deals@megastore.example", "Last chance: 60% off everything in outdoor"),
    ("offers@airline.example", "Fares from $59 - book by Friday"),
    ("promo@coffeeclub.example", "Your September roast is here (plus 20% off)"),
    ("sale@shoeshop.example", "New arrivals + free shipping this week only"),
    ("club@gymchain.example", "Bring a friend free in September"),
]
AUTOMATED = [
    ("ci@github-notify.example", "[dayflow] CI passed on main (#{n})"),
    ("ci@github-notify.example", "[dayflow] CI failed on branch feature/{w} (#{n})"),
    ("calendar-noreply@example.com", "Accepted: Weekly staff sync"),
    ("calendar-noreply@example.com", "Updated invitation: 1:1 with Priya"),
    ("receipts@rideshare.example", "Your Tuesday trip receipt"),
    ("noreply@utility.example", "Your statement is ready"),
    ("alerts@bank.example", "A payment of ${amt} was made from your account"),
    ("noreply@pharmacy.example", "Your prescription is ready for pickup"),
    ("security@cloudhost.example", "New sign-in to your account from Chrome on macOS"),
    ("support@saas.example", "[Ticket #{n}] We've received your request"),
]

PARAS = [
    "If you no longer wish to receive these emails you can update your preferences at any time from the link at the bottom of this message.",
    "This message was sent automatically. Replies to this address are not monitored, so please use the help centre if you need assistance.",
    "We have been working hard on this release and we think you are going to like what we shipped. Read on for the highlights, or jump straight into the app.",
    "Thanks for being part of the community. We send one of these a week and never share your address with anyone.",
    "Terms and conditions apply. Offer valid while stocks last and cannot be combined with other discounts or promotional codes.",
    "You are receiving this because you signed up on our website or made a purchase with us in the last twelve months.",
    "Highlights this week include three long reads, one interview and a short note from the editor about what we are planning for the autumn.",
    "To view this message in your browser, click the link at the top of the email. Images may not display correctly in some clients.",
]

def body(n_paras: int) -> str:
    return " ".join(rng.choice(PARAS) for _ in range(n_paras))

def stamp(day: int, hour: int, minute: int) -> str:
    return f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00Z"

msgs: list[dict] = list(base)
i = len(base)

for frm, subj, b in NEEDS_REPLY:
    i += 1
    msgs.append({"id": f"m{i}", "from": frm, "subject": subj,
                 "date": stamp(rng.randint(1, 7), rng.randint(7, 20), rng.randint(0, 59)), "body": b})

while len(msgs) < 120:
    i += 1
    kind = rng.choice(["news", "news", "promo", "auto", "auto", "auto"])
    if kind == "news":
        frm, subj = rng.choice(NEWSLETTERS)
        b = body(rng.randint(4, 7))
    elif kind == "promo":
        frm, subj = rng.choice(PROMOS)
        b = body(rng.randint(3, 5))
    else:
        frm, subj = rng.choice(AUTOMATED)
        subj = subj.format(n=rng.randint(100, 999), w=rng.choice(["store", "resume", "hooks", "zod"]), amt=f"{rng.randint(12, 240)}.{rng.randint(0, 99):02d}")
        b = body(rng.randint(1, 3))
    msgs.append({"id": f"m{i}", "from": frm, "subject": subj,
                 "date": stamp(rng.randint(1, 7), rng.randint(0, 23), rng.randint(0, 59)), "body": b})

(HERE / "inbox_large.json").write_text(json.dumps(msgs, indent=1) + "\n")
print(f"wrote {len(msgs)} messages, {sum(len(m['body']) for m in msgs)} body chars")
