import pytest
from cryptography.fernet import Fernet

from account_console.security import SecretBox, SessionSigner


def test_secret_box_encrypts_and_decrypts_values():
    box = SecretBox(Fernet.generate_key().decode("ascii"))
    ciphertext = box.encrypt("MailPassword123!")

    assert b"MailPassword123!" not in ciphertext
    assert box.decrypt(ciphertext) == "MailPassword123!"
    with pytest.raises(ValueError, match="encrypted_value_invalid"):
        box.decrypt(b"not-ciphertext")


def test_session_signer_rejects_tampered_and_expired_tokens():
    signer = SessionSigner("s" * 48, ttl_seconds=10)
    token = signer.issue(now=100)

    assert signer.verify(token, now=105)
    assert not signer.verify(token, now=111)
    assert not signer.verify(token[:-4] + "AAAA", now=105)
