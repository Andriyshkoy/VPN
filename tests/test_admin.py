import pytest
from fastapi import HTTPException

from admin.app import parse
from admin.schemas import ServerCreate, TopUp


class DummyRequest:
    def __init__(self, data):
        self.json = data


def test_parse_valid():
    req = DummyRequest(
        {"name": "srv", "ip": "1.1.1.1", "host": "h", "location": "us", "api_key": "k"}
    )
    model = parse(ServerCreate, req)
    assert model.name == "srv" and model.port == 22


def test_parse_invalid():
    req = DummyRequest({"name": "srv"})
    with pytest.raises(HTTPException):
        parse(ServerCreate, req)


def test_topup_model():
    data = TopUp(amount=5.5)
    assert data.amount == 5.5
