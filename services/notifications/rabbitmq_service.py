"""
RabbitMQ Service & Topology Manager for Share Document Portal.
Mirrors the microservice message topology in CEO Dashboard and Admin:
- Resilient connection management with multi-host candidate fallback
- Topology declaration (Durable exchanges, dead-letter routing DLX/DLQ)
- Publisher confirmations for guaranteed delivery
- Consumer subscription with manual acknowledgments
- In-memory fallback broker for seamless offline development
"""

import os
import re
import json
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Set

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


def _sanitize_amqp_url(url: str) -> str:
    """Mask password from AMQP URLs to prevent logging credentials."""
    if not url:
        return ""
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:****@", url)


def get_rabbitmq_host() -> str:
    return os.getenv("RABBITMQ_HOST", "127.0.0.1")


def get_rabbitmq_port() -> int:
    try:
        return int(os.getenv("RABBITMQ_PORT", "5672"))
    except (ValueError, TypeError):
        return 5672


def get_rabbitmq_user() -> str:
    return (
        os.getenv("RABBITMQ_DEFAULT_USER")
        or os.getenv("RABBITMQ_USERNAME")
        or os.getenv("RABBITMQ_USER")
        or "guest"
    )


def get_rabbitmq_password() -> str:
    return (
        os.getenv("RABBITMQ_DEFAULT_PASS")
        or os.getenv("RABBITMQ_PASSWORD")
        or os.getenv("RABBITMQ_PASS")
        or "guest"
    )


def get_rabbitmq_url() -> str:
    explicit = os.getenv("RABBITMQ_URL")
    if explicit:
        return explicit
    host = get_rabbitmq_host()
    port = get_rabbitmq_port()
    user = get_rabbitmq_user()
    password = get_rabbitmq_password()
    return f"amqp://{user}:{password}@{host}:{port}/"


def get_commands_exchange() -> str:
    return os.getenv("COMMANDS_EXCHANGE", "ceo.commands")


def get_events_exchange() -> str:
    return os.getenv("EVENTS_EXCHANGE", "service.events")


def get_dlx_exchange() -> str:
    return os.getenv("DLX_EXCHANGE", "ceo.commands.dlx")


def get_prefetch_count() -> int:
    try:
        return int(os.getenv("RABBITMQ_PREFETCH", "20"))
    except (ValueError, TypeError):
        return 20


# Backward-compatibility module constants
COMMANDS_EXCHANGE_NAME = get_commands_exchange()
EVENTS_EXCHANGE_NAME = get_events_exchange()
DLX_EXCHANGE_NAME = get_dlx_exchange()
PREFETCH_COUNT = get_prefetch_count()
RABBITMQ_HOST = get_rabbitmq_host()
RABBITMQ_PORT = get_rabbitmq_port()
RABBITMQ_USER = get_rabbitmq_user()
RABBITMQ_PASS = get_rabbitmq_password()
RABBITMQ_URL = get_rabbitmq_url()


class InMemoryMessageBroker:
    """
    In-memory fallback & mock broker for unit testing and offline development
    when a live RabbitMQ broker container is not running.
    """
    def __init__(self):
        self._queues: Dict[str, asyncio.Queue] = {}
        self._subscribers: Dict[str, List[Callable]] = {}

    def get_queue(self, queue_name: str) -> asyncio.Queue:
        if queue_name not in self._queues:
            self._queues[queue_name] = asyncio.Queue()
        return self._queues[queue_name]

    async def publish(self, exchange: str, routing_key: str, message: bytes):
        target_queues = []
        if "share_document" in routing_key or "share-document" in routing_key:
            if "event" in exchange or "event" in routing_key:
                target_queues.append("ceo.share_document.events")
            else:
                target_queues.append("share_document.commands")
        elif "administration" in routing_key or "admin" in routing_key:
            if "event" in exchange or "event" in routing_key:
                target_queues.append("ceo.administration.events")
            else:
                target_queues.append("administration.commands")
        else:
            target_queues.append(routing_key)

        for q_name in target_queues:
            q = self.get_queue(q_name)
            await q.put(message)

    def clear(self):
        self._queues.clear()
        self._subscribers.clear()


in_memory_broker = InMemoryMessageBroker()


