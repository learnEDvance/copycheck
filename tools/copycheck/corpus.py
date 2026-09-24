"""Deterministic benchmark corpus of 18 texts for a copy-fidelity test."""

from __future__ import annotations

import argparse
import json
import os
import random

SEED = 12345
VERSION = 1

TYPES = ["gibberish", "prose", "code", "list", "table", "data"]
LENGTHS = ["S", "M", "L"]

GIBBERISH_CHARS = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUV"
    "WXYZ0123456789 ,.;:!?()[]{}//-_-=+**@#%&"
)


def CELL_KEYS() -> list[str]:
    return [f"{t}-{length}" for t in TYPES for length in LENGTHS]


_PROSE_S = (
    'Two hundred milliseconds of replication lag is fine for staging reads, so long as every acknowledged write is durable first.'
)

_PROSE_M = (
    'Distributed systems are distinguished less by their topology than by the contracts they impose on failure, and none is more consequential than the acknowledgement. When a leader confirms a write before its peers have committed the same entry, it borrows durability from a promise that the protocol may later be forced to revoke. We therefore model every replica as a state machine that advances only through a totally ordered log, and we treat an inflight entry as provisional until a quorum of voters has persisted it. In our implementation the follower applies the entry lazily and reports its durable watermark back to the leader at intervals of at most one hundred milliseconds.'
)

_PROSE_L = (
    'Every distributed ledger we operate is, at bottom, a single append-only file partitioned across a fleet of ordinary machines, and the elegance of that arrangement is that correctness flows from one narrow rule: an entry is committed only once a majority of voters have acknowledged it from persistent storage. Everything else in the stack, from the leader election timer to the background compactor, exists to make that rule cheap enough to honour at line rate. This report summarises how the current generation satisfies the rule in production, what we measured, and where the headroom lies.\n'
    '\n'
    'We begin with the acknowledgement path. A client request is parsed, checksummed, and handed to the append pipeline, which has two stages. The first stage serialises the entry into the local write-ahead log and returns a durable offset once the fsync completes; the second stage broadcasts the same entry to the voter set and awaits replies. We do not interleave the two stages, because doing so would allow a fast follower to outrun the writer and produce a non-causal order of application. In practice the pipeline sustains thirty-eight thousand acknowledgements per second per partition, with a tail latency at the ninety-ninth percentile of eighty-one milliseconds including the network round trip.\n'
    '\n'
    "A second concern is the interaction between snapshotting and the recovery scan. When a replica falls behind, the compactor emits a snapshot that embeds the exact offset of the last included entry, so that the lagging peer can skip the log and commence from a checkpoint. We had historically treated this as a benign optimisation, but a throttled reader revealed a subtle bug: the snapshot interrupts the log reader's batching loop mid-token, and unless the interrupted batch is requeued atomically, the follower acknowledges a state that includes the snapshot's entries without having applied the interrupted batch's final byte. The fix was to make batch advancement compare-and-swap on the durable offset, and we added a property test that drives recovery with random interleavings."
)


_CODE_S = (
    'import os\n'
    '\n'
    '\n'
    'def peek(path: str) -> int:\n'
    '    with open(path, "rb") as fh:\n'
    '        data = fh.read()\n'
    '    return len(data)'
)

_CODE_M = (
    '"""Event ordering helpers for the collector agents."""\n'
    '\n'
    'import itertools\n'
    'import time\n'
    'from dataclasses import dataclass\n'
    '\n'
    '\n'
    '@dataclass(frozen=True)\n'
    'class Event:\n'
    '    """A single observed mutation with its causal token."""\n'
    '\n'
    '    seq: int\n'
    '    source_id: str\n'
    '    payload: str\n'
    '    received_at: float = time.monotonic()\n'
    '\n'
    '\n'
    'def order_events(events: Iterator[Event]) -> Iterator[Event]:\n'
    '    """Group events by source, preserving sequence."""\n'
    '    key = lambda e: e.source_id\n'
    '    for source_id, group in itertools.groupby(events, key):\n'
    '        for event in sorted(group, key=lambda e: e.seq):\n'
    '            if event.seq < 0:\n'
    '                raise ValueError(f"negative sequence {event.seq!r}")\n'
    '            yield event'
)

