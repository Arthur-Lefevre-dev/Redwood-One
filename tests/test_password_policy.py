"""Tests for password_policy.validate_password_strength."""

from core.password_policy import validate_password_strength


def test_too_short():
    assert validate_password_strength("Abcdef1!") is not None
    assert "12" in (validate_password_strength("Abcdef1!") or "")


def test_accept_strong_mixed():
    assert validate_password_strength("MyS3cure#Pass") is None


def test_no_spaces():
    assert validate_password_strength("MyS3cure# Pass") is not None
    assert validate_password_strength("correcthorseX1!") is None


def test_requires_all_four_classes():
    assert validate_password_strength("abcdefghijkl") is not None  # only lower
    assert validate_password_strength("Abcdefghijkl") is not None  # lower + upper
    assert validate_password_strength("KlmnoPqr5stu") is not None  # missing special
    assert validate_password_strength("KlmnoPqr5stu!") is None


def test_weak_exact():
    assert validate_password_strength("passwordpassword") is not None


def test_username_in_password():
    assert (
        validate_password_strength("alice-MyStr0ng!Pw", username="alice") is not None
    )


def test_email_local_in_password():
    assert (
        validate_password_strength("superX1!Ysecret", email="superX@mail.com")
        is not None
    )


def test_digits_only():
    assert validate_password_strength("123456789012") is not None


def test_repeated_char():
    assert validate_password_strength("aaaaaaaaaaaa") is not None


def test_leading_trailing_space():
    assert validate_password_strength(" Abcdefgh1!Xy ") is not None
