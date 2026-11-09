import pytest

import tokens

K = "test-secret"


def test_roundtrip():
    t = tokens.sign({"m": 42, "s": "outlet.example.com"}, K)
    assert tokens.verify(t, K) == {"m": 42, "s": "outlet.example.com"}


def test_tampered_token_rejected():
    t = tokens.sign({"m": 42}, K)
    body, sig = t.split(".")
    with pytest.raises(ValueError):
        tokens.verify(body[:-2] + "AA." + sig, K)


def test_wrong_secret_rejected():
    with pytest.raises(ValueError):
        tokens.verify(tokens.sign({"m": 1}, K), "other")


@pytest.mark.parametrize("junk", ["", "abc", "a.b.c", "....", "%%%.%%%"])
def test_garbage_rejected(junk):
    with pytest.raises(ValueError):
        tokens.verify(junk, K)


def test_empty_secret_refuses_to_sign():
    with pytest.raises(ValueError):
        tokens.sign({"m": 1}, "")


def test_visitor_token_roundtrip_and_rejects_other_tokens():
    t = tokens.visitor(42, "k")
    assert tokens.visitor_profile(t, "k") == 42
    assert tokens.visitor_profile(t, "other") is None
    assert tokens.visitor_profile(tokens.sign({"m": 1, "a": "open"}, "k"), "k") is None
    assert tokens.visitor_profile("garbage", "k") is None
    assert tokens.visitor_profile(tokens.sign({"p": "1", "a": "id"}, "k"), "k") is None   # p must be an int
