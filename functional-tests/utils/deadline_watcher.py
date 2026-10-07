"""Protocol-time watcher for the monitoring demo.

Polls two things on the same cadence and prints them side by side:

    wall-clock view   the node's own Prometheus duty metrics, in seconds
    protocol view     blocks until the contest deadline, derived from chain

That contrast is the whole thesis of `DELIVERABLE-MONITORING.md`: every metric
the bridge emits is denominated in seconds, every deadline in the protocol is
denominated in Bitcoin blocks, and a node scores perfectly on the first while
running out of the second.

Scope. Claim enumeration is **stubbed** — the test hands this watcher the claim
txid rather than discovering it. That is deliberate and the demo says so:
`stratabridge_reimbursementStatus` and `pendingWithdrawalInfo` both resolve
through the deposit's assigned operator and return nothing for an unassigned
claim (`server.rs:415`, `server.rs:460`), so a real exporter must enumerate
claims from chain. The arithmetic below is the part being demonstrated.
"""

import logging
import time
from threading import Event, Thread

from utils.bridge import block_height
from utils.metrics import read_prometheus_metric_sum

POLL_INTERVAL_SECS = 2

# Duty-execution metrics the node already exposes, all denominated in seconds
# or in counts of completed work. None of them know about a deadline.
WALL_CLOCK_METRICS = (
    "strata_bridge_duties_total",
    "strata_bridge_duties_in_flight",
)


class DeadlineWatcher:
    """Emits blocks-until-deadline next to the node's own wall-clock metrics."""

    def __init__(
        self,
        bitcoin_rpc,
        metrics_url: str,
        contest_timelock: int,
        alert_at_blocks: int,
        logger: logging.Logger | None = None,
    ):
        self.bitcoin_rpc = bitcoin_rpc
        self.metrics_url = metrics_url
        self.contest_timelock = contest_timelock
        self.alert_at_blocks = alert_at_blocks
        self.log = logger or logging.getLogger(__name__)

        self.claim_txid: str | None = None
        self.claim_height: int | None = None
        self.alert_height: int | None = None
        self.min_blocks_left: int | None = None
        self._stop = Event()
        self._thread = Thread(target=self._loop, daemon=True)

    def watch_claim(self, claim_txid: str) -> None:
        """Register the open claim. Stands in for chain enumeration."""
        self.claim_txid = claim_txid
        self.log.info(f"[TIME] now tracking claim {claim_txid}")

    def start(self):
        self._thread.start()

    def stop(self, timeout: float = 10):
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _loop(self):
        while not self._stop.wait(timeout=POLL_INTERVAL_SECS):
            try:
                self._poll()
            except Exception as ex:
                self.log.warning(f"[TIME] poll failed, retrying next tick: {ex}")

    def _poll(self):
        tip = self.bitcoin_rpc.proxy.getblockcount()
        wall = self._wall_clock_view()

        if self.claim_txid is None:
            self.log.info(f"[TIME {_ts()} h={tip}] {wall} | no open claim")
            return

        if self.claim_height is None:
            self.claim_height = self._tx_height(self.claim_txid)
            if self.claim_height is None:
                self.log.info(f"[TIME {_ts()} h={tip}] {wall} | claim unconfirmed")
                return

        deadline = self.claim_height + self.contest_timelock
        blocks_left = deadline - tip
        if self.min_blocks_left is None or blocks_left < self.min_blocks_left:
            self.min_blocks_left = blocks_left

        verdict = "ok    "
        if blocks_left < 0:
            verdict = "EXPIRED"
        elif blocks_left <= self.alert_at_blocks:
            verdict = "ALERT "
            if self.alert_height is None:
                self.alert_height = tip

        self.log.info(
            f"[TIME {_ts()} h={tip}] {wall} | deadline h={deadline} "
            f"blocks_left={blocks_left:>4} {verdict}"
        )

    def _wall_clock_view(self) -> str:
        """The node's own metrics. Deliberately the ones an operator watches."""
        parts = []
        for name in WALL_CLOCK_METRICS:
            try:
                value = read_prometheus_metric_sum(self.metrics_url, name)
            except Exception:
                parts.append(f"{_short(name)}=?")
                continue
            parts.append(f"{_short(name)}={value:g}")
        return " ".join(parts)

    def _tx_height(self, txid: str) -> int | None:
        block_hash = self.bitcoin_rpc.proxy.getrawtransaction(txid, True).get("blockhash")
        return None if block_hash is None else block_height(self.bitcoin_rpc, block_hash)


def _short(metric_name: str) -> str:
    return metric_name.removeprefix("strata_bridge_")


def _ts() -> str:
    return time.strftime("%H:%M:%S")
