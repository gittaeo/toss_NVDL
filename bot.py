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
    mode = config.get("price_mode", "KRW_ESTIMATE")
    if mode not in {"KRW_ESTIMATE", "USD"}:
        raise ValueError("price_mode must be KRW_ESTIMATE or USD")
    suffix = "usd" if mode == "USD" else "krw"
    bands = [dec(config[key]) for key in
             (f"buy_min_{suffix}", f"buy_max_{suffix}", f"sell_min_{suffix}")]
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
    if config["live_trading"] and mode == "KRW_ESTIMATE" and config.get("accept_estimated_krw_price") is not True:
        raise RuntimeError("Live KRW estimate requires accept_estimated_krw_price=true.")
    config["cash_buffer_percent"] = buffer
    config["poll_seconds"] = interval
    config["price_mode"] = mode
    return config


def decision(price, held, config):
    mode = config.get("price_mode", "KRW_ESTIMATE")
    price = dec(price)
    if mode == "KRW_ESTIMATE":
        price = price.to_integral_value(rounding=ROUND_FLOOR)
    suffix = "usd" if mode == "USD" else "krw"
    if held:
        if dec(config[f"sell_min_{suffix}"]) <= price:
            return "SELL"
        return "WAIT"
    if dec(config[f"buy_min_{suffix}"]) <= price <= dec(config[f"buy_max_{suffix}"]):
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

    def quote_usd(self, held=False):
        try:
            book = self.call("GET", "/api/v1/orderbook", {"symbol": "NVDL"})["result"]
        except (RuntimeError, error.URLError):
            book = {}
        book_age = self._age_seconds(book.get("timestamp"))
        if book.get("currency") == "USD" and book_age is not None and -30 <= book_age <= 120:
            asks, bids = book.get("asks") or [], book.get("bids") or []
            if asks and bids:
                ask, bid = dec(asks[0]["price"]), dec(bids[0]["price"])
                if 0 < bid <= ask and (ask - bid) / bid <= Decimal("0.03"):
                    return (bid if held else ask), ("best bid" if held else "best ask"), book_age
        prices = self.call("GET", "/api/v1/prices", {"symbols": "NVDL"})["result"]
        if len(prices) != 1 or prices[0].get("symbol", "").upper() != "NVDL" or prices[0].get("currency") != "USD":
            raise RuntimeError("Unexpected NVDL price response")
        last = dec(prices[0]["lastPrice"])
        last_age = self._age_seconds(prices[0].get("timestamp"))
        if last <= 0:
            raise RuntimeError("Invalid USD price")
        if last_age is None or not -30 <= last_age <= 120:
            age_label = "unknown" if last_age is None else f"{last_age:.0f}s"
            book_label = "unknown" if book_age is None else f"{book_age:.0f}s"
            raise RuntimeError(f"no fresh quote; last trade ${last} age={age_label}, orderbook age={book_label}; no trade")
        return last, "last trade", last_age

    @staticmethod
    def _age_seconds(timestamp):
        if not timestamp:
            return None
        return (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds()

    def quote_krw(self, held=False):
        usd, source, age = self.quote_usd(held)
        fx = self.call("GET", "/api/v1/exchange-rate", {
            "baseCurrency": "USD", "quoteCurrency": "KRW"
        })["result"]
        valid_until = datetime.fromisoformat(fx["validUntil"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > valid_until:
            raise RuntimeError("Expired FX quote; no trade")
        rate = dec(fx["rate"])
        if usd <= 0 or rate <= 0:
            raise RuntimeError("Invalid price or FX rate")
        return usd, rate, usd * rate, source, age

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
    market_open = api.regular_market_open()
    try:
        if config.get("price_mode", "KRW_ESTIMATE") == "USD":
            usd, source, age = api.quote_usd(held > 0)
            action = decision(usd, held > 0, config)
            log(f"NVDL API {source}=${usd} ({age:.0f}s old); held={held}; action={action}")
        else:
            usd, rate, krw, source, age = api.quote_krw(held > 0)
            action = decision(krw, held > 0, config)
            log(f"NVDL API {source} ${usd} ({age:.0f}s old) × {rate} = ₩{krw:.0f} estimate; held={held}; action={action}")
    except RuntimeError as exc:
        if market_open:
            raise
        log(f"US regular session closed; fresh quote unavailable ({exc}); waiting")
        return
    if not market_open:
        log("US regular session is closed or within its final hour; no order")
        return
    if action == "WAIT":
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
        value = dec(args.simulate[0]) if config["price_mode"] == "USD" else dec(args.simulate[0]) * dec(args.simulate[1])
        print(f"Signal price ({config['price_mode']}): {value}; flat: {decision(value, False, config)}; holding: {decision(value, True, config)}")
        return
    client_id = os.environ.get("TOSS_CLIENT_ID")
    client_secret = os.environ.get("TOSS_CLIENT_SECRET")
    account_seq = os.environ.get("TOSS_ACCOUNT_SEQ")
    if not all((client_id, client_secret)):
        raise RuntimeError("Set TOSS_CLIENT_ID and TOSS_CLIENT_SECRET in your local environment.")
    api = TossAPI(client_id, client_secret, account_seq)
    api.resolve_account()
    state = load_state()
    log(f"{'LIVE' if config['live_trading'] else 'DRY RUN'} mode; price source={config['price_mode']}; "
        f"buy={config['buy_min_krw']}–{config['buy_max_krw']} KRW estimate; "
        f"sell>={config['sell_min_krw']} KRW estimate" if config["price_mode"] == "KRW_ESTIMATE"
        else f"{'LIVE' if config['live_trading'] else 'DRY RUN'} mode; price source=USD; "
             f"buy=${config['buy_min_usd']}–${config['buy_max_usd']}; sell>=${config['sell_min_usd']}")
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
