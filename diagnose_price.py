"""Read-only price diagnostics for NVDL. Does not access an account or place orders."""

import getpass
import json
import os
from urllib import error, parse, request

BASE = "https://openapi.tossinvest.com"


def fetch(path, token, params=None):
    url = BASE + path
    if params:
        url += "?" + parse.urlencode(params)
    req = request.Request(url, headers={"Authorization": "Bearer " + token})
    with request.urlopen(req, timeout=15) as response:
        return json.load(response)["result"]


def main():
    client_id = os.environ.get("TOSS_CLIENT_ID") or input("Toss client ID: ").strip()
    client_secret = os.environ.get("TOSS_CLIENT_SECRET") or getpass.getpass("Toss client secret: ")
    if not client_id or not client_secret:
        raise SystemExit("Client ID and secret are required; no orders were submitted")
    body = parse.urlencode({"grant_type": "client_credentials",
                            "client_id": client_id,
                            "client_secret": client_secret}).encode("ascii")
    req = request.Request(BASE + "/oauth2/token", data=body,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with request.urlopen(req, timeout=15) as response:
            token = json.load(response)["access_token"]
    except error.HTTPError as exc:
        raise SystemExit(f"API HTTP {exc.code}; no orders were submitted") from exc
    queries = {
        "prices": ("/api/v1/prices", {"symbols": "NVDL"}),
        "trades": ("/api/v1/trades", {"symbol": "NVDL", "count": 1}),
        "orderbook": ("/api/v1/orderbook", {"symbol": "NVDL"}),
        "usd_krw_rate": ("/api/v1/exchange-rate",
                         {"baseCurrency": "USD", "quoteCurrency": "KRW"}),
    }
    results = {}
    for name, (path, params) in queries.items():
        try:
            results[name] = fetch(path, token, params)
        except error.HTTPError as exc:
            results[name] = {"http_error": exc.code}
    # Public market data only. Never print the access token or credentials.
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
