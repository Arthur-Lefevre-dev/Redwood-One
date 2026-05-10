"""Password strength rules for viewer (and other) accounts.

No spaces; password must include at least one lowercase letter, one uppercase letter,
one digit, and one special (non-alphanumeric) character. Only those character kinds
are allowed (letters, digits, punctuation/symbols — no whitespace).
"""

from __future__ import annotations

MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 128

# Exact-match blocklist (lowercase). Extend as needed.
_WEAK_EXACT = frozenset(
    {
        "password",
        "password1",
        "password12",
        "password123",
        "motdepasse",
        "motdepasse1",
        "azerty",
        "azerty123",
        "qwerty",
        "qwerty123",
        "admin",
        "admin123",
        "letmein",
        "welcome",
        "welcome123",
        "redwood",
        "redwood123",
    }
)

# Substrings that indicate trivial keyboard walks (lowercase check).
_TRIVIAL_SUBSTRINGS = (
    "qwerty",
    "azerty",
    "123456",
    "abcdef",
    "asdfgh",
)


def _allowed_char(c: str) -> bool:
    """Letter, digit, or non-alphanumeric symbol (no whitespace or control chars)."""
    if c.isspace() or not c.isprintable():
        return False
    return c.isalpha() or c.isdigit() or (not c.isalnum())


def validate_password_strength(
    password: str,
    *,
    username: str | None = None,
    email: str | None = None,
) -> str | None:
    """Return a French error message if invalid, or None if acceptable."""
    if not isinstance(password, str):
        return "Mot de passe invalide."

    if len(password) > MAX_PASSWORD_LEN:
        return f"Le mot de passe ne doit pas dépasser {MAX_PASSWORD_LEN} caractères."

    if any(c.isspace() for c in password):
        return "Les espaces ne sont pas autorisés dans le mot de passe."

    if len(password) < MIN_PASSWORD_LEN:
        return (
            f"Le mot de passe doit contenir au moins {MIN_PASSWORD_LEN} caractères "
            "(lettres, chiffres et symboles, sans espace)."
        )

    for c in password:
        if not _allowed_char(c):
            return "Utilisez uniquement des lettres, des chiffres et des symboles (pas d’espace ni de caractère de contrôle)."

    lower = password.lower()
    if lower in _WEAK_EXACT:
        return "Ce mot de passe est trop courant. Choisissez-en un autre."

    for sub in _TRIVIAL_SUBSTRINGS:
        if sub in lower:
            return "Évitez les suites de touches ou les séquences trop prévisibles (clavier, 123456…)."

    if password.isdigit():
        return "Le mot de passe ne doit pas être composé uniquement de chiffres."

    if len(set(password)) == 1:
        return "Le mot de passe doit contenir plusieurs caractères différents."

    has_lower = any(c.islower() for c in password)
    has_upper = any(c.isupper() for c in password)
    has_digit = any(c.isdigit() for c in password)
    has_special = any(not c.isalnum() for c in password)

    if not (has_lower and has_upper and has_digit and has_special):
        return (
            "Le mot de passe doit contenir au moins une minuscule, une majuscule, "
            "un chiffre et un symbole (par exemple ! ? # @ $ % & * …)."
        )

    user = (username or "").strip()
    if len(user) >= 3 and user.lower() in lower:
        return "Le mot de passe ne doit pas contenir votre identifiant."

    em = (email or "").strip().lower()
    if em and "@" in em:
        local = em.split("@", 1)[0]
        if len(local) >= 3 and local in lower:
            return "Le mot de passe ne doit pas contenir la partie locale de votre adresse e-mail."

    return None