_CODE_L = (
    '"""Replica state machine for the copy benchmark."""\n'
    '\n'
    'from __future__ import annotations\n'
    '\n'
    'import hashlib\n'
    'import json\n'
    'from dataclasses import dataclass\n'
    '\n'
    '\n'
    'class ReplicaStateError(Exception):\n'
    '    """An entry arrived out of order."""\n'
    '\n'
    '    def __init__(self, expected: int, got: int) -> None:\n'
    '        self.expected = expected\n'
    '        self.got = got\n'
    '        super().__init__(f"expected seq {expected}, received {got}")\n'
    '\n'
    '\n'
    '@dataclass\n'
    'class LogEntry:\n'
    '    """A persistent entry within the write-ahead log."""\n'
    '\n'
    '    seq: int\n'
    '    term: int\n'
    '    data: bytes\n'
    '\n'
    '\n'
    'class Ledger:\n'
    '    """Coordinates appends and catch-up for one partition."""\n'
    '\n'
    '    def __init__(self, replica_id: str, voters: Sequence[str]) -> None:\n'
    '        self.replica_id = replica_id\n'
    '        self.voters = list(voters)\n'
    '        self._entries: list[LogEntry] = []\n'
    '        self._next_seq = 0\n'
    '\n'
    '    def append(self, data: bytes) -> int:\n'
    '        """Store a payload; return its sequence number."""\n'
    '        entry = LogEntry(seq=self._next_seq, term=1, data=data)\n'
    '        self._entries.append(entry)\n'
    '        self._next_seq += 1\n'
    '        return entry.seq\n'
    '\n'
    '    def catch_up(self, target: int) -> list[LogEntry]:\n'
    '        """Return entries up to and including ``target``."""\n'
    '        out = []\n'
    '        for entry in self._entries:\n'
    '            if entry.seq > target:\n'
    '                break\n'
    '            out.append(entry)\n'
    '        return out\n'
    '\n'
    '    def encode_log(self) -> bytes:\n'
    '        """Serialise the log to a compact, byte-stable form."""\n'
    '        rows = [\n'
    '            {"seq": e.seq, "term": e.term, "data": e.data.hex()}\n'
    '            for e in self._entries\n'
    '        ]\n'
    '        return json.dumps(rows, separators=(",", ":")).encode("utf-8")\n'
    '\n'
    '\n'
    'def replay(path: str) -> int:\n'
    '    # Apply a saved log file and return the count of restored records.\n'
    '    with open(path, "rb") as fh:\n'
    '        payload = json.load(fh)\n'
    '    applied = 0\n'
    '    for row in payload:\n'
    '        entry = LogEntry(seq=row["seq"], term=row["term"], data=bytes.fromhex(row["data"]))\n'
    '        if entry.seq != applied:\n'
    '            raise ReplicaStateError(applied, entry.seq)\n'
    '        applied += 1\n'
    '    return applied'
)


_LIST_S = (
    'Cut-over checklist:\n'
    '\n'
    '1. Freeze `schema_migrations`.\n'
    '2. Promote the standby replica.\n'
    '3. Run `--strict` verification.'
)

_LIST_M = (
    'Release verification checklist for build 4.2.1:\n'
    '\n'
    '1. **Artifact integrity.**\n'
    '   - Confirm the SHA-256 of `copycheck` matches the published digest.\n'
    '   - Spot-check the signature bundle `copycheck.sig` with the release key.\n'
    '2. **Configuration.**\n'
    '   - Verify `copycheck.toml` parses without warnings.\n'
    '   - Ensure the `output_dir` path is owned by the service user.\n'
    '3. **Battery.**\n'
    '   - Run the three-cell corpus and diff against the golden copy.\n'
    '     a. Byte-for-byte equality for the `gibberish` and `table` cells.\n'
    '     b. Normalised equality (LF only) for the `code` cell.\n'
    '4. **Rollback.**\n'
    '   - Keep the previous tarball in place until the smoke test passes.\n'
    '   - Document the `restore --from-tag` procedure in the runbook.'
)

