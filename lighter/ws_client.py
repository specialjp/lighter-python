import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

from websockets.sync.client import connect
from websockets.client import connect as connect_async

from lighter.configuration import Configuration


Callback = Callable[[str, Any], None]


@dataclass(frozen=True)
class Subscription:
    """Lightweight container describing a websocket subscription."""

    channel: str
    auth: Optional[str] = None
    callback: Optional[Callback] = None

    @property
    def channel_key(self) -> str:
        """Return subscription identifier in the colon format used in responses."""
        return self.channel.replace("/", ":")

class WsClient:
    def __init__(
        self,
        host=None,
        path="/stream",
        order_book_ids=None,
        account_ids=None,
        on_order_book_update=print,
        on_account_update=print,
        on_account_orders=None,
        subscriptions=None,
        callbacks=None,
        default_handler=None,
        raise_on_unhandled=True,
        account_orders=None,
        on_account_market=None,
        account_market=None,
    ):
        if host is None:
            host = Configuration.get_default().host.replace("https://", "")

        self.base_url = f"wss://{host}{path}"

        self.order_book_states: Dict[str, Dict[str, Any]] = {}
        self.account_states: Dict[str, Dict[str, Any]] = {}
        self.account_orders_states: Dict[str, Dict[str, Any]] = {}
        self.account_market_states: Dict[str, Dict[str, Any]] = {}
        self.ws = None

        self.on_order_book_update = on_order_book_update
        self.on_account_update = on_account_update
        self.on_account_orders = on_account_orders
        self.on_account_market = on_account_market
        self.default_handler = default_handler
        self.raise_on_unhandled = raise_on_unhandled

        self.channel_callbacks: Dict[str, Callback] = {}
        self.message_type_callbacks: Dict[str, Callback] = {}
        if callbacks:
            if not hasattr(callbacks, "items"):
                raise TypeError("callbacks must be a mapping of keys to callables.")
            for key, callback in callbacks.items():
                if key.startswith(("update/", "subscribed/")):
                    self.message_type_callbacks[key] = callback
                else:
                    self.channel_callbacks[key] = callback

        if on_order_book_update:
            self.channel_callbacks.setdefault("order_book", self._order_book_callback)
        if on_account_update:
            self.channel_callbacks.setdefault("account_all", self._account_all_callback)
        self.channel_callbacks.setdefault("account_orders", self._account_orders_callback)
        self.channel_callbacks.setdefault("account_market", self._account_market_callback)

        self.subscription_callbacks: Dict[str, Callback] = {}
        self.subscriptions: List[Subscription] = []

        raw_subscriptions = subscriptions
        if raw_subscriptions is None:
            normalized_subscriptions: Iterable[Any] = []
        elif isinstance(raw_subscriptions, dict):
            normalized_subscriptions = raw_subscriptions.values()
        elif isinstance(raw_subscriptions, (list, tuple, set, frozenset)):
            normalized_subscriptions = raw_subscriptions
        else:
            normalized_subscriptions = (raw_subscriptions,)

        for sub in normalized_subscriptions:
            self.add_subscription(sub)

        for market_id in order_book_ids or []:
            self.add_subscription(f"order_book/{market_id}")

        for account_id in account_ids or []:
            self.add_subscription(f"account_all/{account_id}")

        account_order_specs = account_orders
        if account_order_specs is None:
            account_order_iter: Iterable[Any] = []
        elif isinstance(account_order_specs, dict):
            account_order_iter = account_order_specs.values()
        elif isinstance(account_order_specs, (list, tuple, set, frozenset)):
            account_order_iter = account_order_specs
        else:
            account_order_iter = (account_order_specs,)

        for account_order in account_order_iter:
            subscription = self._build_account_orders_subscription(account_order)
            self.add_subscription(subscription)

        account_market_specs = account_market
        if account_market_specs is None:
            account_market_iter: Iterable[Any] = []
        elif isinstance(account_market_specs, dict):
            account_market_iter = account_market_specs.values()
        elif isinstance(account_market_specs, (list, tuple, set, frozenset)):
            account_market_iter = account_market_specs
        else:
            account_market_iter = (account_market_specs,)

        for account_market_spec in account_market_iter:
            subscription = self._build_account_market_subscription(account_market_spec)
            self.add_subscription(subscription)

        if len(self.subscriptions) == 0:
            raise Exception("No subscriptions provided.")

    def add_subscription(self, subscription: Union[Subscription, str, Dict[str, Any], Iterable[Any]]) -> Subscription:
        normalized = self._normalize_subscription(subscription)
        if not any(
            existing.channel == normalized.channel and existing.auth == normalized.auth
            for existing in self.subscriptions
        ):
            self.subscriptions.append(normalized)

        if normalized.callback:
            self.subscription_callbacks[normalized.channel] = normalized.callback
            colon_key = normalized.channel.replace("/", ":", 1)
            self.subscription_callbacks.setdefault(colon_key, normalized.callback)

        return normalized

    def _normalize_subscription(
        self, subscription: Union[Subscription, str, Dict[str, Any], Iterable[Any]]
    ) -> Subscription:
        if isinstance(subscription, Subscription):
            return subscription

        if isinstance(subscription, str):
            return Subscription(channel=subscription)

        if isinstance(subscription, dict):
            channel = subscription.get("channel")
            if not channel:
                raise ValueError("Subscription dictionary must include a 'channel' key.")
            return Subscription(
                channel=channel,
                auth=subscription.get("auth"),
                callback=subscription.get("callback"),
            )

        if isinstance(subscription, Iterable):
            items = list(subscription)
            if not items:
                raise ValueError("Subscription iterable cannot be empty.")

            channel = items[0]
            if not isinstance(channel, str):
                raise TypeError("First element of subscription iterable must be a channel string.")

            auth: Optional[str] = None
            callback: Optional[Callback] = None

            for item in items[1:]:
                if isinstance(item, str) and auth is None:
                    auth = item
                elif callable(item) and callback is None:
                    callback = item  # type: ignore[assignment]
                elif item is None:
                    continue
                else:
                    raise TypeError(f"Unsupported subscription element: {item!r}")

            return Subscription(channel=channel, auth=auth, callback=callback)

        raise TypeError(f"Unsupported subscription type: {type(subscription)!r}")

    def _build_account_orders_subscription(self, spec: Any) -> Subscription:
        if isinstance(spec, Subscription):
            return spec

        market_id: Optional[Union[str, int]] = None
        account_id: Optional[Union[str, int]] = None
        auth: Optional[str] = None
        callback: Optional[Callback] = None

        if isinstance(spec, dict):
            market_id = spec.get("market_id")
            account_id = spec.get("account_id")
            auth = spec.get("auth")
            callback = spec.get("callback")
        elif isinstance(spec, (list, tuple)):
            if len(spec) < 2 or len(spec) > 4:
                raise ValueError(
                    "account_orders entries must be (market_id, account_id[, auth][, callback]) or a dict."
                )
            market_id, account_id = spec[0], spec[1]
            remaining = spec[2:]
            for item in remaining:
                if isinstance(item, str) and auth is None:
                    auth = item
                elif callable(item) and callback is None:
                    callback = item  # type: ignore[assignment]
                elif item is None:
                    continue
                else:
                    raise TypeError(f"Unsupported account_orders element: {item!r}")
        else:
            raise TypeError("account_orders entries must be dicts, tuples, lists, or Subscription instances.")

        if market_id is None or account_id is None:
            raise ValueError("account_orders entries require both market_id and account_id.")

        channel = f"account_orders/{market_id}/{account_id}"
        return Subscription(channel=channel, auth=auth, callback=callback)

    def _build_account_market_subscription(self, spec: Any) -> Subscription:
        if isinstance(spec, Subscription):
            return spec

        market_id: Optional[Union[str, int]] = None
        account_id: Optional[Union[str, int]] = None
        auth: Optional[str] = None
        callback: Optional[Callback] = None

        if isinstance(spec, dict):
            market_id = spec.get("market_id")
            account_id = spec.get("account_id")
            auth = spec.get("auth")
            callback = spec.get("callback")
        elif isinstance(spec, (list, tuple)):
            if len(spec) < 2 or len(spec) > 4:
                raise ValueError(
                    "account_market entries must be (market_id, account_id[, auth][, callback]) or a dict."
                )
            market_id, account_id = spec[0], spec[1]
            remaining = spec[2:]
            for item in remaining:
                if isinstance(item, str) and auth is None:
                    auth = item
                elif callable(item) and callback is None:
                    callback = item  # type: ignore[assignment]
                elif item is None:
                    continue
                else:
                    raise TypeError(f"Unsupported account_market element: {item!r}")
        else:
            raise TypeError("account_market entries must be dicts, tuples, lists, or Subscription instances.")

        if market_id is None or account_id is None:
            raise ValueError("account_market entries require both market_id and account_id.")

        channel = f"account_market/{market_id}/{account_id}"
        return Subscription(channel=channel, auth=auth, callback=callback)

    def _subscription_payload(self, subscription: Subscription) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"type": "subscribe", "channel": subscription.channel}
        if subscription.auth is not None:
            payload["auth"] = subscription.auth
        return payload

    def _iter_subscription_payloads(self) -> Iterable[Dict[str, Any]]:
        for subscription in self.subscriptions:
            yield self._subscription_payload(subscription)

    def _order_book_callback(self, channel: str, order_book: Dict[str, Any]) -> None:
        if not self.on_order_book_update:
            return
        _, _, identifier = channel.partition("/")
        market_id = identifier or channel
        self.on_order_book_update(market_id, order_book)

    def _account_all_callback(self, channel: str, account_payload: Dict[str, Any]) -> None:
        if not self.on_account_update:
            return
        _, _, identifier = channel.partition("/")
        account_id = identifier or channel
        self.on_account_update(account_id, account_payload)

    def _account_orders_callback(self, channel: str, payload: Dict[str, Any]) -> None:
        normalized = self._normalize_channel(channel)
        parts = normalized.split("/")
        market_segment = parts[1] if len(parts) > 1 else normalized
        market_id = str(market_segment) if market_segment is not None else normalized
        account_value: Optional[Any] = None
        if isinstance(payload, dict):
            account_value = payload.get("account")
        account_id = parts[2] if len(parts) > 2 else None
        if account_id is not None:
            account_id = str(account_id)
        if account_value is not None:
            account_id = str(account_value)

        key = f"{market_id}:{account_id}" if account_id is not None else market_id
        self.account_orders_states[key] = payload

        if self.on_account_orders:
            self.on_account_orders(market_id, account_id, payload)

    def _account_market_callback(self, channel: str, payload: Dict[str, Any]) -> None:
        normalized = self._normalize_channel(channel)
        _, _, identifier = normalized.partition("/")
        ids = identifier.split("/", 1) if identifier else []
        market_segment = ids[0] if ids else identifier or normalized
        market_id = str(market_segment)
        account_id = ids[1] if len(ids) > 1 else None
        if account_id is not None:
            account_id = str(account_id)

        if isinstance(payload, dict):
            account_val = payload.get("account")
            if account_val is not None:
                account_id = str(account_val)

        key = f"{market_id}:{account_id}" if account_id is not None else market_id
        self.account_market_states[key] = payload

        if self.on_account_market:
            self.on_account_market(market_id, account_id, payload)

    def _invoke_channel_callback(self, message: Dict[str, Any], payload: Any = None) -> bool:
        channel = message.get("channel")
        if not channel:
            return False

        channel_key = self._normalize_channel(channel)
        callback = self.subscription_callbacks.get(channel)
        if callback is None:
            callback = self.subscription_callbacks.get(channel_key)
        if callback is None:
            account_value = message.get("account")
            if account_value is not None:
                account_candidate = f"{channel_key}/{account_value}"
                callback = self.subscription_callbacks.get(account_candidate)

        if callback is None:
            prefix = channel.split(":", 1)[0]
            callback = self.channel_callbacks.get(prefix)

        if callback is None:
            message_type = message.get("type")
            if message_type:
                callback = self.message_type_callbacks.get(message_type)

        if callback:
            callback(channel_key, payload if payload is not None else message)
            return True
        return False

    @staticmethod
    def _extract_identifier(channel: str) -> str:
        if ":" in channel:
            return channel.split(":", 1)[1]
        return channel

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        if ":" in channel:
            prefix, _, rest = channel.partition(":")
            return f"{prefix}/{rest}"
        return channel

    def on_message(self, ws, message):
        if isinstance(message, str):
            message = json.loads(message)

        message_type = message.get("type")

        if message_type == "connected":
            self.handle_connected(ws)
        elif message_type == "ping":
            ws.send(json.dumps({"type": "pong"}))
        elif message_type == "subscribed/order_book":
            self.handle_subscribed_order_book(message)
        elif message_type == "update/order_book":
            self.handle_update_order_book(message)
        elif message_type == "subscribed/account_all":
            self.handle_subscribed_account(message)
        elif message_type == "update/account_all":
            self.handle_update_account(message)
        else:
            handled = False
            if message_type and (
                message_type.startswith("subscribed/") or message_type.startswith("update/")
            ):
                handled = self._invoke_channel_callback(message)
            if not handled:
                self.handle_unhandled_message(message)

    async def on_message_async(self, ws, message):
        if isinstance(message, str):
            message = json.loads(message)

        message_type = message.get("type")

        if message_type == "connected":
            await self.handle_connected_async(ws)
        elif message_type == "ping":
            await ws.send(json.dumps({"type": "pong"}))
        elif message_type == "subscribed/order_book":
            self.handle_subscribed_order_book(message)
        elif message_type == "update/order_book":
            self.handle_update_order_book(message)
        elif message_type == "subscribed/account_all":
            self.handle_subscribed_account(message)
        elif message_type == "update/account_all":
            self.handle_update_account(message)
        else:
            handled = False
            if message_type and (
                message_type.startswith("subscribed/") or message_type.startswith("update/")
            ):
                handled = self._invoke_channel_callback(message)
            if not handled:
                self.handle_unhandled_message(message)

    def handle_connected(self, ws):
        for payload in self._iter_subscription_payloads():
            ws.send(json.dumps(payload))

    async def handle_connected_async(self, ws):
        for payload in self._iter_subscription_payloads():
            await ws.send(json.dumps(payload))

    def handle_subscribed_order_book(self, message):
        channel = message.get("channel", "")
        market_id = self._extract_identifier(channel)
        order_book = message.get("order_book", {}) or {}
        order_book_state = dict(order_book)
        order_book_state["asks"] = list(order_book.get("asks", []))
        order_book_state["bids"] = list(order_book.get("bids", []))
        self.order_book_states[market_id] = order_book_state
        self._invoke_channel_callback(message, self.order_book_states[market_id])

    def handle_update_order_book(self, message):
        channel = message.get("channel", "")
        market_id = self._extract_identifier(channel)
        self.update_order_book_state(market_id, message.get("order_book", {}))
        self._invoke_channel_callback(message, self.order_book_states[market_id])

    def update_order_book_state(self, market_id, order_book):
        state = self.order_book_states.setdefault(
            market_id, {"asks": [], "bids": [], "offset": None, "code": None}
        )

        if "offset" in order_book:
            state["offset"] = order_book["offset"]
        if "code" in order_book:
            state["code"] = order_book["code"]

        self.update_orders(order_book.get("asks", []), state["asks"], reverse=False)
        self.update_orders(order_book.get("bids", []), state["bids"], reverse=True)

    def update_orders(self, new_orders, existing_orders, reverse=False):
        price_map: Dict[str, Dict[str, Any]] = {order["price"]: dict(order) for order in existing_orders}

        for new_order in new_orders:
            price = new_order["price"]
            size = float(new_order.get("size", 0))
            if size == 0:
                price_map.pop(price, None)
            else:
                price_map[price] = dict(new_order)

        sorted_orders = sorted(
            price_map.values(),
            key=lambda order: float(order["price"]),
            reverse=reverse,
        )
        existing_orders[:] = sorted_orders

    def handle_subscribed_account(self, message):
        channel = message.get("channel", "")
        account_id = self._extract_identifier(channel)
        self.account_states[account_id] = message
        self._invoke_channel_callback(message, self.account_states[account_id])

    def handle_update_account(self, message):
        channel = message.get("channel", "")
        account_id = self._extract_identifier(channel)
        self.account_states[account_id] = message
        self._invoke_channel_callback(message, self.account_states[account_id])

    def handle_unhandled_message(self, message):
        if self.default_handler:
            self.default_handler(message)
            return
        if self.raise_on_unhandled:
            raise Exception(f"Unhandled message: {message}")

    def on_error(self, ws, error):
        raise Exception(f"Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        raise Exception(f"Closed: {close_status_code} {close_msg}")

    def _ensure_connection(self) -> None:
        if self.ws is None:
            raise RuntimeError("Websocket connection has not been established yet.")

    def send_payload(self, payload: Dict[str, Any]) -> None:
        """Send a JSON payload over an active synchronous websocket connection."""
        self._ensure_connection()
        message = json.dumps(payload)
        result = self.ws.send(message)  # type: ignore[call-arg]
        if inspect.isawaitable(result):
            raise RuntimeError(
                "Active websocket connection is asynchronous; use send_payload_async instead."
            )

    async def send_payload_async(self, payload: Dict[str, Any]) -> None:
        """Send a JSON payload over an active asynchronous websocket connection."""
        self._ensure_connection()
        message = json.dumps(payload)
        result = self.ws.send(message)  # type: ignore[call-arg]
        if inspect.isawaitable(result):
            await result  # type: ignore[func-returns-value]
        else:
            raise RuntimeError(
                "Active websocket connection is synchronous; use send_payload instead."
            )

    def send_tx(self, tx_type: int, tx_info: Any) -> None:
        """Send a single transaction using the websocket JSONAPI."""
        payload = {"type": "jsonapi/sendtx", "data": {"tx_type": tx_type, "tx_info": tx_info}}
        self.send_payload(payload)

    async def send_tx_async(self, tx_type: int, tx_info: Any) -> None:
        """Asynchronously send a single transaction using the websocket JSONAPI."""
        payload = {"type": "jsonapi/sendtx", "data": {"tx_type": tx_type, "tx_info": tx_info}}
        await self.send_payload_async(payload)

    def send_tx_batch(self, tx_types: Iterable[int], tx_infos: Iterable[Any]) -> None:
        """Send multiple transactions in a single websocket payload."""
        payload = {
            "type": "jsonapi/sendtxbatch",
            "data": {"tx_types": list(tx_types), "tx_infos": list(tx_infos)},
        }
        self.send_payload(payload)

    async def send_tx_batch_async(self, tx_types: Iterable[int], tx_infos: Iterable[Any]) -> None:
        """Asynchronously send multiple transactions in a single websocket payload."""
        payload = {
            "type": "jsonapi/sendtxbatch",
            "data": {"tx_types": list(tx_types), "tx_infos": list(tx_infos)},
        }
        await self.send_payload_async(payload)

    def run(self):
        ws = connect(self.base_url)
        self.ws = ws

        for message in ws:
            self.on_message(ws, message)

    async def run_async(self):
        ws = await connect_async(self.base_url)
        self.ws = ws

        async for message in ws:
            await self.on_message_async(ws, message)
