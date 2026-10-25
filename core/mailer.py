"""Sends sign in codes.

With RESEND_API_KEY (and optionally EMAIL_FROM) set, codes go to the student's inbox through Resend.
Without it the site runs in demo mode: the code is shown on screen and the page says so plainly.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def email_enabled() -> bool:
    return bool(os.environ.get("RESEND_API_KEY"))


def send_code(to: str, code: str) -> bool:
    """Returns True if an email was sent. False means demo mode (show the code on screen)."""
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        return False
    sender = os.environ.get("EMAIL_FROM", "Student Agents <onboarding@resend.dev>")
    body = json.dumps({
        "from": sender, "to": [to], "subject": f"Your Student Agents code: {code}",
        "text": f"Your Student Agents sign in code is {code}.\nIt expires in 10 minutes.\n\n"
                "If you didn't ask for this, you can ignore this email.",
    }).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": "student-agents/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"Couldn't send the email ({e}). Try again in a minute.") from e
