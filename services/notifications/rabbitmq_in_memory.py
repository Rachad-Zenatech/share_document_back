"""In-memory fallback broker used when no live RabbitMQ is available."""

import asyncio
from typing import Callable, Dict, List


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
        elif "ma" in routing_key:
            if "event" in exchange or "event" in routing_key:
                target_queues.append("ceo.ma.events")
            else:
                target_queues.append("ma.commands")
        else:
            target_queues.append(routing_key)

        for q_name in target_queues:
            q = self.get_queue(q_name)
            await q.put(message)

    def clear(self):
        self._queues.clear()
        self._subscribers.clear()


in_memory_broker = InMemoryMessageBroker()
