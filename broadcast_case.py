import argparse
import json
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory

from edition import RevisionCatalog, freeze
from intake import DeviceSegment, DraftLayout, DraftStore, open_and_load
from reconcile import StatementArchive, read_history, reconcile
from shipment import IssueHistory, encode_literal_packet, issue
from station import ReceiptStore, StationMaterial, accept
from tests.broadcast import BENCH_FRAME_BUDGET, broadcast


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def prepare(source, desks, coded):
    drafts, catalog, issues = DraftStore(source), RevisionCatalog(source), IssueHistory(source)
    layout = DraftLayout("bench", 2, 1)
    draft = open_and_load(drafts, layout, (
        DeviceSegment("device-0", 0, bytes.fromhex("01")),
        DeviceSegment("device-1", 1, bytes.fromhex("02")),
    ))
    directory = freeze(drafts, catalog, draft.draft_id, "R1").directory
    before = {}
    for name, held in (("A", 0), ("B", 1)):
        material, receipts, archive = (StationMaterial(desks[name]), ReceiptStore(desks[name]),
                                       StatementArchive(desks[name]))
        accept(material, receipts, name, issue(catalog, issues, "bench", "R1", name, [held]))
        before[name] = reconcile(material, archive, name, directory)
        require(before[name].missing == (1 - held,), f"wrong opening coverage at {name}")

    if coded:
        from shipment import plan_broadcast

        packet = plan_broadcast(catalog, issues, "bench", "R1", "A", {0}, (1,), "B", {1}, (0,),
                                BENCH_FRAME_BUDGET)
        require(isinstance(packet, bytes), "one-frame job should be feasible")
        payload = json.loads(packet)
        require(issues.get(payload["packet_id"]) == packet, "issued wire changed in history")
        delivery = broadcast(packet, len(payload["frames"]))
        require(delivery.frame_count == BENCH_FRAME_BUDGET, "the two stations share one frame")
    else:
        literal_a = issue(catalog, issues, "bench", "R1", "A", before["A"].missing)
        literal_b = issue(catalog, issues, "bench", "R1", "B", before["B"].missing)
        needed = len(literal_a.blocks) + len(literal_b.blocks)
        require(needed == 2 and needed > BENCH_FRAME_BUDGET, "literal exchange should need two frames")
        delivery = broadcast(encode_literal_packet(literal_a), len(literal_a.blocks))
    return directory, before, delivery


def check_gap(db, directory, before, delivery):
    material, receipts, archive = StationMaterial(db), ReceiptStore(db), StatementArchive(db)
    try:
        accept(material, receipts, "B", delivery.copies[1])
    except ValueError as error:
        require("different station" in str(error), "literal delivery failed for an unexpected reason")
    else:
        raise RuntimeError("B accepted a literal delivery addressed to A")
    require(reconcile(material, archive, "B", directory).missing == before.missing,
            "the rejected delivery changed B coverage")
    return {"literal_frames_needed": 2, "budget": BENCH_FRAME_BUDGET, "b_still_missing": list(before.missing)}


def check_broadcast(db, name, directory, before, packet, target):
    from station import accept_broadcast

    material, receipts, archive = StationMaterial(db), ReceiptStore(db), StatementArchive(db)
    receipt = accept_broadcast(material, receipts, name, packet)
    require(len(receipt.lines) == 1, f"wrong receipt line count at {name}")
    line = receipt.lines[0]
    require(line.position == target and line.data == bytes([target + 1]), f"wrong decoded block at {name}")
    require(receipt.directory == directory and line.origins == directory.entries[target].origins,
            f"wrong frozen origin at {name}")
    require(line.first_receipt_id == receipt.receipt_id, f"wrong first receipt at {name}")
    after = reconcile(material, archive, name, directory)
    require((after.received, after.missing, after.received_bytes) == ((0, 1), (), 2), f"incomplete report at {name}")
    require(read_history(receipts, archive, name, before.report_id) == before, f"old report changed at {name}")
    return receipt, {"received": list(after.received), "bytes": after.received_bytes,
                     "top_up": [line.position, line.data.hex()], "receipt": receipt.receipt_id}


def run(root, coded):
    source_path = root / "source.sqlite"
    desks = {name: root / f"desk-{name}.sqlite" for name in ("A", "B")}
    with ExitStack() as stack:
        source = stack.enter_context(closing(sqlite3.connect(source_path)))
        connections = {name: stack.enter_context(closing(sqlite3.connect(path))) for name, path in desks.items()}
        directory, before, delivery = prepare(source, connections, coded)
    source_path.rename(root / "source.offline")
    if not coded:
        with closing(sqlite3.connect(desks["B"])) as db:
            return check_gap(db, directory, before["B"], delivery)

    from station import accept_broadcast

    summary = {"broadcast_frames": delivery.frame_count}
    for name, packet, target in zip(desks, delivery.copies, (1, 0)):
        with closing(sqlite3.connect(desks[name])) as db:
            receipt, summary[name] = check_broadcast(db, name, directory, before[name], packet, target)
        with closing(sqlite3.connect(desks[name])) as db:
            material, receipts, archive = StationMaterial(db), ReceiptStore(db), StatementArchive(db)
            require(read_history(receipts, archive, name, receipt.receipt_id) == receipt,
                    f"receipt could not be read after reopening {name}")
            replay = json.dumps(json.loads(packet), indent=2).encode("utf-8")
            require(accept_broadcast(material, receipts, name, replay) == receipt,
                    f"broadcast replay did not return the first receipt at {name}")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Run the two-station exchange sample.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--broadcast", action="store_true")
    modes.add_argument("--verify", action="store_true")
    options = parser.parse_args()
    if options.verify:
        from check_project import main as check_existing

        result = check_existing()
        if result:
            return result
    with TemporaryDirectory(prefix="bench-broadcast-") as directory:
        print(json.dumps(run(Path(directory), options.broadcast or options.verify), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
