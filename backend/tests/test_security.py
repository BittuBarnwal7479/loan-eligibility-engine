import time

from app.security import constant_time_equals, mask_email, sign_upload, verify_upload_signature


def test_upload_signature_roundtrip_and_tamper():
    expires = int(time.time()) + 60
    sig = sign_upload("s", "batch", expires)
    assert verify_upload_signature("s", "batch", expires, sig)
    assert not verify_upload_signature("s", "other-batch", expires, sig)
    assert not verify_upload_signature("wrong", "batch", expires, sig)
    assert not verify_upload_signature("s", "batch", expires + 1, sig)


def test_expired_signature_rejected():
    expires = int(time.time()) - 1
    assert not verify_upload_signature("s", "batch", expires, sign_upload("s", "batch", expires))


def test_token_comparison():
    assert constant_time_equals("abc", "abc")
    assert not constant_time_equals("abc", "abd")
    assert not constant_time_equals(None, "abc")
    assert not constant_time_equals("abc", None)


def test_mask_email():
    assert mask_email("alice@example.com") == "a***@example.com"
    assert mask_email("broken") == "***"
