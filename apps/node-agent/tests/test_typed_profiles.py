"""Service-specific profile formats remain private explicit exports."""

import base64
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from vpn_control.app import create_app
from vpn_control.common import BackendAccess
from vpn_control.config import Principal, Settings, token_hash
from vpn_control.models import ProfileResponse

TOKEN = "typed-node-profile-" + "q" * 40
READER = "typed-node-reader-" + "r" * 40
SECRET = "synthetic-profile-secret"


def content():
    return json.dumps(
        {
            "outbounds": [
                {
                    "type": "hysteria2",
                    "tag": "Hysteria",
                    "server": "node.invalid",
                    "server_port": 1935,
                    "password": "synthetic-user:" + SECRET,
                    "obfs": {"type": "salamander", "password": "synthetic-obfs"},
                    "tls": {
                        "enabled": True,
                        "server_name": "node.invalid",
                        "insecure": False,
                        "certificate_sha256": [base64.b64encode(b"x" * 32).decode()],
                    },
                }
            ]
        }
    )


class Backend:
    def __init__(self, service):
        self.service = service
        self.rows = {}

    def create(self, username, **policy):
        self.rows[username] = BackendAccess(
            "active", 0, policy["traffic_limit_bytes"], policy["expires_at"]
        )
        return self.rows[username]

    def get(self, username):
        return self.rows.get(username)

    def profile(self, username):
        scheme = "hysteria2" if self.service == "hysteria" else "vless"
        return f"{scheme}://{SECRET}@node.invalid:1935"

    def profile_json(self, username):
        assert username in self.rows
        return content()

    def health(self):
        return {"ready": True, "data_plane_verified": False}


@pytest.fixture
def client(tmp_path):
    principals = (
        Principal(
            "operator",
            token_hash(TOKEN),
            frozenset({"access:read", "access:write", "profiles:read"}),
        ),
        Principal("reader", token_hash(READER), frozenset({"access:read"})),
    )
    settings = Settings(tmp_path / "typed-control.sqlite3", principals=principals)
    app = create_app(
        settings,
        {service: Backend(service) for service in ("hysteria", "vless-reality")},
    )
    with TestClient(app) as test_client:
        yield test_client, settings


def create(client, service):
    response = client.post(
        "/v1/accesses",
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Idempotency-Key": "typed-" + service,
        },
        json={"service_id": service},
    )
    assert response.status_code == 201
    return response.json()["access"]["id"]


@pytest.mark.parametrize("service", ["hysteria", "vless-reality"])
def test_default_profile_format_is_appropriate_for_hiddify(client, service):
    client, settings = client
    access_id = create(client, service)
    response = client.get(
        f"/v1/accesses/{access_id}/profile",
        headers={"Authorization": "Bearer " + TOKEN},
    )
    assert response.status_code == 200
    result = response.json()
    if service == "hysteria":
        assert result["format"] == "singbox-json"
        assert result["uri"] is None
        assert json.loads(result["content"]) == json.loads(content())
    else:
        assert result["format"] == "uri" and result["uri"].startswith("vless://")
        assert result.get("content") is None
    assert SECRET not in settings.database.read_text(errors="ignore")
    assert response.headers["cache-control"] == "no-store"
    assert (
        client.get(
            f"/v1/accesses/{access_id}/profile",
            headers={"Authorization": "Bearer " + READER},
        ).status_code
        == 403
    )
    assert (
        SECRET
        not in client.get(
            "/v1/accesses", headers={"Authorization": "Bearer " + READER}
        ).text
    )


def test_native_uri_export_is_an_explicit_request(client):
    client, _ = client
    access_id = create(client, "hysteria")
    result = client.get(
        f"/v1/accesses/{access_id}/profile?format=native-uri",
        headers={"Authorization": "Bearer " + TOKEN},
    ).json()
    assert result["format"] == "uri" and result["uri"].startswith("hysteria2://")
    assert result.get("content") is None


@pytest.mark.parametrize(
    "values",
    [
        {
            "format": "singbox-json",
            "uri": "hysteria2://secret@node:1935",
            "content": content(),
        },
        {"format": "singbox-json", "uri": None, "content": None},
        {"format": "uri", "uri": "vless://secret@node:9443", "content": content()},
    ],
)
def test_profile_response_has_exactly_one_explicit_secret_representation(values):
    with pytest.raises(ValidationError):
        ProfileResponse(
            access_id="fixture-id",
            service_id="hysteria",
            contains_secret=True,
            **values,
        )
