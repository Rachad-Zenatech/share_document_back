"""
RabbitMQ Service & Topology Manager
Provides resilient connection management, topology declaration, publisher confirmations,
and dead-letter queue routing for asynchronous cross-service messaging.
"""

import os
import json
import socket
import time
import asyncio
import logging
from typing import Any, Callable, Dict, Optional, Set
from uuid import uuid4


try:
    import aio_pika
    from aio_pika.abc import (
        AbstractRobustConnection,
        AbstractRobustChannel,
        AbstractExchange,
        AbstractQueue,
        AbstractIncomingMessage,
    )
    AIO_PIKA_AVAILABLE = True
except ImportError:
    aio_pika = None
    AbstractRobustConnection = Any  # type: ignore[misc,assignment]
    AbstractRobustChannel = Any  # type: ignore[misc,assignment]
    AbstractExchange = Any  # type: ignore[misc,assignment]
    AbstractQueue = Any  # type: ignore[misc,assignment]
    AbstractIncomingMessage = Any  # type: ignore[misc,assignment]
    AIO_PIKA_AVAILABLE = False

logger = logging.getLogger(__name__)


from services.notifications.rabbitmq_config import (  # noqa: F401  (re-exported for existing importers)
    DEFAULT_SERVICE_KEY,
    DEFAULT_SERVICE_DISPLAY_NAME,
    _sanitize_amqp_url,
    _explicit_url_parts,
    get_rabbitmq_host,
    get_rabbitmq_port,
    get_rabbitmq_management_port,
    get_hub_status_cache_ttl,
    hub_peer_http_probe_enabled,
    get_rabbitmq_user,
    get_rabbitmq_password,
    get_rabbitmq_vhost,
    _vhost_url_part,
    get_rabbitmq_url,
    get_commands_exchange,
    get_events_exchange,
    get_dlx_exchange,
    get_presence_exchange,
    get_prefetch_count,
    COMMANDS_EXCHANGE_NAME,
    EVENTS_EXCHANGE_NAME,
    DLX_EXCHANGE_NAME,
    PRESENCE_EXCHANGE_NAME,
    PREFETCH_COUNT,
    RABBITMQ_HOST,
    RABBITMQ_PORT,
    RABBITMQ_USER,
    RABBITMQ_PASS,
    RABBITMQ_URL,
    _HUB_SERVICES,
    _TOPOLOGY_SERVICES,
    _build_hub_application,
    HUB_APPLICATIONS,
)
from services.notifications.rabbitmq_hub_status import HubStatusMixin
from services.notifications.rabbitmq_in_memory import InMemoryMessageBroker, in_memory_broker  # noqa: F401


