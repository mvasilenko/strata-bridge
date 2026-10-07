import os
import time

import flexitest

from constants import CONTEST_WATCHTOWER_0_VOUT
from envs import BridgeNetworkEnv
from envs.base_test import StrataTestBase
from factory.bridge_operator.config_cfg import BridgeConfigParams
from factory.bridge_operator.params_cfg import BridgeProtocolParams
from rpc.types import RpcDepositStatusComplete
from utils.bridge import get_bridge_nodes_and_rpcs, post_faulty_claim_contest_and_proof
from utils.deposit import (
    wait_until_deposit_status,
    wait_until_drts_recognized,
    wait_until_utxo_spent,
)
from utils.dev_cli import DevCli
from utils.utils import read_operator_key, wait_for_tx_confirmation

# A devnet left HOLDING with an undefendable claim open and its contest clock
# running, so every observability surface can be polled at once from outside.
#
#   DEVNET_HOLD_SECS=3600 CONTEST_TIMELOCK=900 ./run_test.sh -t fn_devnet_cockpit
#
# This is the setup for the rank-3 demo. `fn_demo_protocol_time` (DEMO-E) proves
# the same thing inside the harness against one node's metrics; this one leaves
# the state standing so the real dashboard, /healthz and /metrics can be read
# side by side with the block countdown. See analysis/DEMO-H-README.md.
HOLD_SECS = int(os.environ.get("DEVNET_HOLD_SECS", 3600))

# Must outlast the hold, or the window closes and the countdown sits at EXPIRED.
# The harness automines in bursts -- roughly 1.5 blocks/second here -- so this
# is deliberately far larger than the shipped 144.
CONTEST_TIMELOCK = int(os.environ.get("CONTEST_TIMELOCK", 900))

WATCHTOWER_IDX = 1
CLAIMANT_IDX = 0


@flexitest.register
class DevnetCockpitTest(StrataTestBase):
    """Hold a devnet in the state the deadline metric exists for.

    An operator has filed a claim nobody can answer, because round 1 consumed
    every watchtower's garbler tableset. From that moment there is an open
    claim, no defender, and a contest clock running — and every instrument the
    operator owns reports healthy.
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

        dev_cli = DevCli(
            bitcoind_service.props,
            [read_operator_key(i) for i in range(num_operators)],
            bridge_protocol_params=self.bridge_protocol_params,
        )

        drt_txids = [dev_cli.send_deposit_request() for _ in range(2)]
        deposit_ids = wait_until_drts_recognized(bridge_rpc, drt_txids)
        for deposit_id in sorted(deposit_ids):
            assert wait_until_deposit_status(bridge_rpc, deposit_id, RpcDepositStatusComplete), (
                f"deposit {deposit_id} did not complete"
            )
        self.logger.info("[COCKPIT] deposits 0 and 1 complete")

        claimant_url = f"http://127.0.0.1:{bridge_nodes[CLAIMANT_IDX].props['rpc_port']}"
        claimant_seed = read_operator_key(CLAIMANT_IDX).SEED

        # Round 1 burns every watchtower's garbler tableset, so the round-2
        # claim is one nobody can answer.
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
        self.logger.info("[COCKPIT] every watchtower garbler setup consumed")

        claim_txid = dev_cli.send_claim(
            deposit_idx=1,
            operator_idx=CLAIMANT_IDX,
            bridge_node_url=claimant_url,
            seed=claimant_seed,
        )
        claim_block = wait_for_tx_confirmation(bitcoin_rpc, claim_txid, timeout=300)
        claim_height = bitcoin_rpc.proxy.getblock(claim_block)["height"]
        self.logger.info(f"[COCKPIT] undefendable claim {claim_txid} at height {claim_height}")

        self.print_cockpit_args(bridge_nodes, bitcoind_service, claim_txid, claim_height)
        self.hold(bitcoin_rpc, claim_height)
        return True

    def print_cockpit_args(self, bridge_nodes, bitcoind_service, claim_txid, claim_height) -> None:
        btc = bitcoind_service.props
        node = bridge_nodes[WATCHTOWER_IDX]
        lines = [
            "[COCKPIT ---- ATTACH ----]",
            f"  bitcoin rpc    http://{btc['rpc_user']}:{btc['rpc_password']}"
            f"@127.0.0.1:{btc['rpc_port']}",
            f"  claim txid     {claim_txid}",
            f"  claim height   {claim_height}",
            f"  contest window {CONTEST_TIMELOCK} blocks "
            f"-> deadline {claim_height + CONTEST_TIMELOCK}",
            "",
            "  python3 analysis/tools/demo_cockpit.py \\",
            f"      --bridge http://127.0.0.1:{node.props['rpc_port']} \\",
            f"      --metrics {node.props['metrics_url']}/metrics \\",
            f"      --bitcoind http://{btc['rpc_user']}:{btc['rpc_password']}"
            f"@127.0.0.1:{btc['rpc_port']} \\",
            "      --dashboard http://127.0.0.1:3000 \\",
            f"      --claim-height {claim_height} --timelock {CONTEST_TIMELOCK}",
        ]
        for idx, n in enumerate(bridge_nodes):
            lines.append(f"  bridge-{idx}       rpc http://127.0.0.1:{n.props['rpc_port']}")
        self.logger.info("\n".join(lines))

    def hold(self, bitcoin_rpc, claim_height: int) -> None:
        deadline_height = claim_height + CONTEST_TIMELOCK
        self.logger.info(f"[COCKPIT] holding {HOLD_SECS}s — Ctrl-C to tear down early")
        until = time.monotonic() + HOLD_SECS
        while time.monotonic() < until:
            time.sleep(30)
            # A held devnet must never die because a status read failed; see
            # fn_devnet_shared_l1 for why Errno 49 shows up here.
            try:
                tip = bitcoin_rpc.proxy.getblockcount()
            except Exception as ex:  # noqa: BLE001
                self.logger.warning(f"[COCKPIT] tip read failed, continuing: {ex}")
                continue
            self.logger.info(
                f"[COCKPIT] alive, tip={tip}, blocks_left={deadline_height - tip}, "
                f"{int(until - time.monotonic())}s hold left"
            )
