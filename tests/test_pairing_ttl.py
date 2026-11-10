import json
import time
import pytest
from rebot_teleop.auth import DEFAULT_PAIRING_TTL, Auth, pairing_ttl


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("REBOT_TELEOP_PAIRING_TTL", raising=False)


def test_the_default_is_five_minutes():
    assert pairing_ttl() == DEFAULT_PAIRING_TTL == 300


@pytest.mark.parametrize("value,expected", [("30", 30), ("3600", 3600), ("86400", 86400)])
def test_a_valid_window_is_honoured(monkeypatch, value, expected):
    monkeypatch.setenv("REBOT_TELEOP_PAIRING_TTL", value)
    assert pairing_ttl() == expected


@pytest.mark.parametrize("value", ["0", "29", "86401", "forever", "", "-1"])
def test_a_bad_window_falls_back_to_the_default(monkeypatch, value):
    monkeypatch.setenv("REBOT_TELEOP_PAIRING_TTL", value)
    assert pairing_ttl() == DEFAULT_PAIRING_TTL


def test_the_code_expiry_uses_the_window(monkeypatch, tmp_path):
    monkeypatch.setenv("REBOT_TELEOP_PAIRING_TTL", "3600")
    auth = Auth(tmp_path / ".env")
    before = time.time()
    auth.pair("operator")
    stored = json.loads(auth.values["REBOT_TELEOP_PAIRING"])
    assert stored["expires"] - before == pytest.approx(3600, abs=5)


def test_a_long_lived_code_still_logs_in(monkeypatch, tmp_path):
    monkeypatch.setenv("REBOT_TELEOP_PAIRING_TTL", "7200")
    auth = Auth(tmp_path / ".env")
    code = auth.pair("operator")
    assert auth.login(code, "10.0.0.5").role == "operator"


def test_a_code_is_still_single_use(monkeypatch, tmp_path):
    monkeypatch.setenv("REBOT_TELEOP_PAIRING_TTL", "7200")
    auth = Auth(tmp_path / ".env")
    code = auth.pair("operator")
    auth.login(code, "10.0.0.5")
    with pytest.raises(ValueError):
        auth.login(code, "10.0.0.6")


def test_rate_limiting_survives_a_long_window(monkeypatch, tmp_path):
    monkeypatch.setenv("REBOT_TELEOP_PAIRING_TTL", "86400")
    auth = Auth(tmp_path / ".env")
    auth.pair("operator")
    for _ in range(5):
        with pytest.raises(ValueError, match="invalid or expired"):
            auth.login("00000000", "10.0.0.9")
    with pytest.raises(ValueError, match="Too many pairing attempts"):
        auth.login("00000000", "10.0.0.9")
