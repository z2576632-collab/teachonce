"""Secrets scanning tests (AGENTS.md safety red line #1)."""

from __future__ import annotations

import pytest

from teachonce.errors import SensitiveContentError
from teachonce.security import ensure_no_secrets, scan_for_secrets


def test_scan_finds_openai_style_key():
    hits = scan_for_secrets("here is my key sk-abcdefghijklmnop1234")
    assert any("sk-" in h for h in hits)


def test_scan_finds_password_assignment():
    hits = scan_for_secrets("password: hunter2secret")
    assert hits


def test_scan_finds_bearer_token():
    hits = scan_for_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456")
    assert any("bearer" in h.lower() for h in hits)


def test_scan_finds_github_token():
    hits = scan_for_secrets("token ghp_abcdefghijklmnopqrstuvwx1234567890")
    assert hits


def test_scan_finds_private_key_block():
    hits = scan_for_secrets("-----BEGIN RSA PRIVATE KEY-----\nMIIE...")
    assert any("private key" in h for h in hits)


def test_clean_text_passes():
    assert scan_for_secrets("把课件按课程整理，旧版本放入历史版本文件夹") == []


def test_ensure_no_secrets_raises_with_context():
    with pytest.raises(SensitiveContentError) as exc:
        ensure_no_secrets("api_key=abcdef123456", where="demonstration")
    assert "demonstration" in str(exc.value)
    assert "Remove the sensitive fragment" in str(exc.value)


def test_ensure_no_secrets_passes_clean():
    ensure_no_secrets("普通的示范文本，没有敏感信息", where="correction")
