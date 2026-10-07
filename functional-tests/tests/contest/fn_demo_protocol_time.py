import flexitest

from constants import CONTEST_WATCHTOWER_0_VOUT
from envs import BridgeNetworkEnv
from envs.base_test import StrataTestBase
from factory.bridge_operator.config_cfg import BridgeConfigParams
from factory.bridge_operator.params_cfg import BridgeProtocolParams
from rpc.types import RpcDepositStatusComplete
from utils.bridge import get_bridge_nodes_and_rpcs, post_faulty_claim_contest_and_proof
from utils.deadline_watcher import DeadlineWatcher
from utils.deposit import (
    wait_until_deposit_status,
    wait_until_drts_recognized,
    wait_until_utxo_spent,
)
from utils.dev_cli import DevCli
from utils.utils import read_operator_key, wait_for_tx_confirmation, wait_until

# Demo E. The monitoring plan's thesis, in one log stream:
#
#   every metric the bridge emits is denominated in SECONDS
#   every deadline in the protocol is denominated in BLOCKS
#
# See analysis/DEMO-E-README.md.

# Deliberately long so the countdown is visible across many polls. The shipped
# value is 144; the arithmetic is identical and the demo is shorter.
CONTEST_TIMELOCK = 30

# Page when the contest deadline is this close. In production this number comes
# from measured admin-quorum time, not from taste — see the monitoring plan.
ALERT_AT_BLOCKS = 12

WATCHTOWER_IDX = 1
CLAIMANT_IDX = 0


@flexitest.register
class DemoProtocolTimeTest(StrataTestBase):
    """Can an operator tell how long it has left, in the unit that matters?

    The bridge is well instrumented — eight orchestrator metrics with bounded
    label cardinality, OTLP and Prometheus, a 14-component `/healthz`. All of
    it measures wall-clock seconds and counts of completed work.

    The protocol's deadlines are relative timelocks measured in Bitcoin blocks.
    Nothing in the node converts between the two, so a duty that is dispatched,
    fast and persisted scores perfectly while the window it exists to beat
    closes.

    This test posts a faulty claim, then prints the node's own duty metrics
    next to a block-denominated countdown to the contest deadline, on the same
    cadence. The duty metrics stay healthy and uninformative throughout. The
    countdown is the signal, and it does not exist today.

    Claim enumeration is stubbed — the test hands the watcher the claim txid.
    Both reimbursement RPCs are gated on the deposit's assigned operator and
    return nothing here, so a real exporter must read claims from chain. The
    deadline arithmetic is what this demonstrates.
    """

    def __init__(self, ctx: flexitest.InitContext):
        self.bridge_protocol_params = BridgeProtocolParams(
            contest_timelock=CONTEST_TIMELOCK,
        )
        ctx.set_env(
            BridgeNetworkEnv(
                bridge_protocol_params=self.bridge_protocol_params,
                bridge_config_params=BridgeConfigParams(
                    cooperative_payout_timeout=0,
                    retry_interval_secs=1,
                    prometheus_metrics=True,
                ),
            )
        )

    def main(self, ctx: flexitest.RunContext):
        bridge_nodes, bridge_rpcs = get_bridge_nodes_and_rpcs(ctx)
        bridge_rpc = bridge_rpcs[0]

        bitcoind_service = ctx.get_service("bitcoin")
        bitcoin_rpc = bitcoind_service.create_rpc()

        num_operators = len(bridge_nodes)
        operator_key_infos = [read_operator_key(i) for i in range(num_operators)]
        dev_cli = DevCli(
            bitcoind_service.props,
            operator_key_infos,
            bridge_protocol_params=self.bridge_protocol_params,
        )

        metrics_url = bridge_nodes[WATCHTOWER_IDX].props["metrics_url"]
        watcher = DeadlineWatcher(
            bitcoin_rpc=bitcoin_rpc,
            metrics_url=metrics_url,
            contest_timelock=CONTEST_TIMELOCK,
            alert_at_blocks=ALERT_AT_BLOCKS,
            logger=self.logger,
        )
        watcher.start()

        try:
            drt_txids = [dev_cli.send_deposit_request() for _ in range(2)]
            deposit_ids = wait_until_drts_recognized(bridge_rpc, drt_txids)
            assert set(deposit_ids) >= {0, 1}, f"expected deposits 0 and 1, got {deposit_ids}"
            for deposit_id in sorted(deposit_ids):
                assert wait_until_deposit_status(
                    bridge_rpc, deposit_id, RpcDepositStatusComplete
                ), f"Deposit {deposit_id} did not complete"
            self.logger.info("Deposits completed; no claim open, nothing to count down to")

            claimant_url = f"http://127.0.0.1:{bridge_nodes[CLAIMANT_IDX].props['rpc_port']}"
            claimant_seed = read_operator_key(CLAIMANT_IDX).SEED

            # Round 1 burns every watchtower's garbler tableset, so the round-2
            # claim is one nobody can answer. That is the state the deadline
            # metric exists for: an open claim with no defender and a clock.
            first_contest = post_faulty_claim_contest_and_proof(
                bitcoin_rpc,
                dev_cli,
                deposit_idx=0,
                operator_idx=CLAIMANT_IDX,
                bridge_node_url=claimant_url,
                seed=claimant_seed,
            )
            for slot in range(num_operators - 1):
                wait_until_utxo_spent(
                    bitcoin_rpc, first_contest, CONTEST_WATCHTOWER_0_VOUT + slot, timeout=300
                )
            self.logger.info("Round 1: every watchtower garbler setup consumed")

            claim_txid = dev_cli.send_claim(
                deposit_idx=1,
                operator_idx=CLAIMANT_IDX,
                bridge_node_url=claimant_url,
                seed=claimant_seed,
            )
            wait_for_tx_confirmation(bitcoin_rpc, claim_txid, timeout=300)
            watcher.watch_claim(claim_txid)

            # Let the clock run past the contest deadline. Nobody will contest,
            # which is the point.
            self.wait_past_deadline(bitcoin_rpc, watcher)
        finally:
            watcher.stop()

        self.report(watcher)
        return True

    def wait_past_deadline(self, bitcoin_rpc, watcher: DeadlineWatcher) -> None:
        """Block until the contest deadline has passed, so the countdown is visible."""
        wait_until(
            lambda: watcher.min_blocks_left is not None and watcher.min_blocks_left < 0,
            timeout=600,
            step=2,
            error_msg="the contest deadline never passed; the countdown never completed",
        )

    def report(self, watcher: DeadlineWatcher) -> None:
        assert watcher.claim_height is not None, (
            "the watcher never resolved the claim height, so no deadline was derived "
            "and the demo shows nothing"
        )
        assert watcher.alert_height is not None, (
            "the deadline passed without the watcher alerting; the threshold or the "
            "poll interval is wrong and the demo shows nothing"
        )
        deadline = watcher.claim_height + CONTEST_TIMELOCK
        self.logger.info(
            "[TIME ---- SUMMARY ----]\n"
            f"  claim confirmed at height {watcher.claim_height}\n"
            f"  contest deadline         {deadline} ({CONTEST_TIMELOCK} blocks)\n"
            f"  closest approach         {watcher.min_blocks_left} blocks remaining\n"
            f"  alert threshold          {ALERT_AT_BLOCKS} blocks\n"
            f"  alert fired              {
                'yes, at height ' + str(watcher.alert_height) if watcher.alert_height else 'NO'
            }\n"
            "  none of this is derivable from the node's own metrics, which are\n"
            "  denominated in seconds and counted up throughout"
        )