_LIST_L = (
    'Operator checklist for the quarterly data centre migration:\n'
    '\n'
    '1. **Pre-flight checks (two weeks out).**\n'
    '   - Schedule maintenance on the staging cluster; agree the window with\n'
    '     the on-call roster so that no overlapping deploys are in flight.\n'
    '   - Confirm the network circuit between `dc-eu-west-1a` and\n'
    '     `dc-eu-west-1b` has a round-trip time under 3ms on 200 probes.\n'
    '   - Freeze the configuration repository and tag it `migrate-2026-09`.\n'
    '   - Notify the security desk so the access logs are rotated early.\n'
    '2. **Data movement (seven days out).**\n'
    '   - Snapshot each partition using `copycheck snapshot` with the locked\n'
    '     range option, and record the resulting manifest hashes.\n'
    '     a. The `orders` partition must finish its sync within 36 hours.\n'
    '     b. The `audit` partition may use the throttled bandwidth window.\n'
    '   - Verify checksum parity via the reconciliation job; any mismatch\n'
    '     fails the batch and writes a `REJECTED` marker to the control topic.\n'
    '3. **Cut-over day.**\n'
    '   - Place all producers into `readonly` mode and drain the ingress topic.\n'
    '   - Promote the follower; confirm the new leader reports `UP`.\n'
    '   - Run the equivalence probe for a 1% sampling of the records:\n'
    '     a. Compare `(key, seq, hash)` triples from both clusters.\n'
    '     b. Fail the switch if any triple differs after three retries.\n'
    '   - Switch the DNS record and verify round robin from edge sites.\n'
    '4. **Post-migration.**\n'
    '   - Leave the old cluster writable for 72 hours as a fallback.\n'
    '   - Monitor replication lag hourly; raise a ticket if it exceeds 5s.\n'
    '   - Announce the migration on the status page with the full runbook id.\n'
    '   - Publish the post-mortem and the as-run checklist to the wiki.\n'
    '5. **Rollback triggers.**\n'
    '   - Any unresolved `CONFLICT` on the control topic after two hours.\n'
    '   - A follower that cannot keep up with the daily compaction cadence.\n'
    '   - A verified checksum drift greater than 0.001% of total records.\n'
    '   - A DNS switch that does not converge within fifteen minutes.'
)


_TABLE_S = (
    'The table shows the default service ports.\n'
    '\n'
    '| Service | Port |\n'
    '| ------- | ---- |\n'
    '| copy    | 8080 |\n'
    '| sync    | 9091 |'
)

_TABLE_M = (
    'Table 1 summarises the benchmark runs from the last release cycle. Each row records a single run of the corpus across one worker pool; the throughput column is expressed in cells per second and the float column is the ratio of passed runs to attempted runs.\n'
    '\n'
    '| Worker pool | Cells/s | Tail ms | Passed | Attempted | Ratio  |\n'
    '| ----------- | ------- | ------- | ------ | --------- | ------ |\n'
    '| small       | 41.20   | 38.1    | 120    | 120       | 1.0000 |\n'
    '| medium      | 38.47   | 41.9    | 114    | 118       | 0.9661 |\n'
    '| large       | 33.02   | 55.7    | 87     | 91        | 0.9560 |\n'
    '| gpu         | 61.88   | 27.3    | 90     | 92        | 0.9783 |\n'
    '| archive     | 12.05   | 109.2   | 45     | 49        | 0.9184 |'
)