class RabbitMQManager(HubStatusMixin):
    def __init__(
        self,
        amqp_url: Optional[str] = None,
        service_key: str = DEFAULT_SERVICE_KEY,
        display_name: str = DEFAULT_SERVICE_DISPLAY_NAME,
    ):
        self._custom_amqp_url = amqp_url
        self.service_key = service_key
        self.display_name = display_name
        self.instance_id = os.getenv(
            "SERVICE_INSTANCE_ID",
            f"{service_key}-{socket.gethostname()}-{os.getpid()}",
        )
        self._connection: Optional[AbstractRobustConnection] = None
        self._channel: Optional[AbstractRobustChannel] = None
        self._stream_channel: Optional[AbstractRobustChannel] = None
        self._commands_exchange: Optional[AbstractExchange] = None
        self._events_exchange: Optional[AbstractExchange] = None
        self._dlx_exchange: Optional[AbstractExchange] = None
        self._presence_exchange: Optional[AbstractExchange] = None
        self._is_connected: bool = False
        self._use_fallback: bool = False
        self._lock = asyncio.Lock()
        self._in_memory_tasks: Set[asyncio.Task] = set()
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._peer_monitor_task: Optional[asyncio.Task] = None
        self._presence_registry: Dict[str, Dict[str, Any]] = {}
        self._known_peer_online: Dict[str, bool] = {}
        self._sse_subscribers: Set[asyncio.Queue] = set()
        self._last_active_test: Dict[str, Any] = {}
        # Hub snapshot cache: collapses the monitor loop, status polls and SSE connects into one broker/peer fetch
        self._hub_status_cache: Optional[Dict[str, Any]] = None
        self._hub_status_cached_at: float = 0.0
        self._hub_status_lock = asyncio.Lock()
        self._last_reconnect_attempt: float = 0.0
        self._shutting_down: bool = False

    @property
    def amqp_url(self) -> str:
        return self._custom_amqp_url or get_rabbitmq_url()

    @amqp_url.setter
    def amqp_url(self, value: str):
        self._custom_amqp_url = value

    @property
    def is_connected(self) -> bool:
        return self._is_connected and self._connection is not None and not self._connection.is_closed

    async def connect(self, max_retries: int = 3, retry_delay: float = 1.0) -> bool:
        """
        Attempts to connect to RabbitMQ broker and initialize topology.
        If connection fails, switches to in-memory fallback so the service remains operational.
        """
        self._shutting_down = False
        self.install_uvicorn_shutdown_hook()
        async with self._lock:
            if self.is_connected:
                return True

            if not AIO_PIKA_AVAILABLE:
                logger.info("aio_pika library is not installed; operating in in-memory fallback messaging mode.")
                self._use_fallback = True
                self._is_connected = False
                return False

            base_url = self.amqp_url
            candidates = [base_url]

            host = get_rabbitmq_host()
            port = get_rabbitmq_port()
            user = get_rabbitmq_user()
            password = get_rabbitmq_password()

            vhost_part = _vhost_url_part(get_rabbitmq_vhost())

            for alt_host in [host, "rabbitmq", "127.0.0.1", "host.docker.internal"]:
                if alt_host:
                    alt_url = f"amqp://{user}:{password}@{alt_host}:{port}/{vhost_part}"
                    if alt_url not in candidates:
                        candidates.append(alt_url)

            unique_candidates = [c for c in candidates if c]

            for attempt in range(1, max_retries + 1):
                for target_url in unique_candidates:
                    masked_url = _sanitize_amqp_url(target_url)
                    try:
                        logger.info(f"Connecting to RabbitMQ at {masked_url} (attempt {attempt}/{max_retries})...")
                        self._connection = await aio_pika.connect_robust(
                            target_url,
                            timeout=3.0,
                            client_properties={
                                "connection_name": f"{self.service_key}:{self.instance_id}",
                                "service": self.service_key,
                                "application": self.display_name,
                            },
                        )
                        self.amqp_url = target_url
                        self._channel = await self._connection.channel(publisher_confirms=True)
                        await self._channel.set_qos(prefetch_count=get_prefetch_count())

                        await self._setup_topology()

                        self._stream_channel = await self._connection.channel(publisher_confirms=True)
                        await self._stream_channel.set_qos(prefetch_count=max(get_prefetch_count(), 50))
                        await self._setup_hub_streams()
                        await self._start_hub_consumers()

                        self._is_connected = True
                        self._use_fallback = False

                        if self._heartbeat_task is None or self._heartbeat_task.done():
                            self._heartbeat_task = asyncio.create_task(
                                self._presence_heartbeat_loop(),
                                name=f"rabbitmq-presence-heartbeat-{self.service_key}",
                            )
                        if self._peer_monitor_task is None or self._peer_monitor_task.done():
                            self._peer_monitor_task = asyncio.create_task(
                                self._peer_status_monitor_loop(),
                                name=f"rabbitmq-peer-monitor-{self.service_key}",
                            )

                        await self._publish_presence_event("online", reason="server_connected")
                        logger.info("RabbitMQ connection, queues and streams successfully initialized.")
                        return True
                    except Exception as exc:
                        logger.debug(f"RabbitMQ candidate {masked_url} attempt {attempt} failed: {exc}")

                if attempt < max_retries:
                    await asyncio.sleep(retry_delay * attempt)

            logger.warning("RabbitMQ is unreachable. Enabling in-memory fallback mode for cross-service messaging.")
            self._use_fallback = True
            self._is_connected = False
            return False

    async def _setup_topology(self):
        """Declares durable exchanges, queues, DLX, and bindings."""
        if not self._channel:
            return

        dlx_name = get_dlx_exchange()
        commands_exchange_name = get_commands_exchange()
        events_exchange_name = get_events_exchange()

        # 1. Dead Letter Exchange & Queues
        self._dlx_exchange = await self._channel.declare_exchange(
            dlx_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        for svc in _TOPOLOGY_SERVICES:
            dlq = await self._channel.declare_queue(f"{svc}.commands.dlq", durable=True)
            await dlq.bind(self._dlx_exchange, routing_key=f"{svc}.#")

        # 2. Main Commands Exchange
        self._commands_exchange = await self._channel.declare_exchange(
            commands_exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )

        # 3. Main Events Exchange
        self._events_exchange = await self._channel.declare_exchange(
            events_exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )

        # 4. Hub Presence Exchange
        self._presence_exchange = await self._channel.declare_exchange(
            get_presence_exchange(),
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )

        # 5. Service Command Queues (with DLX configured)
        for svc in _TOPOLOGY_SERVICES:
            cmd_queue = await self._channel.declare_queue(
                f"{svc}.commands",
                durable=True,
                arguments={
                    "x-dead-letter-exchange": dlx_name,
                    "x-dead-letter-routing-key": f"{svc}.dlq",
                },
            )
            await cmd_queue.bind(self._commands_exchange, routing_key=f"{svc}.#")

        # 5b. Legacy share_document queues (events published via publish_share_document_event)
        sd_dlq = await self._channel.declare_queue("share_document.commands.dlq", durable=True)
        for rk in ("share_document.#", "share-document.#"):
            await sd_dlq.bind(self._dlx_exchange, routing_key=rk)
        sd_cmd = await self._channel.declare_queue(
            "share_document.commands",
            durable=True,
            arguments={"x-dead-letter-exchange": dlx_name, "x-dead-letter-routing-key": "share_document.dlq"},
        )
        sd_evt = await self._channel.declare_queue("ceo.share_document.events", durable=True)
        for rk in ("share_document.#", "share-document.#"):
            await sd_cmd.bind(self._commands_exchange, routing_key=rk)
            await sd_evt.bind(self._events_exchange, routing_key=rk)

        # 6. CEO Result/Event Queues (the CEO's own "ceo.ceo.events" is declared but left unbound so it never fills up)
        for svc in _TOPOLOGY_SERVICES:
            evt_queue = await self._channel.declare_queue(f"ceo.{svc}.events", durable=True)
            if svc != "ceo":
                await evt_queue.bind(self._events_exchange, routing_key=f"{svc}.#")

    async def _setup_hub_streams(self):
        """Declares RabbitMQ Streams. Failures are tolerated: the hub then reports streams as not working."""
        if not self._stream_channel:
            return
        try:
            stream_names = {f"{svc}.events.stream" for svc in _TOPOLOGY_SERVICES}
            for name in sorted(stream_names):
                q = await self._stream_channel.declare_queue(name, durable=True, arguments={"x-queue-type": "stream"})
                svc = name[: -len(".events.stream")]
                await q.bind(get_events_exchange(), routing_key=f"{svc}.#")
            presence_stream = await self._stream_channel.declare_queue(
                "hub.presence.stream", durable=True, arguments={"x-queue-type": "stream"}
            )
            await presence_stream.bind(get_presence_exchange(), routing_key="presence.#")
        except Exception as exc:
            logger.warning(f"RabbitMQ Streams unavailable on this broker; continuing with queues only: {exc}")
            try:
                self._stream_channel = await self._connection.channel(publisher_confirms=True)
                await self._stream_channel.set_qos(prefetch_count=max(get_prefetch_count(), 50))
            except Exception:
                self._stream_channel = None

    async def _handle_incoming_presence_payload(self, data: Dict[str, Any]) -> None:
        svc = data.get("service")
        if not svc:
            return
        new_status = data.get("status", "online")
        prev_entry = self._presence_registry.get(svc)
        prev_status = prev_entry.get("status") if prev_entry else None

        self._presence_registry[svc] = {**data, "received_at_epoch": time.time()}
        self._known_peer_online[svc] = new_status == "online"

        # Push a fresh snapshot to SSE listeners as soon as another server announces a change
        if svc != self.service_key and (
            prev_status != new_status
            or data.get("eventType") in ("service.presence.offline", "service.presence.online")
        ):
            logger.info(f"[MessageHub] Status update from '{svc}': status={new_status} reason={data.get('reason', 'update')}")
            try:
                self._broadcast_to_sse(await self.get_hub_live_status(active_test=False, force=True))
            except Exception as exc:
                logger.debug(f"Failed broadcasting presence update to SSE: {exc}")

    async def _start_hub_consumers(self):
        """Consumers for this server's command queue, the presence stream, and an instant status-notify queue."""
        if not self._channel:
            return
        app_cfg = HUB_APPLICATIONS.get(self.service_key) or HUB_APPLICATIONS[DEFAULT_SERVICE_KEY]

        async def _presence_only(message: AbstractIncomingMessage):
            async with message.process(requeue=False):
                try:
                    data = json.loads(message.body.decode("utf-8"))
                    if str(data.get("eventType", "")).startswith("service.presence."):
                        await self._handle_incoming_presence_payload(data)
                except Exception:
                    pass

        # 1. Own command queue (presence payloads only; real commands are not sent here yet)
        try:
            cmd_queue = await self._channel.get_queue(app_cfg["primary_queue"])
            await cmd_queue.consume(_presence_only, consumer_tag=f"{self.service_key}-queue-consumer-{uuid4().hex[:8]}")
        except Exception as exc:
            logger.warning(f"Failed starting command queue consumer for {app_cfg['primary_queue']}: {exc}")

        # 2. Exclusive instant status queue (peer online/offline broadcasts)
        try:
            notify_q = await self._channel.declare_queue(
                f"hub.status.notify.{self.service_key}.{uuid4().hex[:8]}",
                durable=False, exclusive=True, auto_delete=True,
            )
            if self._presence_exchange:
                await notify_q.bind(self._presence_exchange, routing_key="presence.#")
            await notify_q.consume(_presence_only, consumer_tag=f"{self.service_key}-status-notify-{uuid4().hex[:8]}")
        except Exception as exc:
            logger.warning(f"Failed starting instant status notification consumer: {exc}")

        # 3. Own event stream + presence stream (consumers make the streams count as 'working')
        if self._stream_channel:
            for stream_name in (app_cfg["primary_stream"], "hub.presence.stream"):
                try:
                    stream_q = await self._stream_channel.declare_queue(
                        stream_name, durable=True, arguments={"x-queue-type": "stream"}
                    )
                    await stream_q.consume(
                        _presence_only,
                        consumer_tag=f"{self.service_key}-{stream_name}-{uuid4().hex[:8]}",
                        arguments={"x-stream-offset": "last"},
                    )
                except Exception as exc:
                    logger.warning(f"Failed starting stream consumer for {stream_name}: {exc}")

    async def publish_message(
        self,
        exchange_name: str,
        routing_key: str,
        payload: Dict[str, Any],
        correlation_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> bool:
        """
        Publishes a persistent message with publisher confirmations.
        Returns True on successful broker confirmation.
        """
        body = json.dumps(payload, default=str).encode("utf-8")

        if self.is_connected and self._channel:
            try:
                exchange = await self._channel.get_exchange(exchange_name)
                message = aio_pika.Message(
                    body=body,
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    content_type="application/json",
                    correlation_id=correlation_id,
                    message_id=message_id,
                )
                # publisher_confirms ensures broker accepted the message
                await exchange.publish(message, routing_key=routing_key)
                return True
            except Exception as exc:
                logger.warning(f"RabbitMQ publish error: {exc}. Falling back to in-memory buffer.")

        # Fallback publish
        await in_memory_broker.publish(exchange_name, routing_key, body)
        return True

    async def publish_share_document_event(self, routing_key: str, payload: Dict[str, Any]) -> bool:
        """Convenience method to publish domain events to the service events exchange."""
        rk = routing_key if routing_key.startswith(("share_document.", "share-document.")) else f"share_document.{routing_key}"
        return await self.publish_message(exchange_name=get_events_exchange(), routing_key=rk, payload=payload)

    async def consume_queue(self, queue_name: str, message_handler: Callable[[Dict[str, Any]], Any]):
        """
        Subscribes to a queue and processes messages with manual acknowledgments.
        """
        if self.is_connected and self._channel:
            try:
                queue = await self._channel.get_queue(queue_name)

                async def _on_message(message: AbstractIncomingMessage):
                    async with message.process(requeue=False, reject_on_redelivered=False):
                        try:
                            payload = json.loads(message.body.decode("utf-8"))
                            await message_handler(payload)
                        except Exception as e:
                            logger.error(f"Error handling message from {queue_name}: {e}", exc_info=True)
                            raise

                await queue.consume(_on_message)
                logger.info(f"Subscribed to RabbitMQ queue: {queue_name}")
                return
            except Exception as exc:
                logger.warning(f"Failed to consume from live RabbitMQ queue {queue_name}: {exc}")

        # Fallback consumption from in-memory queue
        task = asyncio.create_task(
            self._consume_in_memory(queue_name, message_handler),
            name=f"in-memory-consumer-{queue_name}",
        )
        self._in_memory_tasks.add(task)
        task.add_done_callback(self._in_memory_tasks.discard)

    async def _consume_in_memory(self, queue_name: str, message_handler: Callable[[Dict[str, Any]], Any]):
        q = in_memory_broker.get_queue(queue_name)
        while True:
            try:
                raw_body = await q.get()
                payload = json.loads(raw_body.decode("utf-8"))
                await message_handler(payload)
                q.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"In-memory consumer error on {queue_name}: {exc}")
                await asyncio.sleep(0.5)

    async def close(self):
        async with self._lock:
            for bg_task in (self._heartbeat_task, self._peer_monitor_task):
                if bg_task and not bg_task.done():
                    bg_task.cancel()
                    try:
                        await bg_task
                    except (asyncio.CancelledError, Exception):
                        pass
            self._heartbeat_task = None
            self._peer_monitor_task = None

            if self.is_connected:
                try:
                    logger.info(f"[MessageHub] Broadcasting offline event for '{self.service_key}'...")
                    await self._publish_presence_event("offline", reason="server_disconnected")
                except Exception:
                    pass

            for task in list(self._in_memory_tasks):
                task.cancel()
            if self._in_memory_tasks:
                await asyncio.gather(*self._in_memory_tasks, return_exceptions=True)
            self._in_memory_tasks.clear()

            if self._stream_channel and not self._stream_channel.is_closed:
                await self._stream_channel.close()
            if self._channel and not self._channel.is_closed:
                await self._channel.close()
            if self._connection and not self._connection.is_closed:
                await self._connection.close()
            self._is_connected = False
            logger.info("RabbitMQ connection closed.")


rabbitmq_manager = RabbitMQManager()
