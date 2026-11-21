from collections.abc import Mapping
from dataclasses import dataclass

from edition.directory import RevisionDirectory
from station.material import StationBlock


@dataclass(frozen=True)
class Coverage:
    received: tuple[StationBlock, ...]
    missing: tuple[int, ...]


def compute_coverage(directory: RevisionDirectory, positions: Mapping[int, StationBlock]) -> Coverage:
    received = []
    missing = []
    for entry in directory.entries:
        block = positions.get(entry.position)
        if block is None:
            missing.append(entry.position)
        else:
            received.append(block)
    return Coverage(tuple(received), tuple(missing))
