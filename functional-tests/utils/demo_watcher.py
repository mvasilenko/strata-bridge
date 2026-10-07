"""Background capability watcher for the Demo A before/after run.

Throwaway demo tooling. The production detection path is specified in
`analysis/spec-protocol-time-exporter.md` and is a Rust sidecar, so that
protocol type changes break the build rather than silently breaking an alert.
This exists only to make the contrast visible inside one functional-test run.

What it watches, from the point of view of one watchtower: whether that node
still holds usable garbling material against each peer. The watchtower
counterproofs as the *garbler* against the claiming operator ("we are the
garbler/watchtower", `bridge-exec/src/graph/counterproof.rs:180`), so a
garbler tableset that has left `SetupComplete` is a lost defence.

The node reports nothing in that state: the path that gives up returns
`Ok(())` with a `warn!` and no metric
(`bridge-exec/src/graph/counterproof.rs:158-167`), and `/healthz` has no
notion of garbling material at all.

Scope note. `DELIVERABLE-5-DAY-SCOPE.md` rule 2 is "open claim AND NOT
can_defend". This watcher alerts on the capability half alone, because in
this reproduction the claim half is not reachable over RPC: both
`reimbursementStatus` and `pendingWithdrawalInfo` resolve through the
deposit's *assigned* operator, and these deposits carry no withdrawal
assignment, so one errors with "Deposit has no assigned operator" and the
other returns null. Claim context is still polled and logged when available.
Production needs both conjuncts — a consumed tableset after a legitimate
contest is expected, not an incident.
"""

import json
import logging
import time
from threading import Event, Thread
from urllib.request import urlopen

# Mosaic reserves `instance_id` for multiple tablesets per (garbler, evaluator)
# pair, but the bridge pins it to zero (`mosaic-client/src/util.rs:8`) and
# mosaic discards it anyway (`rpc/service/src/default.rs:127`). Mirror the
# bridge so we address the same tableset the node does.
ZERO_INSTANCE = "00" * 32

POLL_INTERVAL_SECS = 2
HEALTH_TIMEOUT_SECS = 2

# `/healthz` serialises HealthStatus::Ok as "ok" (bin/strata-bridge/src/health.rs:71).
HEALTHY = "ok"


class DemoWatcher:
    """Polls defence capability and `/healthz` on one cadence, from one watchtower.

    Start with `start()`, stop with `stop()`. `alert_height` is the chain height
    at which capability loss first became observable, or None if it never did.
    """

    def __init__(
        self,
        bridge_rpc,
        mosaic_rpc,
        bitcoin_rpc,
        health_url: str,
        peer_ids: list[str],
        pov_idx: int,
        logger: logging.Logger | None = None,
    ):
        self.bridge_rpc = bridge_rpc
        self.mosaic_rpc = mosaic_rpc
        self.bitcoin_rpc = bitcoin_rpc
        self.health_url = health_url
        self.peer_ids = peer_ids
        self.pov_idx = pov_idx
        self.log = logger or logging.getLogger(__name__)

        self.alert_height: int | None = None
        self._alerted: set[int] = set()
        self._stop = Event()
        self._thread = Thread(target=self._loop, daemon=True)

    def start(self):
        peers = [idx for idx in range(len(self.peer_ids)) if idx != self.pov_idx]
        self.log.info(f"[WATCH] watchtower {self.pov_idx} watching peers {peers}")
        self._thread.start()

    def stop(self, timeout: float = 10):
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _loop(self):
        while not self._stop.wait(timeout=POLL_INTERVAL_SECS):
            try:
                tip = self.bitcoin_rpc.proxy.getblockcount()
                self._poll_health(tip)
                self._poll_capability(tip)
            except Exception as ex:
                # A watcher that dies mid-run proves the wrong thing. Keep polling.
                self.log.warning(f"[WATCH] poll failed, retrying next tick: {ex}")

    def _poll_health(self, tip: int):
        try:
            with urlopen(self.health_url, timeout=HEALTH_TIMEOUT_SECS) as response:
                code = response.status
                snapshot = json.loads(response.read().decode())
        except Exception as ex:
            self.log.info(f"[HEALTH {_ts()} h={tip}] unreachable: {ex}")
            return

        components = snapshot.get("components", [])
        healthy = sum(1 for c in components if c.get("status") == HEALTHY)
        self.log.info(
            f"[HEALTH {_ts()} h={tip}] {code} {snapshot.get('status')}  "
            f"{healthy}/{len(components)} components healthy"
        )

    def _poll_capability(self, tip: int):
        claims = self._open_claims()
        for peer_idx in range(len(self.peer_ids)):
            if peer_idx == self.pov_idx:
                continue

            status = self._tableset_status(peer_idx)
            claim = claims.get(peer_idx, "-")

            if status is None:
                # No tableset for this peer yet. Not a loss of capability, and
                # alerting on it would fire during startup before setup lands.
                self.log.info(f"[WATCH {_ts()} h={tip}] ...     peer={peer_idx} no tableset yet")
                continue

            # Only `SetupComplete` can answer a new claim. `Contest`, `Consumed`
            # and `Aborted` all mean the material is spent; this mirrors
            # `is_setup_available` (`mosaic-client/src/client.rs:357`) minus its
            # same-game reuse case, which cannot apply to a fresh claim.
            if status == "SetupComplete":
                self.log.info(
                    f"[WATCH {_ts()} h={tip}] ok      peer={peer_idx} "
                    f"can_defend=True   claim={claim}"
                )
                continue

            self.log.info(
                f"[WATCH {_ts()} h={tip}] ALERT   peer={peer_idx} "
                f"CAPABILITY_LOST   tableset={_short(status)} claim={claim}"
            )
            if peer_idx not in self._alerted:
                self._alerted.add(peer_idx)
                if self.alert_height is None:
                    self.alert_height = tip

    def _tableset_status(self, peer_idx: int):
        tsid = self.mosaic_rpc.mosaic_getRpcTablesetId(
            "garbler", self.peer_ids[peer_idx], ZERO_INSTANCE
        )
        return self.mosaic_rpc.mosaic_getTablesetStatus(tsid)

    def _open_claims(self) -> dict[int, str]:
        """Best-effort map of claiming operator to claim phase.

        Empty whenever the deposits carry no withdrawal assignment, which is the
        case in this reproduction — see the scope note at the top of the module.
        """
        claims = {}
        for deposit_idx in self.bridge_rpc.stratabridge_depositIndices():
            try:
                info = self.bridge_rpc.stratabridge_pendingWithdrawalInfo(deposit_idx)
            except Exception:
                continue
            if not info:
                continue
            active = list(info.get("competing_claims", []))
            if info.get("assigned_claim"):
                active.append(info["assigned_claim"])
            for c in active:
                claims[c["operator"]] = c.get("phase", "?")
        return claims


def _short(status) -> str:
    """One word for a tableset status, which may be a string or a tagged object."""
    return status if isinstance(status, str) else next(iter(status), "?")


def _ts() -> str:
    return time.strftime("%H:%M:%S")
