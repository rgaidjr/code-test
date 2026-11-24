import json

import pytest
from fastapi.testclient import TestClient

import api
import cloud_runtime


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "bearer")
    monkeypatch.setenv("SERVICE_API_TOKEN", "local-test-token")
    monkeypatch.delenv("APP_SECRET_ARN", raising=False)
    monkeypatch.setattr(api, "ensure_database", lambda: None)
    cloud_runtime.load_cloud_secrets.cache_clear()
    with TestClient(api.app) as connection:
        yield connection


def test_authentication_and_fail_closed_configuration(client, monkeypatch):
    assert client.get("/health").status_code == 200
    assert client.get("/dataset").status_code == 401
    assert client.get("/docs").status_code == 401
    monkeypatch.setattr(api, "describe_dataset", lambda: {"source": "test.csv"})
    assert client.get("/dataset", headers={"Authorization": "Bearer local-test-token"}).json() == {"source": "test.csv"}
    monkeypatch.delenv("SERVICE_API_TOKEN")
    assert client.get("/dataset", headers={"Authorization": "Bearer local-test-token"}).status_code == 401


def test_iam_mode_does_not_trust_an_http_header(client, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "aws_iam")
    assert client.get("/dataset", headers={"X-Aws-User-Arn": "arn:aws:iam::123:user/admin"}).status_code == 401


def test_queries_validate_inputs_and_handle_expected_failures(client, monkeypatch):
    headers = {"Authorization": "Bearer local-test-token"}
    monkeypatch.setattr(api, "query_dataset", lambda sql: {"rows": [[1]], "sql": sql})
    response = client.post("/query", headers=headers, json={"sql": "SELECT 1"})
    assert response.json()["rows"] == [[1]]
    assert response.headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store"
    assert client.post("/query", headers=headers, json={"sql": "x", "extra": 1}).status_code == 422

    def rejected(sql):
        raise ValueError("Read-only query rejected")

    monkeypatch.setattr(api, "query_dataset", rejected)
    assert client.post("/query", headers=headers, json={"sql": "DELETE"}).status_code == 400


def test_unexpected_errors_do_not_leak_secrets(client, monkeypatch):
    def broken():
        raise RuntimeError("a-secret-that-must-not-leak")

    monkeypatch.setattr(api, "describe_dataset", broken)
    response = client.get("/dataset", headers={"Authorization": "Bearer local-test-token"})
    assert response.status_code == 500
    assert "a-secret" not in response.text


def test_investigation_requires_configured_key(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    response = client.post("/investigate", headers={"Authorization": "Bearer local-test-token"}, json={"question": "Analyze"})
    assert response.status_code == 503


def test_cloud_secret_is_cached_and_only_known_keys_are_loaded(monkeypatch):
    calls = []

    class FakeSecrets:
        def get_secret_value(self, **arguments):
            calls.append(arguments)
            return {"SecretString": json.dumps({"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "test-model", "UNRELATED": "ignore"})}

    monkeypatch.setenv("APP_SECRET_ARN", "test-arn")
    monkeypatch.setattr(cloud_runtime.boto3, "client", lambda name: FakeSecrets())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("UNRELATED", raising=False)
    cloud_runtime.load_cloud_secrets.cache_clear()
    cloud_runtime.load_cloud_secrets()
    cloud_runtime.load_cloud_secrets()
    import os
    assert os.environ["OPENAI_API_KEY"] == "test-key"
    assert os.environ["OPENAI_MODEL"] == "test-model"
    assert "UNRELATED" not in os.environ
    assert calls == [{"SecretId": "test-arn"}]
    cloud_runtime.load_cloud_secrets.cache_clear()
