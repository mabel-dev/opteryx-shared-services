import pytest

from opteryx_shared_services.usernames import InvalidUsername
from opteryx_shared_services.usernames import is_principal_shape
from opteryx_shared_services.usernames import is_valid_username
from opteryx_shared_services.usernames import normalise_username
from opteryx_shared_services.usernames import validate_username


@pytest.mark.parametrize(
    "raw, stored",
    [
        ("bastian", "bastian"),
        ("Bastian", "bastian"),
        ("  xb500 ", "xb500"),
        ("a_b_", "a_b_"),
        ("abcd", "abcd"),
        ("a" * 64, "a" * 64),
    ],
)
def test_valid_names_return_the_lowercase_form(raw, stored):
    assert validate_username(raw) == stored


def test_case_variants_share_one_stored_form():
    assert validate_username("Bastian") == validate_username("bastian")


@pytest.mark.parametrize(
    "raw, message",
    [
        ("", "is required"),
        ("   ", "is required"),
        ("ann-marie", "letters, numbers and underscores"),
        ("ann.marie", "letters, numbers and underscores"),
        ("ann marie", "letters, numbers and underscores"),
        ("carol\n", None),  # stripped, then valid
        ("аdmin", "letters, numbers and underscores"),  # Cyrillic a
        ("ａdmin", "letters, numbers and underscores"),  # full-width a
        ("Kelvin", "letters, numbers and underscores"),  # KELVIN SIGN lowercases to k
        ("1abc", "start with a letter"),
        ("_abc", "start with a letter"),
        ("abc", "between 4 and 64"),
        ("a" * 65, "between 4 and 64"),
    ],
)
def test_invalid_names_are_refused_with_a_clear_message(raw, message):
    if message is None:
        assert validate_username(raw) == "carol"
        return
    with pytest.raises(InvalidUsername, match=message):
        validate_username(raw)


def test_field_name_appears_in_the_message():
    with pytest.raises(InvalidUsername, match="^client_id may only"):
        validate_username("a-b-c", field_name="client_id")


def test_embedded_newline_is_refused():
    assert not is_valid_username("car\nol")


def test_normalise_does_not_validate():
    assert normalise_username(" Ann-Marie ") == "ann-marie"


@pytest.mark.parametrize("name", ["ann-marie", "Bastian", "abcd", "a" * 64, "x" + "-" * 10])
def test_existing_names_keep_their_shape(name):
    assert is_principal_shape(name)


@pytest.mark.parametrize(
    "name", ["", "abc", "a" * 65, "1abc", "-abc", "_abc", "a.bcd", "abcd\n", "\u0430dmin"]
)
def test_names_no_account_can_have_are_not_principals(name):
    assert not is_principal_shape(name)


def test_every_valid_new_username_is_also_a_principal_shape():
    for name in ["abcd", "a" * 64, "Ab_9", "xb500"]:
        assert is_principal_shape(validate_username(name))
