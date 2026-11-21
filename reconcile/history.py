from station.receipts import Receipt, ReceiptStore

from .accounting import Statement
from .archive import StatementArchive


def read_history(
    receipts: ReceiptStore,
    archive: StatementArchive,
    station: str,
    record_id: str,
) -> Receipt | Statement | None:
    if not isinstance(station, str) or not isinstance(record_id, str):
        return None
    if record_id.startswith('R-'):
        return receipts.get(station, record_id)
    if record_id.startswith('S-'):
        return archive.get(station, record_id)
    return None
