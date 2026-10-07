import os
from typing import Any, cast

import flexitest

from constants import CONTEST_WATCHTOWER_0_VOUT, STAKE_VOUT
from envs import BridgeNetworkEnv
from envs.base_test import StrataTestBase
from factory.bridge_operator.config_cfg import BridgeConfigParams
from factory.bridge_operator.params_cfg import BridgeProtocolParams
from rpc.types import RpcDepositStatusComplete
from utils.bridge import (
    get_bridge_nodes_and_rpcs,
    post_faulty_claim_contest_and_proof,
    race_payout_against_slash,
)
from utils.demo_watcher import DemoWatcher
from utils.deposit import (
    wait_until_deposit_status,
    wait_until_drts_recognized,
    wait_until_utxo_spent,
)
from utils.dev_cli import DevCli
from utils.mosaic import get_peer_ids
from utils.stake import confirmed_stake_txid_for_operator
from utils.utils import read_operator_key

# Demo A. Same scenario twice; the only variable is whether anything is
# watching. Two separate tests would drift, so this is one test with a flag:
#
#   DEMO_WATCHER=0 ./run_test.sh -t fn_demo_capability_loss    # before
#   DEMO_WATCHER=1 ./run_test.sh -t fn_demo_capability_loss    # after
#
# The attack itself is `fn_tableset_depletion_stake_race.py`, which proves it
# pays at these parameters. See analysis/DEMO-A-spec.md.
WATCHER_ENABLED = os.environ.get("DEMO_WATCHER", "1") != "0"

ACK_TIMELOCK_BLOCKS = int(os.environ.get("ACK_TIMELOCK", 20))
CONTESTED_PAYOUT_TIMELOCK_BLOCKS = int(os.environ.get("CONTESTED_PAYOUT_TIMELOCK", 60))

RACE_TIMEOUT_SECS = 900

WATCHTOWER_IDX = 1
DISHONEST_IDX = 0


@flexitest.register
class DemoCapabilityLossTest(StrataTestBase):
    """Is the loss of every watchtower's ability to challenge visible anywhere?

    An operator burns every watchtower's garbler tableset with a sacrificial
    faulty claim, then files a second claim nobody can refute and collects.
    Throughout, `/healthz` stays 200 and every component reports healthy: the
    path that gives up returns `Ok(())` without a metric or an error
    (`bridge-exec/src/graph/counterproof.rs:158-167`).

    With `DEMO_WATCHER=1` a read-only watcher polls chain state and mosaic
    capability alongside `/healthz`, and the run prints the gap in blocks
    between the capability-loss alert and the attacker's payout. That gap is
    the response window the five-day detection plan is sized against.
    """

    def __init__(self, ctx: flexitest.InitContext):
        self.bridge_protocol_params = BridgeProtocolParams(
            contest_timelock=5,
            ack_timelock=ACK_TIMELOCK_BLOCKS,
            contested_payout_timelock=CONTESTED_PAYOUT_TIMELOCK_BLOCKS,
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

        drt_txids = [dev_cli.send_deposit_request() for _ in range(2)]
        deposit_ids = wait_until_drts_recognized(bridge_rpc, drt_txids)
        assert set(deposit_ids) >= {0, 1}, f"expected deposits 0 and 1, got {deposit_ids}"

        deposit_txids = {}
        for deposit_id in sorted(deposit_ids):
            info = wait_until_deposit_status(bridge_rpc, deposit_id, RpcDepositStatusComplete)
            assert info is not None, f"Deposit {deposit_id} did not complete"
            deposit_txids[deposit_id] = str(cast("dict[str, Any]", info)["status"]["deposit_txid"])

        dishonest_url = f"http://127.0.0.1:{bridge_nodes[DISHONEST_IDX].props['rpc_port']}"
        dishonest_seed = read_operator_key(DISHONEST_IDX).SEED

        stake_txid = confirmed_stake_txid_for_operator(bridge_rpc, bitcoin_rpc, DISHONEST_IDX)
        self.logger.info(f"Operator {DISHONEST_IDX} stake outpoint: {stake_txid}:{STAKE_VOUT}")

        watcher = self.start_watcher(ctx, bridge_nodes, bitcoin_rpc, num_operators)

        try:
            # Round 1 — sacrificial claim that burns every watchtower's garbler tableset.
            first_contest_txid = post_faulty_claim_contest_and_proof(
                bitcoin_rpc,
                dev_cli,
                deposit_idx=0,
                operator_idx=DISHONEST_IDX,
                bridge_node_url=dishonest_url,
                seed=dishonest_seed,
            )
            for slot in range(num_operators - 1):
                wait_until_utxo_spent(
                    bitcoin_rpc, first_contest_txid, CONTEST_WATCHTOWER_0_VOUT + slot, timeout=300
                )
            self.logger.info("Round 1: every watchtower garbler setup consumed")

            # Round 2 — the claim no watchtower can refute.
            post_faulty_claim_contest_and_proof(
                bitcoin_rpc,
                dev_cli,
                deposit_idx=1,
                operator_idx=DISHONEST_IDX,
                bridge_node_url=dishonest_url,
                seed=dishonest_seed,
            )

            # The slash is watched only so a slash-first outcome ends the run
            # promptly instead of burning the full timeout.
            # `fn_tableset_depletion_stake_race.py` is where that race is asserted on.
            payout_height, _ = race_payout_against_slash(
                bitcoin_rpc, deposit_txids[1], stake_txid, timeout=RACE_TIMEOUT_SECS
            )
        finally:
            if watcher is not None:
                watcher.stop()

        assert payout_height is not None, (
            "deposit-1 never paid out; the demo needs the attacker to win. Re-run the "
            "parameter sweep in fn_tableset_depletion_stake_race.py"
        )
        self.logger.info(f"Attacker collected deposit 1 at height {payout_height}")

        if watcher is None:
            self.logger.info(
                "[WATCH ---- SUMMARY ----]\n"
                "  watcher disabled; the deposit was taken with no alert, no metric and "
                "no error, while /healthz stayed 200"
            )
            return True

        assert watcher.alert_height is not None, (
            "watcher never alerted; detection did not work and the demo shows nothing"
        )
        self.logger.info(
            "[WATCH ---- SUMMARY ----]\n"
            f"  capability-loss alert fired at height {watcher.alert_height}\n"
            f"  attacker payout confirmed at height {payout_height}\n"
            f"  RESPONSE WINDOW: {payout_height - watcher.alert_height} blocks"
        )
        return True

    def start_watcher(self, ctx, bridge_nodes, bitcoin_rpc, num_operators) -> DemoWatcher | None:
        """Attach the read-only watcher to one watchtower, unless disabled."""
        if not WATCHER_ENABLED:
            self.logger.info("[WATCH] disabled (DEMO_WATCHER=0); running the 'before' half")
            return None

        rpc_port = bridge_nodes[WATCHTOWER_IDX].props["rpc_port"]
        watcher = DemoWatcher(
            bridge_rpc=bridge_nodes[WATCHTOWER_IDX].create_rpc(),
            mosaic_rpc=ctx.get_service(f"mosaic_{WATCHTOWER_IDX}").create_rpc(),
            bitcoin_rpc=bitcoin_rpc,
            health_url=f"http://127.0.0.1:{rpc_port}/healthz",
            peer_ids=get_peer_ids(num_operators),
            pov_idx=WATCHTOWER_IDX,
            logger=self.logger,
        )
        watcher.start()
        return watcher
