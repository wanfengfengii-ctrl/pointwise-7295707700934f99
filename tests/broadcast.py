from dataclasses import dataclass
from typing import Generic, TypeVar


Payload = TypeVar('Payload')
BENCH_FRAME_BUDGET = 1


@dataclass(frozen=True)
class BroadcastDelivery(Generic[Payload]):
    copies: tuple[Payload, Payload]
    frame_count: int


def broadcast(packet: Payload, frame_count: int) -> BroadcastDelivery[Payload]:
    """Deliver one opaque packet to two local endpoints and report its physical frames."""
    if type(frame_count) is not int or frame_count < 0:
        raise ValueError('frame count must be a nonnegative integer')
    return BroadcastDelivery((packet, packet), frame_count)
