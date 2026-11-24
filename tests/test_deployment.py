import asyncio
import json

import pytest

from scripts import release
from scripts.cloud_client import call
from scripts.container_smoke import event
from scripts.dataset import digest, validate
from scripts.bootstrap import stack_owns_oidc_provider
from scripts.scan_image import scan


class MissingAlias(Exception):
    pass


def test_ecr_scan_is_started_once_and_missing_results_never_pass():
    class ScanMissing(Exception):
        pass

    class Client:
        class exceptions:
            ScanNotFoundException = ScanMissing

        def __init__(self):
            self.reads = 0
            self.starts = 0

        def describe_image_scan_findings(self, **kwargs):
            self.reads += 1
            if self.reads <= 2:
                raise ScanMissing()
            return {"imageScanStatus": {"status": "COMPLETE"}, "imageScanFindings": {"findingSeverityCounts": {"LOW": 2}}}

        def start_image_scan(self, **kwargs):
            assert kwargs == {"repositoryName": "philgeps", "imageId": {"imageDigest": "sha256:example"}}
            self.starts += 1

    client = Client()
    assert scan(client, "philgeps", "sha256:example", attempts=3, delay=0) == {"LOW": 2}
    assert client.starts == 1
    with pytest.raises(TimeoutError):
        scan(Client(), "philgeps", "sha256:example", attempts=1, delay=0)


def test_ecr_high_severity_findings_block_a_release():
    class Client:
        def describe_image_scan_findings(self, **kwargs):
            return {"imageScanStatus": {"status": "COMPLETE"}, "imageScanFindings": {"findingSeverityCounts": {"HIGH": 1}}}

    with pytest.raises(RuntimeError, match="HIGH/CRITICAL"):
        scan(Client(), "philgeps", "digest", attempts=1, delay=0)


def test_bootstrap_preserves_the_existing_stack_owned_oidc_provider():
    class Paginator:
        def paginate(self, **kwargs):
            assert kwargs == {"StackName": "foundation"}
            return [{"StackResourceSummaries": [{"LogicalResourceId": "DatasetBucket", "ResourceStatus": "CREATE_COMPLETE"}]},
                    {"StackResourceSummaries": [{"LogicalResourceId": "GitHubOidcProvider", "ResourceStatus": "CREATE_COMPLETE"}]}]

    class Client:
        def get_paginator(self, operation):
            assert operation == "list_stack_resources"
            return Paginator()

    assert stack_owns_oidc_provider(Client(), "foundation") is True


class FakeLambda:
    class exceptions:
        ResourceNotFoundException = MissingAlias

    def __init__(self, previous="1"):
        self.aliases = {"live": {"FunctionVersion": previous, "RevisionId": "initial"}} if previous else {}
        self.deleted_urls = []

    def get_alias(self, FunctionName, Name):
        if Name not in self.aliases:
            raise MissingAlias()
        return dict(self.aliases[Name])

    def create_alias(self, FunctionName, Name, FunctionVersion):
        self.aliases[Name] = {"FunctionVersion": FunctionVersion, "RevisionId": "new"}

    def update_alias(self, FunctionName, Name, FunctionVersion, RevisionId, RoutingConfig):
        assert RevisionId == self.aliases[Name]["RevisionId"]
        self.aliases[Name] = {"FunctionVersion": FunctionVersion, "RevisionId": "updated"}

    def delete_alias(self, FunctionName, Name):
        del self.aliases[Name]

    def delete_function_url_config(self, FunctionName, Qualifier):
        self.deleted_urls.append(Qualifier)


@pytest.mark.parametrize("failure", ["candidate", "live"])
def test_failed_smoke_checks_preserve_previous_live_version(monkeypatch, failure):
    client = FakeLambda()
    monkeypatch.setattr(release, "url_for", lambda client, function, alias: alias)

    def smoke(url, *args):
        if url == failure:
            raise RuntimeError("Smoke check failed")

    monkeypatch.setattr(release, "smoke", smoke)
    with pytest.raises(RuntimeError, match="Smoke"):
        release.promote(client, "service", "2", "region", "revision", "sha", 1)
    assert client.aliases["live"]["FunctionVersion"] == "1"


def test_first_release_failure_removes_the_broken_live_endpoint(monkeypatch):
    client = FakeLambda(previous=None)
    monkeypatch.setattr(release, "url_for", lambda client, function, alias: alias)

    def smoke(url, *args):
        if url == "live":
            raise RuntimeError("Broken live release")

    monkeypatch.setattr(release, "smoke", smoke)
    with pytest.raises(RuntimeError):
        release.promote(client, "service", "2", "region", "revision", "sha", 1)
    assert "live" not in client.aliases
    assert client.deleted_urls == ["live"]


def test_successful_release_records_previous_version(monkeypatch):
    client = FakeLambda()
    checked = []
    monkeypatch.setattr(release, "url_for", lambda client, function, alias: alias)
    monkeypatch.setattr(release, "smoke", lambda url, *args: checked.append(url))
    result = release.promote(client, "service", "2", "region", "revision", "sha", 1)
    assert checked == ["candidate", "live"]
    assert client.aliases["live"]["FunctionVersion"] == "2"
    assert result["previous_version"] == "1"


def test_url_configuration_rejects_an_existing_public_endpoint():
    class PublicUrl:
        def get_function_url_config(self, **kwargs):
            return {"AuthType": "NONE", "FunctionUrl": "https://example.com"}

    with pytest.raises(RuntimeError, match="IAM"):
        release.url_for(PublicUrl(), "service", "live")


def test_dataset_checksum_detects_same_size_tampering(tmp_path):
    path = tmp_path / "data.csv"
    path.write_bytes(b"original")
    lock = {"sha256": digest(path), "bytes": path.stat().st_size}
    validate(path, lock)
    path.write_bytes(b"modified")
    with pytest.raises(ValueError, match="Dataset bytes"):
        validate(path, lock)


@pytest.mark.parametrize("url", ["http://example.com", "https://attacker.example.com", "https://x.lambda-url.us-east-2.on.aws@attacker.example.com"])
def test_signed_client_does_not_send_credentials_to_arbitrary_hosts(url):
    with pytest.raises(ValueError):
        call(url, "health", region="us-east-2")


def test_lambda_adapter_accepts_iam_event_but_rejects_missing_identity(monkeypatch):
    import api
    from mangum import Mangum
    import cloud_runtime

    monkeypatch.setenv("AUTH_MODE", "aws_iam")
    monkeypatch.delenv("APP_SECRET_ARN", raising=False)
    cloud_runtime.load_cloud_secrets.cache_clear()
    monkeypatch.setattr(api, "ensure_database", lambda: None)
    monkeypatch.setattr(api, "describe_dataset", lambda: {"coverage": {"raw_rows": 1}})
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        handler = Mangum(api.app, lifespan="on")
        response = handler(event("/dataset"), {})
        assert response["statusCode"] == 200
        assert json.loads(response["body"])["coverage"]["raw_rows"] == 1
        assert handler(event("/dataset", authenticated=False), {})["statusCode"] == 401
    finally:
        loop.close()
        asyncio.set_event_loop(None)
        cloud_runtime.load_cloud_secrets.cache_clear()
