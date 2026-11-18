"""The username rule, in one place.

A username is the `clients/{id}` document id, the JWT `sub`, and the second
part of `personal.{username}.*` in SQL -- so the rule has to suit all three:

- ASCII letters, digits and underscore only. A hyphen is not a legal unquoted
  SQL identifier character, and anything non-ASCII risks a name that renders
  like somebody else's.
- Starts with a letter, so `personal.{username}` never needs quoting.
- 4 to 64 characters.
- Case-insensitive. The stored and displayed form is lowercase, and because
  the lowercase form IS the document id, uniqueness on it is the document's
  existence -- `Bastian` and `bastian` are the same document.

The same rule applies to human accounts and service accounts: they share one
namespace.

Characters are checked BEFORE lowercasing. `str.lower` is Unicode-aware, and
some non-ASCII characters lowercase into ASCII (KELVIN SIGN U+212A becomes
`k`), so lowercasing first would let a name in that only looks valid
afterwards.

This governs names being CREATED. Accounts created under older, looser rules
(hyphens, mixed-case service accounts) keep working: refusing them at sign-in
would lock their owners out, not migrate them.
"""

import re

MIN_LENGTH = 4
MAX_LENGTH = 64

# `\Z`, not `$`: `$` also matches before a trailing newline.
_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")

# What an EXISTING principal may look like, for code that accepts a name rather
# than creating one (sign-in, routes addressed by `sub`). Wider than the rule
# above: it still admits the hyphens and mixed case that older accounts were
# created with, so tightening creation does not lock anyone out. The length
# ceiling is the new one, so names created under the new rule are admitted too.
PRINCIPAL_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{%d,%d}\Z" % (MIN_LENGTH - 1, MAX_LENGTH - 1))


class InvalidUsername(ValueError):
    """The name breaks the rule. `str(exc)` is a message fit to show a user."""


def normalise_username(raw: str) -> str:
    """The stored form of `raw`: whitespace trimmed, lowercased. Does not validate."""
    return (raw or "").strip().lower()


def validate_username(raw: str, *, field_name: str = "username") -> str:
    """Check `raw` against the rule and return its stored (lowercase) form.

    Raises:
        InvalidUsername: with a message naming `field_name` and what is wrong.
    """
    value = (raw or "").strip()
    if not value:
        raise InvalidUsername(f"{field_name} is required")
    if not value.isascii() or not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise InvalidUsername(
            f"{field_name} may only contain letters, numbers and underscores"
        )
    if not _PATTERN.match(value):
        raise InvalidUsername(f"{field_name} must start with a letter")
    if not MIN_LENGTH <= len(value) <= MAX_LENGTH:
        raise InvalidUsername(
            f"{field_name} must be between {MIN_LENGTH} and {MAX_LENGTH} characters long"
        )
    return value.lower()


def is_principal_shape(value: str) -> bool:
    """Whether `value` could name an existing account (see PRINCIPAL_PATTERN).

    Not normalised: a legacy mixed-case service account is addressed by its
    exact id.
    """
    return bool(value) and bool(PRINCIPAL_PATTERN.match(value))


def is_valid_username(raw: str) -> bool:
    try:
        validate_username(raw)
    except InvalidUsername:
        return False
    return True


__all__ = [
    "MIN_LENGTH",
    "MAX_LENGTH",
    "PRINCIPAL_PATTERN",
    "InvalidUsername",
    "is_principal_shape",
    "normalise_username",
    "validate_username",
    "is_valid_username",
]
