"""Junk scoring for the sms-bridge.

POSITIVE EVIDENCE ONLY. A message must earn quarantine; nothing has to prove it is
legitimate. A whitelist was tried and rejected for a good reason: 2FA senders rotate
numbers as freely as spammers do -- the Okta code that closed the login loop arrived
from a toll-free number never seen before -- so any list of known-good senders
protects only the messages that never needed protecting.

QUARANTINE, NOT BLOCK. Blocking destroys the evidence: 2,461 numbers blocked on the
phone left exactly one thread of content in the archive, which is why there is no
training data to learn from. Quarantine is reversible and accumulates labels.

Number-based blocking is abandoned as a strategy, and the archive says why: junk
threads went 18 (2021) -> 49 (2026 to date) *while* those 2,461 blocks were being
applied. Senders rotate numbers; the pitch is what persists.

COMPLIANCE BOILERPLATE IS STRIPPED BEFORE SCORING, and this is the least obvious part.
"Free Msg:", "Reply STOP to unsubscribe" and "message and data rates may apply" are
legally mandated text that legitimate senders -- banks especially -- are required to
carry. Scoring it selects for compliant senders rather than spam. Stripping it took
2FA misfires from 5 to 1; refining the vocabulary took it to 0 of 284.

Calibrated against ~284 2FA messages and ~65 junk hits in one archive, with the rules
fitted to that sample. The zero is a measurement, not a guarantee -- which is exactly
why the action is quarantine.
"""

import re

# Carrier and TCPA boilerplate. Presence of this is mild evidence of a COMPLIANT
# sender, so it must never contribute to a junk score.
BOILERPLATE = re.compile(
    r"(free\s?msg:?|reply\s+stop\b[^.]*|msg\s*(&|and)\s*data rates may apply|"
    r"message and data rates may apply|std(\s|\.)?msg|to unsubscribe[^.]*|"
    r"txt\s?stop\b[^.]*|reply\s+help\b[^.]*)",
    re.I,
)

CODEWORD = re.compile(
    r"\b(code|otp|passcode|verification|verify|2fa|one[- ]time|pin)\b", re.I)
DIGITS = re.compile(r"\b\d{4,8}\b")

# Each rule is (name, pattern, weight). Weights are coarse on purpose: with a sample
# this small, finer numbers would be false precision.
RULES = [
    ("sales_pitch", re.compile(
        r"\b(offer|deal|save \$|discount|limited time|act now|click here|"
        r"exclusive|winner|congratulations|special promotion|risk[- ]free|"
        r"no obligation|pre[- ]approved|lowest price)\b", re.I), 2),
    ("political", re.compile(
        r"\b(donate|donation|campaign|ballot|chip in|match(ed)? (your )?gift|"
        r"membership renewal|petition)\b", re.I), 2),
    ("cold_open", re.compile(
        r"\b(reaching out|following up|quick question|hope this finds|"
        r"we noticed (that )?your|are you (still )?(interested|looking))\b", re.I), 2),
    ("shortened_url", re.compile(
        r"\b(bit\.ly|tinyurl|goo\.gl|t\.co|is\.gd|buff\.ly|"
        r"[a-z0-9-]+\.(?:link|click|info|xyz|top|shop))\b", re.I), 2),
    ("voicemail_drop", re.compile(
        r"\bdeposited a new message\b", re.I), 3),
    ("debt_or_loan", re.compile(
        r"\b(debt relief|loan approval|refinanc|credit repair|settlement claim)\b",
        re.I), 2),
]

QUARANTINE_AT = 2          # one strong rule is enough; the action is reversible


def strip_boilerplate(body: str) -> str:
    return BOILERPLATE.sub(" ", body or "")


def looks_like_code(body: str) -> bool:
    """A real one-time code: a code WORD and a digit run.

    The word matters. Half the inbound messages carrying a 4-8 digit run are ordinary
    conversation -- URLs, addresses, order numbers -- so digits alone say nothing.
    """
    return bool(CODEWORD.search(body or "") and DIGITS.search(body or ""))


def score(body: str) -> tuple[int, list]:
    """(score, reasons) for one message body."""
    text = strip_boilerplate(body)
    hits = [(name, weight) for name, rx, weight in RULES if rx.search(text)]
    total = sum(w for _, w in hits)
    names = [n for n, _ in hits]
    if TRANSACTIONAL.search(text):
        # Offsets one ordinary rule, so a promo-flavoured shipping notice stays put
        # while something that trips several signals still gets through.
        total -= 2
        names.append("-transactional")
    return total, names


# Only the opt-out family counts as DISENGAGEMENT. "Yes" and "ok" are replies -- a
# doctor's office thread where nine appointment confirmations were answered "Yes" was
# quarantined because a 15-character minimum treated all of them as silence.
OPT_OUT = re.compile(r"^\s*(stop|stopall|unsubscribe|quit|cancel|end|remove|opt\s?out)\W*$",
                     re.I)

# Past-tense, account-specific notifications: orders, bills, appointments, refills.
# Scored NEGATIVE rather than used as a veto, because "your package could not be
# delivered" is also the commonest phishing opener -- a veto would be an invitation.
TRANSACTIONAL = re.compile(
    r"\b(your (order|bill|payment|appointment|package|prescription|refill|"
    r"account|delivery|reservation|statement|balance)|"
    r"has shipped|is paid|has been (delivered|received|scheduled|processed)|"
    r"thanks for (joining|your payment|your order)|receipt|confirmation number|"
    r"dose \d|is (ready|done))\b", re.I)


def classify(messages) -> dict:
    """Judge a conversation from its INBOUND messages.

    SCORED PER MESSAGE, NOT SUMMED OVER THE THREAD. Summing made length itself
    incriminating: a 6,891-message personal conversation accumulates "deal", "save"
    and "offer" eventually, and scored 20 -- higher than most actual spam. What
    matters is whether any single message reads like a pitch.

    Two vetoes, both from the thread's own content rather than from any list of
    known-good senders:

      * a real one-time code anywhere in the thread. Missing a login code is the
        expensive failure here, and a spammer padding one in is a problem for the
        day it happens, not today.
      * a substantive reply from you. "STOP" does not count -- replying STOP is the
        commonest thing done to junk, and counting it as engagement would make the
        clearest junk look legitimate.

    Outbound is otherwise excluded from scoring entirely.
    """
    inbound = [m for m in messages if m.get("dir") != "out"]
    outbound = [m for m in messages if m.get("dir") == "out"]
    if not inbound:
        return {"junk": False, "score": 0, "reasons": [], "has_code": False,
                "engaged": False}

    best, reasons = 0, []
    for m in inbound:
        s, r = score(m.get("body") or "")
        if s > best:
            best, reasons = s, r

    has_code = any(looks_like_code(m.get("body") or "") for m in inbound)
    # Any reply that is not an opt-out is engagement, however short.
    engaged = any((m.get("body") or "").strip() and not OPT_OUT.match(m.get("body") or "")
                  for m in outbound)

    return {
        "junk": best >= QUARANTINE_AT and not has_code and not engaged,
        "score": best,
        "reasons": sorted(set(reasons)),
        "has_code": has_code,
        "engaged": engaged,
    }