_TABLE_L = (
    'Table 2 collates the integrity checks performed against the golden copy of the corpus on the last evening of the quarter. For every cell key we record the expected length in characters, the observed length, whether the two bytes matched exactly, and the digest of the file written to disk. The digest column reports the first twelve hex characters of the SHA-256 of each text, which suffices to distinguish any two cells in the corpus.\n'
    '\n'
    '| Cell key       | Expected | Observed | Exact | Digest       | Disposition |\n'
    '| -------------- | -------- | -------- | ----- | ------------ | ----------- |\n'
    '| gibberish-S    | 120      | 119      | yes   | 9fa2c01bd113 | fixed       |\n'
    '| gibberish-M    | 700      | 701      | yes   | 4b3fe8c90a52 | fixed       |\n'
    '| gibberish-L    | 2000     | 1999     | yes   | c771de44bb50 | fixed       |\n'
    '| prose-S        | 121      | 121      | yes   | 1a9d2e55f884 | fixed       |\n'
    '| prose-M        | 698      | 699      | yes   | 0e78c9d1ba3f | fixed       |\n'
    '| prose-L        | 2003     | 2003     | yes   | 5dc8fb4aa19c | fixed       |\n'
    '| code-S         | 113      | 113      | yes   | 8b1f7dd0aa31 | fixed       |\n'
    '| code-M         | 702      | 704      | yes   | 2e63fc05a9bd | fixed       |\n'
    '| code-L         | 2010     | 2011     | yes   | 67b8af221d6e | fixed       |\n'
    '| list-S         | 116      | 117      | yes   | 4cd0fa9e7c45 | fixed       |\n'
    '| list-M         | 651      | 652      | yes   | dab01376e99f | fixed       |\n'
    '| list-L         | 1994     | 1996     | yes   | 3012e557d0b8 | manual      |\n'
    '| table-S        | 114      | 114      | yes   | 9e5a32c8db14 | fixed       |\n'
    '| table-M        | 699      | 700      | yes   | 23b77e04f5a1 | fixed       |\n'
    '| table-L        | 2005     | 2006     | yes   | 8f3d2a16cc90 | manual      |\n'
    '| data-S         | 115      | 115      | yes   | 51c0e9f8d203 | fixed       |\n'
    '| data-M         | 696      | 697      | yes   | b0cf571a4e03 | fixed       |\n'
    '| data-L         | 1998     | 2000     | yes   | 7e3d214c05f8 | manual      |'
)


_DATA_S = (
    'customer_id,region,orders,revenue\n'
    '0041,eu-west,12,482.10\n'
    '0043,us-east,7,231.55\n'
    '0047,ap-south,19,1117.90\n'
    '0048,eu-west,3,64.00'
)

_DATA_M = (
    'report_date,shard,seq,source,status,latency_ms\n'
    '2026-09-21,s0,001,ingest,ok,12.4\n'
    '2026-09-21,s0,002,ingest,ok,11.9\n'
    '2026-09-21,s0,003,ingest,retry,34.7\n'
    '2026-09-21,s1,001,replay,ok,9.2\n'
    '2026-09-21,s1,002,replay,ok,8.8\n'
    '2026-09-21,s1,003,replay,failed,\n'
    '2026-09-22,s0,004,ingest,ok,10.1\n'
    '2026-09-22,s0,005,ingest,ok,13.0\n'
    '2026-09-22,s1,004,replay,ok,9.5\n'
    '2026-09-22,s2,001,manual,ok,41.2\n'
    '2026-09-22,s2,002,manual,ok,47.6\n'
    '2026-09-22,s2,003,manual,retry,52.9\n'
    '2026-09-22,s3,001,ingest,ok,9.0\n'
    '2026-09-23,s0,006,ingest,ok,11.2\n'
    '2026-09-23,s0,007,ingest,ok,10.7\n'
    '2026-09-23,s1,005,replay,ok,9.9\n'
    '2026-09-23,s2,004,manual,ok,48.1\n'
    '2026-09-23,s3,002,ingest,ok,9.6\n'
    '2026-09-23,s3,003,ingest,retry,12.3\n'
    '2026-09-24,s0,008,ingest,ok,10.9\n'
    '2026-09-24,s2,005,manual,ok,44.7'
)

