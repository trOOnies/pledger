"""Tests for keeping other sites out of pledger.web."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import MOVEMENT

from pledger.web.app import create_app
from pledger.web.routers import server
from pledger.web.store import (
    LOCAL_HOSTS,
    LedgerStore,
    allowed_hosts,
)


class TestSameSiteOnly:
    def test_refuses_other_hosts(self, client):
        r = client.get("/", headers={"Host": "evil.example"})
        assert r.status_code == 403
        assert client.get("/api/summary", headers={"Host": "evil.example:8000"}).json() == {
            "detail": "Host not allowed: evil.example:8000"
        }
        assert client.get("/", headers={"Host": "127.0.0.1:8000"}).status_code == 200
        assert client.get("/", headers={"Host": "[::1]:8000"}).status_code == 200

    def test_any_host_when_allowed(self, settings):
        client = TestClient(create_app(replace(settings, allowed_hosts=None)))
        assert client.get("/", headers={"Host": "pledger.lan"}).status_code == 200

    def test_refuses_changes_from_other_origins(self, client, settings, monkeypatch):
        monkeypatch.setattr(server, "stop_server", lambda: None)  # never stop the tests
        before = len(LedgerStore(settings).load().rows)
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Referer": "https://evil.example/page"}):
            r = client.post("/api/movements", json=MOVEMENT, headers=headers)
            assert r.status_code == 403
            assert r.json() == {"detail": "Cross-origin request refused"}
        assert client.post("/quit", headers={"Origin": "http://127.0.0.1:8000"}).status_code == 403
        assert len(LedgerStore(settings).load().rows) == before
        # Reading from another origin is left to the browser (CORS), and the Host check
        assert client.get("/", headers={"Origin": "https://evil.example"}).status_code == 200

    def test_lets_same_origin_and_scripts_through(self, client, settings):
        before = len(LedgerStore(settings).load().rows)
        same = {"Origin": "http://testserver", "Referer": "http://testserver/"}
        assert client.post("/api/movements", json=MOVEMENT, headers=same).status_code == 201
        assert client.post("/api/movements", json=MOVEMENT).status_code == 201  # like curl
        assert len(LedgerStore(settings).load().rows) == before + 2

    @pytest.mark.parametrize(("text", "hosts"), [
        ("", LOCAL_HOSTS), (" , ", LOCAL_HOSTS), ("*", None),
        ("pledger.lan, [::1] ,LOCALHOST", ("pledger.lan", "::1", "localhost")),
    ])
    def test_allowed_hosts_env(self, text, hosts):
        assert allowed_hosts(text) == hosts
