import pytest

from web_trx import station


@pytest.mark.parametrize("call", ["DA2JH", "da2jh", "DL1ABC/P", "9A1A", "W1AW", "PA/DA2JH", "HB9ABC/MM"])
def test_valid_callsigns(call):
    assert station.normalize(call, "")["call"] == call.upper()


@pytest.mark.parametrize("call", ["AB", "ABCDEFGHIJK", "DA2 JH", "DA-2JH", "ABCDE", "12345"])
def test_invalid_callsigns(call):
    with pytest.raises(ValueError):
        station.normalize(call, "")


@pytest.mark.parametrize("loc", ["JO43", "jo43", "JO43AB", "jo43ab", "AA00", "RR99XX"])
def test_valid_locators(loc):
    assert station.normalize("", loc)["locator"] == loc.upper()


@pytest.mark.parametrize("loc", ["JO4", "SO43", "JO4A", "JO43A", "JO43AY", "JO43AB12", "43JO"])
def test_invalid_locators(loc):
    with pytest.raises(ValueError):
        station.normalize("", loc)


def test_empty_means_not_set():
    assert station.normalize("  ", "") == {"call": "", "locator": ""}


def test_environment_start_values(monkeypatch):
    monkeypatch.setenv("WEB_TRX_STATION_CALL", "da2jh")
    monkeypatch.setenv("WEB_TRX_STATION_LOCATOR", "jo43")
    assert station.from_environment() == {"call": "DA2JH", "locator": "JO43"}
    monkeypatch.setenv("WEB_TRX_STATION_CALL", "not a call")
    assert station.from_environment() == {"call": "", "locator": ""}
