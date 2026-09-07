"""Transactional email over SMTP.

The brief asked for Nodemailer; this backend is Python, so this is the direct
equivalent — `aiosmtplib` over STARTTLS, reading the same environment
variables (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`).

Two rules hold everywhere in this module:

* Credentials come from settings only. Nothing here is ever logged, returned
  from an endpoint, or included in an exception message.
* Sending never breaks the request that triggered it. A signup succeeds even if
  the mail server is down; the caller gets `False` and the user can resend. In
  development, with no SMTP configured, the message is logged instead so the
  flow is still testable.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import Any

import aiosmtplib

from app.config import get_settings

log = logging.getLogger("criclab.email")

# --------------------------------------------------------------------------- #
# Branding — one shell, so every message looks like the product
# --------------------------------------------------------------------------- #

_NIGHT = "#05090a"
_CARD = "#0f1a1b"
_LIME = "#b6f24a"
_CHALK = "#f6f9f7"
_MUTED = "#8a9a93"


def _shell(title: str, body_html: str, *, preheader: str = "") -> str:
    """Wrap content in the CricLab email frame.

    Table-based and inline-styled on purpose: email clients are not browsers,
    and the modern CSS the website uses would collapse in most of them.
    """
    return f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:{_NIGHT};font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
    <span style="display:none;font-size:0;line-height:0;max-height:0;opacity:0;overflow:hidden;">{preheader}</span>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_NIGHT};padding:32px 16px;">
      <tr><td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;">
          <tr><td style="padding-bottom:22px;">
            <span style="display:inline-block;width:34px;height:34px;border-radius:9px;background:{_LIME};text-align:center;line-height:34px;font-weight:800;color:{_NIGHT};font-size:16px;">C</span>
            <span style="color:{_CHALK};font-size:19px;font-weight:800;letter-spacing:-0.3px;padding-left:9px;vertical-align:middle;">Cric<span style="color:{_LIME};">Lab</span></span>
          </td></tr>
          <tr><td style="background:{_CARD};border:1px solid rgba(255,255,255,0.08);border-radius:16px;padding:32px;">
            <h1 style="margin:0 0 16px;color:{_CHALK};font-size:22px;font-weight:800;letter-spacing:-0.4px;">{title}</h1>
            {body_html}
          </td></tr>
          <tr><td style="padding-top:22px;color:{_MUTED};font-size:11px;line-height:1.6;">
            CricLab — the cricket performance lab.<br>
            This is an automated message. If you did not expect it, you can ignore it safely.
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


def _p(text: str) -> str:
    return f'<p style="margin:0 0 14px;color:rgba(246,249,247,0.72);font-size:14px;line-height:1.65;">{text}</p>'


def _code_block(code: str) -> str:
    return (
        f'<div style="margin:22px 0;padding:18px;border-radius:12px;background:rgba(182,242,74,0.09);'
        f'border:1px solid rgba(182,242,74,0.28);text-align:center;">'
        f'<div style="color:{_LIME};font-size:32px;font-weight:800;letter-spacing:8px;font-family:monospace;">{code}</div>'
        f"</div>"
    )


def _button(label: str, url: str) -> str:
    return (
        f'<div style="margin:24px 0;"><a href="{url}" '
        f'style="display:inline-block;background:{_LIME};color:{_NIGHT};text-decoration:none;'
        f'font-weight:700;font-size:14px;padding:13px 26px;border-radius:999px;">{label}</a></div>'
        f'<p style="margin:0;color:{_MUTED};font-size:11px;word-break:break-all;">'
        f"If the button does not work, paste this into your browser:<br>{url}</p>"
    )


def _detail_rows(rows: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<tr><td style="padding:7px 0;color:{_MUTED};font-size:12px;text-transform:uppercase;'
        f'letter-spacing:1px;width:42%;">{k}</td>'
        f'<td style="padding:7px 0;color:{_CHALK};font-size:14px;font-weight:600;">{v}</td></tr>'
        for k, v in rows
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:18px 0;border-top:1px solid rgba(255,255,255,0.08);">'
        f"{cells}</table>"
    )


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

async def send_email(to: str, subject: str, html: str, *, text: str | None = None) -> bool:
    """Deliver one message. Returns whether it was accepted by the server.

    Never raises: callers are request handlers whose own success does not depend
    on the mail server being reachable.
    """
    settings = get_settings()
    if not settings.email_configured:
        # Development fallback. The body is logged, never the credentials.
        log.warning("email not configured — would have sent %r to %s", subject, to)
        return False

    message = EmailMessage()
    message["From"] = f"{settings.email_from_name} <{settings.email_from}>"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text or _strip_tags(html))
    message.add_alternative(html, subtype="html")

    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=settings.smtp_pass,
            start_tls=settings.smtp_port == 587,
            use_tls=settings.smtp_port == 465,
            timeout=20,
        )
        log.info("sent %r to %s", subject, _mask(to))
        return True
    except Exception as exc:
        # Log the class of failure, never the password or the full SMTP dialogue.
        log.error("email send failed for %s: %s", _mask(to), type(exc).__name__)
        return False


def _mask(address: str) -> str:
    """t****@example.com — enough to correlate logs, not enough to harvest."""
    name, _, domain = address.partition("@")
    if not domain:
        return "***"
    return f"{name[:1]}***@{domain}"


def mask_email(address: str) -> str:
    return _mask(address)


def _strip_tags(html: str) -> str:
    import re

    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #

async def send_verification_otp(to: str, name: str, code: str, minutes: int) -> bool:
    html = _shell(
        "Confirm your email",
        _p(f"Hi {name}, enter this code in CricLab to finish setting up your account.")
        + _code_block(code)
        + _p(f"The code expires in {minutes} minutes. If it lapses, request a new one from the app."),
        preheader=f"Your CricLab verification code is {code}",
    )
    return await send_email(to, "Your CricLab verification code", html)


async def send_welcome(to: str, name: str) -> bool:
    url = f"{get_settings().app_base_url}/app/action"
    html = _shell(
        "Welcome to CricLab",
        _p(f"Your email is confirmed, {name} — CricLab is ready.")
        + _p(
            "Film one delivery, side-on, with the whole body in frame. You will get the "
            "marked-up slow-motion clip, the measured numbers, and a clear read on what to work on."
        )
        + _button("Analyse a delivery", url)
        + _p("New to filming for analysis? The filming guide in the app takes two minutes."),
        preheader="Your CricLab account is ready.",
    )
    return await send_email(to, "Welcome to CricLab", html)


async def send_password_reset_otp(to: str, name: str, code: str, minutes: int) -> bool:
    html = _shell(
        "Reset your password",
        _p(f"Hi {name}, use this code to set a new CricLab password.")
        + _code_block(code)
        + _p(f"The code expires in {minutes} minutes and can be used once.")
        + _p("If you did not ask to reset your password, ignore this email — nothing has changed."),
        preheader=f"Your CricLab password reset code is {code}",
    )
    return await send_email(to, "Reset your CricLab password", html)


async def send_password_changed(to: str, name: str) -> bool:
    html = _shell(
        "Your password was changed",
        _p(f"Hi {name}, the password on your CricLab account was just changed.")
        + _p(
            "Signing in again on your other devices will be required. If this was not you, "
            "reset your password immediately and contact support."
        )
        + _button("Open CricLab", f"{get_settings().app_base_url}/login"),
        preheader="Your CricLab password was changed.",
    )
    return await send_email(to, "Your CricLab password was changed", html)


async def send_new_login_alert(to: str, name: str, when: str, context: str) -> bool:
    html = _shell(
        "New sign-in to your account",
        _p(f"Hi {name}, your CricLab account was signed in to.")
        + _detail_rows([("When", when), ("Device", context)])
        + _p("If this was you, nothing to do. If not, change your password straight away."),
        preheader="New sign-in to your CricLab account.",
    )
    return await send_email(to, "New sign-in to CricLab", html)


async def send_ticket_created(to: str, name: str, ticket_id: str, subject: str) -> bool:
    url = f"{get_settings().app_base_url}/app/support/{ticket_id}"
    html = _shell(
        "We have your request",
        _p(f"Thanks {name} — your support request is logged and someone will pick it up.")
        + _detail_rows([("Reference", ticket_id), ("Subject", subject), ("Status", "Open")])
        + _button("View the ticket", url),
        preheader=f"CricLab support ticket {ticket_id} created.",
    )
    return await send_email(to, f"[{ticket_id}] {subject}", html)


async def send_ticket_updated(to: str, name: str, ticket_id: str, subject: str, status: str) -> bool:
    url = f"{get_settings().app_base_url}/app/support/{ticket_id}"
    html = _shell(
        "Your ticket was updated",
        _p(f"Hi {name}, there is an update on your support request.")
        + _detail_rows([("Reference", ticket_id), ("Subject", subject), ("Status", status)])
        + _button("Read the update", url),
        preheader=f"Update on CricLab ticket {ticket_id}.",
    )
    return await send_email(to, f"[{ticket_id}] Update — {subject}", html)


async def send_ticket_resolved(to: str, name: str, ticket_id: str, subject: str) -> bool:
    url = f"{get_settings().app_base_url}/app/support/{ticket_id}"
    html = _shell(
        "Your ticket is resolved",
        _p(f"Hi {name}, we have marked this request resolved.")
        + _detail_rows([("Reference", ticket_id), ("Subject", subject), ("Status", "Resolved")])
        + _p("If it is not sorted, reply on the ticket and it will reopen.")
        + _button("View the ticket", url),
        preheader=f"CricLab ticket {ticket_id} resolved.",
    )
    return await send_email(to, f"[{ticket_id}] Resolved — {subject}", html)


async def send_booking_confirmed(
    to: str, name: str, coach: str, when: str, duration: str, focus: str
) -> bool:
    url = f"{get_settings().app_base_url}/app/coaching"
    html = _shell(
        "Your session is booked",
        _p(f"Hi {name}, your coaching session is confirmed.")
        + _detail_rows([("Coach", coach), ("When", when), ("Length", duration), ("Focus", focus)])
        + _p("Bring a recent clip if you have one — the session works best with footage to look at.")
        + _button("View your sessions", url),
        preheader=f"Coaching with {coach} confirmed for {when}.",
    )
    return await send_email(to, f"Booked: {coach}, {when}", html)


async def send_booking_cancelled(to: str, name: str, coach: str, when: str, reason: str = "") -> bool:
    url = f"{get_settings().app_base_url}/app/coaching"
    html = _shell(
        "Your session was cancelled",
        _p(f"Hi {name}, this coaching session has been cancelled.")
        + _detail_rows([("Coach", coach), ("When", when)] + ([("Reason", reason)] if reason else []))
        + _button("Book another slot", url),
        preheader=f"Coaching with {coach} on {when} cancelled.",
    )
    return await send_email(to, f"Cancelled: {coach}, {when}", html)


async def send_booking_rescheduled(to: str, name: str, coach: str, old: str, new: str) -> bool:
    url = f"{get_settings().app_base_url}/app/coaching"
    html = _shell(
        "Your session moved",
        _p(f"Hi {name}, your coaching session has been rescheduled.")
        + _detail_rows([("Coach", coach), ("Was", old), ("Now", new)])
        + _button("View your sessions", url),
        preheader=f"Coaching with {coach} moved to {new}.",
    )
    return await send_email(to, f"Moved: {coach} is now {new}", html)


async def send_booking_reminder(to: str, name: str, coach: str, when: str) -> bool:
    url = f"{get_settings().app_base_url}/app/coaching"
    html = _shell(
        "Session coming up",
        _p(f"Hi {name}, a reminder that your session with {coach} is {when}.")
        + _button("View your sessions", url),
        preheader=f"Reminder: {coach}, {when}.",
    )
    return await send_email(to, f"Reminder: {coach}, {when}", html)


async def send_system_notice(to: str, name: str, title: str, body: str) -> bool:
    html = _shell(title, _p(f"Hi {name},") + _p(body), preheader=title)
    return await send_email(to, f"CricLab — {title}", html)


def render_preview(kind: str, **kw: Any) -> str:
    """Render a template to HTML without sending. Used by tests and previews."""
    if kind == "verification":
        return _shell("Confirm your email", _p("Preview") + _code_block(kw.get("code", "123456")))
    return _shell(kw.get("title", "CricLab"), _p(kw.get("body", "")))
