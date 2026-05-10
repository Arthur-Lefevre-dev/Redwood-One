"""Strict e-mail validation for viewer registration without sending verification mail.

Uses syntax checks plus DNS deliverability (MX / A for the domain). Rejects a curated
set of disposable / throwaway domains.
"""

from __future__ import annotations

from email_validator import EmailNotValidError, validate_email

# Domains that accept mail but should not be used for real accounts (exact or subdomain).
_DISPOSABLE_DOMAINS = frozenset(
    {
        "yopmail.com",
        "yopmail.fr",
        "mailinator.com",
        "guerrillamail.com",
        "guerrillamailblock.com",
        "grr.la",
        "sharklasers.com",
        "pokemail.net",
        "spam4.me",
        "10minutemail.com",
        "10minutemail.net",
        "tempmail.com",
        "tempmail.org",
        "temp-mail.org",
        "throwaway.email",
        "maildrop.cc",
        "getnada.com",
        "trashmail.com",
        "mailnesia.com",
        "dispostable.com",
        "fakeinbox.com",
        "mohmal.com",
        "emailondeck.com",
        "getairmail.com",
        "burnermail.io",
        "trashmail.de",
        "mailcatch.com",
        "mintemail.com",
        "mytrashmail.com",
    }
)


def _domain_is_disposable(domain: str) -> bool:
    d = domain.lower().strip(".")
    if d in _DISPOSABLE_DOMAINS:
        return True
    return any(d == base or d.endswith("." + base) for base in _DISPOSABLE_DOMAINS)


def validate_viewer_email(email: str) -> tuple[str | None, str | None]:
    """Return (normalized_email, None) if valid, or (None, french_error_message)."""
    if not isinstance(email, str):
        return None, "Adresse e-mail invalide."

    raw = email.strip()
    if not raw:
        return None, "Adresse e-mail requise."

    try:
        info = validate_email(
            raw,
            allow_smtputf8=True,
            check_deliverability=True,
            test_environment=False,
        )
    except EmailNotValidError:
        return None, (
            "Adresse e-mail invalide ou domaine injoignable. "
            "Vérifiez la syntaxe et que le domaine existe (ex. pas de faute dans gmail.com)."
        )

    normalized = info.email
    domain = normalized.rsplit("@", 1)[-1]
    if _domain_is_disposable(domain):
        return None, "Les adresses e-mail jetables ou temporaires ne sont pas acceptées."

    return normalized, None
