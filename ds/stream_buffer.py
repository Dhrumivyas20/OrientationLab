"""Bounded data structures used by the simulated live sensor stream."""
from __future__ import annotations
from collections import deque
from typing import Generic, TypeVar

T = TypeVar("T")


class SensorQueue(Generic[T]):
    """FIFO with a fixed capacity; a full queue rejects instead of hiding data loss."""
    def __init__(self, capacity: int = 256):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._items: deque[T] = deque()

    def enqueue(self, item: T) -> None:
        if len(self._items) >= self.capacity:
            raise OverflowError("sensor queue is full")
        self._items.append(item)

    def dequeue(self) -> T:
        if not self._items:
            raise IndexError("dequeue from empty sensor queue")
        return self._items.popleft()

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)


class WindowedBuffer(Generic[T]):
    """Keep only the newest `capacity` items, as used for a live sparkline."""
    def __init__(self, capacity: int = 60):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._items: deque[T] = deque(maxlen=capacity)

    def append(self, item: T) -> None:
        self._items.append(item)

    def values(self) -> list[T]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)
