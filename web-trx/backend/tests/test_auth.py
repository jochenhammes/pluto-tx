from web_trx.auth import AuthManager


def test_check_password():
    auth = AuthManager(password="s3cret")
    assert auth.check_password("s3cret") is True
    assert auth.check_password("wrong") is False
    assert auth.check_password("") is False


def test_issued_token_validates():
    auth = AuthManager(password="s3cret")
    token = auth.issue_token()
    assert auth.validate(token) is True


def test_unknown_or_missing_token_does_not_validate():
    auth = AuthManager(password="s3cret")
    assert auth.validate("not-a-real-token") is False
    assert auth.validate(None) is False
    assert auth.validate("") is False


def test_revoke_invalidates_token():
    auth = AuthManager(password="s3cret")
    token = auth.issue_token()
    assert auth.validate(token) is True
    auth.revoke(token)
    assert auth.validate(token) is False


def test_revoke_unknown_token_is_a_no_op():
    auth = AuthManager(password="s3cret")
    auth.revoke("never-issued")  # must not raise


def test_expired_token_does_not_validate():
    auth = AuthManager(password="s3cret", token_ttl_s=0)
    token = auth.issue_token()
    assert auth.validate(token) is False


def test_no_password_argument_falls_back_to_a_generated_one(capsys, monkeypatch):
    monkeypatch.delenv("WEB_TRX_PASSWORD", raising=False)
    auth = AuthManager()
    assert auth.password  # something non-empty was generated
    captured = capsys.readouterr()
    assert "generated one-time password" in captured.err


def test_sessions_survive_a_restart_when_persisted(tmp_path):
    path = str(tmp_path / "sessions.json")
    token = AuthManager(password="s3cret", sessions_path=path).issue_token()
    assert token not in (tmp_path / "sessions.json").read_text()  # only hashes are stored
    restarted = AuthManager(password="s3cret", sessions_path=path)
    assert restarted.validate(token) is True
    restarted.revoke(token)
    assert AuthManager(password="s3cret", sessions_path=path).validate(token) is False