class RabbitMQManager:
    def __init__(self, amqp_url: Optional[str] = None):
        self._custom_amqp_url = amqp_url
        self._connection: Optional[AbstractRobustConnection] = None
        self._channel: Optional[AbstractRobustChannel] = None
        self._commands_exchange: Optional[AbstractExchange] = None
        self._events_exchange: Optional[AbstractExchange] = None
        self._dlx_exchange: Optional[AbstractExchange] = None
        self._is_connected = False
        self._use_fallback = False
        self._lock = asyncio.Lock()
        self._in_memory_tasks: Set[asyncio.Task] = set()

    @property
    def is_connected(self) -> bool:
        return self._is_connected and (self._connection is not None) and (not self._connection.is_closed)

    @property
    def using_fallback(self) -> bool:
        return self._use_fallback

    async def connect(self, max_retries: int = 3, retry_delay: float = 1.0) -> bool:
        """
        Connects to RabbitMQ with automatic retries and fallback to candidate hosts.
        Falls back to in-memory broker if unreachable.
        """
        if not AIO_PIKA_AVAILABLE or aio_pika is None:
            logger.warning("aio-pika is not installed. Using in-memory fallback broker.")
            self._use_fallback = True
            self._is_connected = False
            return False

        async with self._lock:
            if self.is_connected:
                return True

            candidates = []
            if self._custom_amqp_url:
                candidates.append(self._custom_amqp_url)
            else:
                primary = get_rabbitmq_url()
                candidates.append(primary)
                host = get_rabbitmq_host()
                port = get_rabbitmq_port()
                user = get_rabbitmq_user()
                password = get_rabbitmq_password()
                for alt_host in ("127.0.0.1", "localhost", "rabbitmq"):
                    if alt_host != host:
                        alt_url = f"amqp://{user}:{password}@{alt_host}:{port}/"
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
                        )
                        self.amqp_url = target_url
                        self._channel = await self._connection.channel(publisher_confirms=True)
                        await self._channel.set_qos(prefetch_count=get_prefetch_count())

                        await self._setup_topology()

                        self._is_connected = True
                        self._use_fallback = False
                        logger.info("RabbitMQ connection and topology successfully initialized.")
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
        share_doc_dlq = await self._channel.declare_queue("share_document.commands.dlq", durable=True)
        await share_doc_dlq.bind(self._dlx_exchange, routing_key="share_document.#")
        await share_doc_dlq.bind(self._dlx_exchange, routing_key="share-document.#")

        admin_dlq = await self._channel.declare_queue("administration.commands.dlq", durable=True)
        await admin_dlq.bind(self._dlx_exchange, routing_key="administration.#")

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

        # 4. Share Document Service Command Queue (with DLX configured)
        share_doc_queue = await self._channel.declare_queue(
            "share_document.commands",
            durable=True,
            arguments={
                "x-dead-letter-exchange": dlx_name,
                "x-dead-letter-routing-key": "share_document.dlq",
            },
        )
        await share_doc_queue.bind(self._commands_exchange, routing_key="share_document.#")
        await share_doc_queue.bind(self._commands_exchange, routing_key="share-document.#")

        # 5. Share Document Events Queue (for CEO Dashboard / Ecosystem result tracking)
        ceo_share_doc_events_queue = await self._channel.declare_queue(
            "ceo.share_document.events",
            durable=True,
        )
        await ceo_share_doc_events_queue.bind(self._events_exchange, routing_key="share_document.#")
        await ceo_share_doc_events_queue.bind(self._events_exchange, routing_key="share-document.#")

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
                await exchange.publish(message, routing_key=routing_key)
                return True
            except Exception as exc:
                logger.warning(f"RabbitMQ publish error: {exc}. Falling back to in-memory buffer.")

        # Fallback publish
        await in_memory_broker.publish(exchange_name, routing_key, body)
        return True

    async def publish_share_document_event(self, routing_key: str, payload: Dict[str, Any]) -> bool:
        """Convenience method to publish domain events to the service events exchange."""
        rk = f"share_document.{routing_key}" if not (routing_key.startswith("share_document.") or routing_key.startswith("share-document.")) else routing_key
        return await self.publish_message(
            exchange_name=get_events_exchange(),
            routing_key=rk,
            payload=payload,
        )

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
            for task in list(self._in_memory_tasks):
                task.cancel()
            if self._in_memory_tasks:
                await asyncio.gather(*self._in_memory_tasks, return_exceptions=True)
            self._in_memory_tasks.clear()

            if self._channel and not self._channel.is_closed:
                await self._channel.close()
            if self._connection and not self._connection.is_closed:
                await self._connection.close()
            self._is_connected = False
            logger.info("RabbitMQ connection closed.")


rabbitmq_manager = RabbitMQManager()
