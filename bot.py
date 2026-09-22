"""NVDL band strategy using the official Toss Securities Open API.

Python 3.10+, standard library only. Live orders are disabled by default.
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from urllib import error, parse, request

BASE = "https://openapi.tossinvest.com"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"


def dec(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"Invalid decimal: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("Non-finite decimal")
    return result


def load_config():
    if not CONFIG_PATH.exists():
        raise RuntimeError("Copy config.example.json to config.json and set quantity.")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if config.get("symbol") != "NVDL":
        raise ValueError("This bot is limited to NVDL.")
    bands = [dec(config[key]) for key in
             ("buy_min_krw", "buy_max_krw", "sell_min_krw")]
    if not (0 < bands[0] <= bands[1] < bands[2]):
        raise ValueError("Bands must be positive, ordered, and nonoverlapping.")
    buffer = dec(config.get("cash_buffer_percent", 1))
    if not 0 <= buffer <= 10:
        raise ValueError("cash_buffer_percent must be between 0 and 10.")
    interval = int(config.get("poll_seconds", 15))
    if interval < 5:
        raise ValueError("poll_seconds must be at least 5.")
    if not isinstance(config.get("live_trading"), bool):
        raise ValueError("live_trading must be true or false.")
    config["cash_buffer_percent"] = buffer
    config["poll_seconds"] = interval
    return config


def decision(price_krw, held, config):
    price = dec(price_krw).to_integral_value(rounding=ROUND_FLOOR)
    if held:
        if dec(config["sell_min_krw"]) <= price:
            return "SELL"
        return "WAIT"
    if dec(config["buy_min_krw"]) <= price <= dec(config["buy_max_krw"]):
        return "BUY"
    return "WAIT"


def log(message):
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}", flush=True)


def save_state(state):
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, STATE_PATH)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"pending_order_id": None, "pending_side": None,
            "last_client_order_id": None, "last_fill_at": 0}


class TossAPI:
    def __init__(self, client_id, client_secret, account_seq):
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_seq = account_seq
        self.token = None
        self.expiry = 0

    def resolve_account(self):
        if self.account_seq:
            return
        accounts = self.call("GET", "/api/v1/accounts")["result"]
        brokerage = [account for account in accounts if account.get("accountType") == "BROKERAGE"]
        if len(brokerage) != 1:
            raise RuntimeError("Set TOSS_ACCOUNT_SEQ: expected exactly one brokerage account")
        self.account_seq = brokerage[0]["accountSeq"]

    def call(self, method, path, params=None, payload=None, authenticated=True):
        url = BASE + path
        if params:
            url += "?" + parse.urlencode(params)
        headers = {"Accept": "application/json"}
        if authenticated:
            self.ensure_token()
            headers["Authorization"] = "Bearer " + self.token
            if path.startswith(("/api/v1/holdings", "/api/v1/orders",
                                "/api/v1/buying-power", "/api/v1/sellable-quantity")):
                headers["X-Tossinvest-Account"] = str(self.account_seq)
        body = None
        if payload is not None:
            if path == "/oauth2/token":
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                body = parse.urlencode(payload).encode("ascii")
            else:
                headers["Content-Type"] = "application/json"
                body = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=15) as response:
                data = json.load(response)
        except error.HTTPError as exc:
            details = exc.read(1000).decode("utf-8", errors="replace")
            raise RuntimeError(f"API {exc.code} {path}: {details}") from exc
        if authenticated and "result" not in data:
            raise RuntimeError(f"Unexpected API response from {path}")
        return data

    def ensure_token(self):
        if self.token and time.time() < self.expiry - 60:
            return
        data = self.call("POST", "/oauth2/token", payload={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }, authenticated=False)
        self.token = data["access_token"]
        self.expiry = time.time() + int(data["expires_in"])

    def quote_krw(self):
        price = self.call("GET", "/api/v1/prices", {"symbols": "NVDL"})["result"]
        if len(price) != 1 or price[0].get("symbol", "").upper() != "NVDL" or price[0].get("currency") != "USD":
            raise RuntimeError("Unexpected NVDL price response")
        timestamp = price[0].get("timestamp")
        if not timestamp:
            raise RuntimeError("Price has no timestamp; market may be closed")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds()
        if age < -30 or age > 120:
            raise RuntimeError(f"Stale price ({age:.0f} seconds old); no trade")
        fx = self.call("GET", "/api/v1/exchange-rate", {
            "baseCurrency": "USD", "quoteCurrency": "KRW"
        })["result"]
        valid_until = datetime.fromisoformat(fx["validUntil"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > valid_until:
            raise RuntimeError("Expired FX quote; no trade")
        usd = dec(price[0]["lastPrice"])
        rate = dec(fx["rate"])
        if usd <= 0 or rate <= 0:
            raise RuntimeError("Invalid price or FX rate")
        return usd, rate, usd * rate

    def holding_quantity(self):
        result = self.call("GET", "/api/v1/holdings", {"symbol": "NVDL"})["result"]
        return sum((dec(item["quantity"]) for item in result.get("items", [])
                    if item.get("symbol", "").upper() == "NVDL"), Decimal(0))

    def open_orders(self):
        result = self.call("GET", "/api/v1/orders", {"status": "OPEN", "symbol": "NVDL"})["result"]
        return result.get("orders", [])

    def buying_power_usd(self):
        result = self.call("GET", "/api/v1/buying-power", {"currency": "USD"})["result"]
        if result.get("currency") != "USD":
            raise RuntimeError("Unexpected buying-power currency")
        return dec(result["cashBuyingPower"])

    def sellable_quantity(self):
        result = self.call("GET", "/api/v1/sellable-quantity", {"symbol": "NVDL"})["result"]
        return dec(result["sellableQuantity"])

    def regular_market_open(self):
        result = self.call("GET", "/api/v1/market-calendar/US")["result"]
        now = datetime.now(timezone.utc)
        for day in (result.get("today"), result.get("previousBusinessDay")):
            session = day and day.get("regularMarket")
            if session:
                start = datetime.fromisoformat(session["startTime"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(session["endTime"].replace("Z", "+00:00"))
                # USD amount buys and fractional sells are only allowed until 1h before close.
                if start <= now < end and (end - now).total_seconds() > 3600:
                    return True
        return False

    def order(self, side, amount_or_quantity, client_order_id):
        # Do not retry an uncertain POST. The caller records the client ID first.
        payload = {
            "clientOrderId": client_order_id,
            "symbol": "NVDL",
            "side": side,
            "orderType": "MARKET",
        }
        payload["orderAmount" if side == "BUY" else "quantity"] = str(amount_or_quantity)
        return self.call("POST", "/api/v1/orders", payload=payload)["result"]

    def order_detail(self, order_id):
        return self.call("GET", "/api/v1/orders/" + parse.quote(order_id, safe=""))["result"]


def cycle(api, config, state):
    if state.get("uncertain_order"):
        raise RuntimeError("Previous order outcome is uncertain. Check Toss order history and state.json manually.")
    pending = state.get("pending_order_id")
    if pending:
        order = api.order_detail(pending)
        status = order["status"]
        log(f"Pending order {pending}: {status}")
        if status in {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE"}:
            return
        if status not in {"FILLED", "CANCELED", "REJECTED", "REPLACED", "CANCEL_REJECTED", "REPLACE_REJECTED"}:
            raise RuntimeError(f"Unknown order status {status}; no new order")
        if state.get("pending_side") not in {"BUY", "SELL"}:
            raise RuntimeError("Missing pending order side; check order manually")
        state["pending_order_id"] = None
        state["pending_side"] = None
        state["last_fill_at"] = time.time()
        save_state(state)
        # Wait until the next cycle so holdings and market data can settle.
        return
    if api.open_orders():
        log("Another NVDL order is open; waiting")
        return
    held = api.holding_quantity()
    if time.time() - state.get("last_fill_at", 0) < 120:
        log("Waiting for holdings to settle after a fill")
        return
    usd, rate, krw = api.quote_krw()
    action = decision(krw, held > 0, config)
    log(f"NVDL ${usd} × {rate} = ₩{krw:.0f}; held={held}; action={action}")
    if action == "WAIT":
        return
    if not api.regular_market_open():
        log("US regular session is closed or within its final hour; waiting")
        return
    if action == "BUY":
        buying_power = api.buying_power_usd()
        size = (buying_power * (Decimal(100) - config["cash_buffer_percent"]) / 100).quantize(
            Decimal("0.01"), rounding=ROUND_FLOOR)
        if size < Decimal("1"):
            log(f"USD buying power ${buying_power} is too small; waiting")
            return
    else:
        size = min(held, api.sellable_quantity()).quantize(Decimal("0.000001"), rounding=ROUND_FLOOR)
        if size <= 0:
            log("No NVDL quantity is sellable; waiting")
            return
    if not config["live_trading"]:
        log(f"DRY RUN: would {action} {'$' if action == 'BUY' else ''}{size} {'USD' if action == 'BUY' else 'shares'}")
        return
    client_order_id = "nvdl-" + uuid.uuid4().hex[:24]
    state["last_client_order_id"] = client_order_id
    state["uncertain_order"] = True
    save_state(state)
    response = api.order(action, size, client_order_id)
    state["pending_order_id"] = response["orderId"]
    state["pending_side"] = action
    state["uncertain_order"] = False
    save_state(state)
    log(f"Submitted {action} market order {response['orderId']}; fill is not yet confirmed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one API check")
    parser.add_argument("--simulate", nargs=2, metavar=("USD_PRICE", "KRW_PER_USD"),
                        help="Offline strategy check without credentials or orders")
    args = parser.parse_args()
    config = load_config()
    if args.simulate:
        krw = dec(args.simulate[0]) * dec(args.simulate[1])
        print(f"KRW estimate: {krw}; flat: {decision(krw, False, config)}; holding: {decision(krw, True, config)}")
        return
    client_id = os.environ.get("TOSS_CLIENT_ID")
    client_secret = os.environ.get("TOSS_CLIENT_SECRET")
    account_seq = os.environ.get("TOSS_ACCOUNT_SEQ")
    if not all((client_id, client_secret)):
        raise RuntimeError("Set TOSS_CLIENT_ID and TOSS_CLIENT_SECRET in your local environment.")
    api = TossAPI(client_id, client_secret, account_seq)
    api.resolve_account()
    state = load_state()
    while True:
        try:
            cycle(api, config, state)
        except (RuntimeError, ValueError, error.URLError) as exc:
            log(f"STOP: {exc}")
            sys.exit(1)
        if args.once:
            break
        time.sleep(config["poll_seconds"])


if __name__ == "__main__":
    main()