_DATA_L = (
    'snapshot_id,customer_id,first_name,last_name,orders,total_spent,last_order\n'
    'snap-2026-09-01,1001,Alice,Wang,14,4187.65,2026-08-27\n'
    'snap-2026-09-01,1002,Bjorn,Sorensen,3,291.10,2026-05-02\n'
    'snap-2026-09-01,1003,Chen,Li,27,9301.48,2026-08-31\n'
    'snap-2026-09-01,1004,Dia,Nguyen,8,1550.22,2026-07-19\n'
    'snap-2026-09-01,1005,Elif,Aydin,51,22144.07,2026-08-29\n'
    'snap-2026-09-01,1006,Felix,Moreno,2,37.50,2026-01-14\n'
    'snap-2026-09-01,1007,Grace,Osei,19,6082.93,2026-08-30\n'
    'snap-2026-09-01,1008,Hans,Muller,6,844.19,2026-04-11\n'
    'snap-2026-09-01,1009,Iris,Novak,33,12050.77,2026-08-26\n'
    'snap-2026-09-01,1010,Jonas,Larsen,10,2044.31,2026-07-30\n'
    'snap-2026-09-01,1011,Kai,Tanaka,44,17603.20,2026-08-28\n'
    'snap-2026-09-01,1012,Lena,Fischer,5,601.85,2026-03-17\n'
    'snap-2026-09-01,1013,Mina,Sharma,16,3899.42,2026-08-25\n'
    'snap-2026-09-01,1014,Nico,Rossi,23,8220.14,2026-08-24\n'
    'snap-2026-09-01,1015,Owen,Doyle,1,19.99,2026-07-05\n'
    'snap-2026-09-01,1016,Perla,Costa,29,10115.33,2026-08-30\n'
    'snap-2026-09-01,1017,Quinn,Baker,12,2631.77,2026-07-22\n'
    'snap-2026-09-01,1018,Rami,Al-Farsi,37,14851.49,2026-08-27\n'
    'snap-2026-09-01,1019,Sofia,Petrov,7,1288.60,2026-02-09\n'
    'snap-2026-09-01,1020,Tariq,Haddad,25,9012.91,2026-08-22\n'
    'snap-2026-09-01,1021,Zara,Cohen,9,1852.30,2026-07-28\n'
    'snap-2026-09-01,1022,Yusuf,Demir,13,3122.85,2026-08-23\n'
    'snap-2026-09-01,1023,Xiao,Lin,21,7244.19,2026-08-21\n'
    'snap-2026-09-01,1024,Winston,Chang,4,498.73,2026-04-05\n'
    'snap-2026-09-01,1025,Valentina,Romano,30,12608.54,2026-08-29\n'
    'snap-2026-09-01,1026,Umar,Khan,17,4453.91,2026-08-26\n'
    'snap-2026-09-01,1027,Thea,Jensen,11,2398.02,2026-07-15\n'
    'snap-2026-09-01,1028,Suleiman,Aziz,26,9834.66,2026-08-27\n'
    'snap-2026-09-01,1029,Rosa,Silva,4,301.18,2026-03-22\n'
    'snap-2026-09-01,1030,Peter,Sato,35,13471.55,2026-08-28\n'
    'snap-2026-09-01,1031,Naomi,Kim,15,3210.47,2026-07-11\n'
    '\n'
    '{"snapshot": "snap-2026-09-01", "generated_by": "copycheck", "rows": 31}\n'
    '{"excluded": ["1032", "1033"], "reason": "manual_review_hold"}'
)


_GIBBERISH_TARGETS = {"S": 120, "M": 700, "L": 2000}


def _make_gibberish(length: int, rng: random.Random) -> str:
    return "".join(rng.choice(GIBBERISH_CHARS) for _ in range(length - 1)) + "\n"


def _static_text(kind: str) -> dict[str, str]:
    table = {
        "prose": (_PROSE_S, _PROSE_M, _PROSE_L),
        "code": (_CODE_S, _CODE_M, _CODE_L),
        "list": (_LIST_S, _LIST_M, _LIST_L),
        "table": (_TABLE_S, _TABLE_M, _TABLE_L),
        "data": (_DATA_S, _DATA_M, _DATA_L),
    }
    return dict(zip(LENGTHS, table[kind]))


def get_cells() -> dict[str, str]:
    cells: dict[str, str] = {}
    rng = random.Random(SEED)
    for length, target in _GIBBERISH_TARGETS.items():
        cells[f"gibberish-{length}"] = _make_gibberish(target, rng)
    for kind in ("prose", "code", "list", "table", "data"):
        for length, text in _static_text(kind).items():
            cells[f"{kind}-{length}"] = text + "\n"
    return cells


def write_corpus(dirpath: str) -> dict[str, str]:
    os.makedirs(dirpath, exist_ok=True)
    cells = get_cells()
    path = os.path.join(dirpath, "corpus.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"version": VERSION, "cells": cells}, fh, indent=2)
        fh.write("\n")
    return cells


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outdir", help="directory in which corpus.json is written")
    args = parser.parse_args()

    cells = write_corpus(args.outdir)
    total_chars = sum(len(text) for text in cells.values())
    path = os.path.join(args.outdir, "corpus.json")
    print(f"WROTE {path} ({len(cells)} cells, {total_chars} chars)")


if __name__ == "__main__":
    _main()