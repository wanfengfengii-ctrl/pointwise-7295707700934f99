from dataclasses import dataclass

from edition.directory import FrozenOrigin, RevisionDirectory

from .coverage import Coverage


@dataclass(frozen=True)
class StatementLine:
    position: int
    byte_count: int
    first_receipt_id: str
    origins: tuple[FrozenOrigin, ...]


@dataclass(frozen=True)
class Statement:
    report_id: str | None
    station: str
    directory: RevisionDirectory
    received: tuple[int, ...]
    missing: tuple[int, ...]
    received_bytes: int
    lines: tuple[StatementLine, ...]


def build_statement(station: str, directory: RevisionDirectory, coverage: Coverage) -> Statement:
    lines = tuple(
        StatementLine(block.position, len(block.data), block.first_receipt_id, block.origins)
        for block in coverage.received
    )
    return Statement(
        None,
        station,
        directory,
        tuple(line.position for line in lines),
        coverage.missing,
        sum(line.byte_count for line in lines),
        lines,
    )
