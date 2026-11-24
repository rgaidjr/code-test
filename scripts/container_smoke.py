"""Exercise the built Lambda image via its local runtime-interface emulator."""

import argparse
import json
import time

import httpx2


def event(path, method="GET", payload=None, authenticated=True):
    return {"version": "2.0", "routeKey": "$default", "rawPath": path,
            "rawQueryString": "", "headers": {"host": "localhost", "content-type": "application/json"},
            "requestContext": {"accountId": "123456789012", "apiId": "local", "domainName": "localhost",
                "domainPrefix": "local", "requestId": "ci-smoke", "routeKey": "$default", "stage": "$default",
                "time": "01/Jan/2026:00:00:00 +0000", "timeEpoch": 0,
                "http": {"method": method, "path": path, "protocol": "HTTP/1.1", "sourceIp": "127.0.0.1", "userAgent": "smoke"},
                "authorizer": {"iam": {"userArn": "arn:aws:iam::123456789012:role/ci-smoke"}} if authenticated else {}},
            "body": json.dumps(payload) if payload is not None else "", "isBase64Encoded": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:9000/2015-03-31/functions/function/invocations")
    parser.add_argument("--records", type=int, default=1)
    args = parser.parse_args()

    def invoke(request):
        response = httpx2.post(args.endpoint, json=request, timeout=180)
        response.raise_for_status()
        return response.json()

    for attempt in range(30):
        try:
            assert invoke(event("/health"))["statusCode"] == 200
            break
        except httpx2.ConnectError:
            if attempt == 29:
                raise
            time.sleep(2)
    assert invoke(event("/dataset", authenticated=False))["statusCode"] == 401
    result = invoke(event("/dataset"))
    assert result["statusCode"] == 200, result
    assert json.loads(result["body"])["coverage"]["raw_rows"] == args.records
    result = invoke(event("/query", "POST", {"sql": "SELECT COUNT(*) AS raw_rows FROM records"}))
    assert result["statusCode"] == 200, result
    assert json.loads(result["body"])["rows"] == [[args.records]]
    assert invoke(event("/query", "POST", {"sql": "DELETE FROM records"}))["statusCode"] == 400
    print("Container health, authentication, dataset, query, and read-only checks passed")


if __name__ == "__main__":
    main()
