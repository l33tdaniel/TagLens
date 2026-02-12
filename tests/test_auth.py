from auth import generate_csrf_token, generate_session_token, verify_csrf_token, verify_session_token


def test_session_token_hash_verification() -> None:
    session = generate_session_token()
    assert verify_session_token(session.token, session.token_hash)
    assert not verify_session_token("invalid", session.token_hash)


def test_csrf_tokens_match() -> None:
    token = generate_csrf_token()
    assert verify_csrf_token(token, token)
    assert not verify_csrf_token(token, "wrong")
