import os
import time

import flexitest

from constants import CONTEST_WATCHTOWER_0_VOUT
from envs import BridgeNetworkEnv
from envs.base_test import StrataTestBase
from factory.bridge_operator.config_cfg import BridgeConfigParams
from factory.bridge_operator.params_cfg import BridgeProtocolParams
from rpc.asm_types import AssignmentEntry
from rpc.types import RpcDepositStatusComplete
from utils.bridge import get_bridge_nodes_and_rpcs, post_faulty_claim_contest_and_proof
from utils.deposit import (
    wait_until_deposit_status,
    wait_until_deposit_utxo_spent,
    wait_until_drts_recognized,
    wait_until_utxo_spent,
)
from utils.dev_cli import DevCli
from utils.utils import read_operator_key, wait_for_tx_confirmation, wait_until
from utils.withdrawal import wait_until_active_valid_claim

# The devnet with everything on it, held for a day.
#
#   DEVNET_HOLD_SECS=28800 ./run_test.sh -t fn_devnet_full
#
# Supersedes fn_devnet_inout (funds in and out) and fn_devnet_cockpit (an
# undefendable claim). Both scenarios on one chain, so every panel of the real
# dashboard has something to say and the two findings are visible side by side:
#
#   deposit 0   a real withdrawal, claimed and paid out      -> DEMO-G
#               the bridge reports it complete; the dashboard's withdrawal and
#               reimbursement panels stay empty, because they are downstream of
#               an EVM indexer that is not there
#
#   deposit 1   the burn round: a faulty claim, contested and proven, which
#               consumes every watchtower's garbler tableset
#
#   deposit 2   a claim nobody can answer, with its contest clock running
#               -> DEMO-H, every instrument green while the window closes
HOLD_SECS = int(os.environ.get("DEVNET_HOLD_SECS", 28800))

# One protocol parameter has to serve two opposite needs: the uncontested
# payout on deposit 0 only confirms AFTER the contest window passes, so a long
# window means no withdrawal ever completes. Short wins -- deposit 2's window
# then closes during setup, which is the state DEMO-H wants anyway.
CONTEST_TIMELOCK = int(os.environ.get("CONTEST_TIMELOCK", 10))

CLAIMANT_IDX = 0
WATCHTOWER_IDX = 1


@flexitest.register
class DevnetFullTest(StrataTestBase):
    """Funds in, funds out, and a deposit being stolen. Held."""

    def __init__(self, ctx: flexitest.InitContext):
        self.bridge_protocol_params = BridgeProtocolParams(
            contest_timelock=CONTEST_TIMELOCK,
        )
        ctx.set_env(
            BridgeNetworkEnv(
                bridge_protocol_params=self.bridge_protocol_params,
                bridge_config_params=BridgeConfigParams(
                    cooperative_payout_timeout=0,  # force the assigned-claim path
                    retry_interval_secs=1,
                    prometheus_metrics=True,
                ),
            )
        )
        self.facts: list[str] = []

    def main(self, ctx: flexitest.RunContext):
        bridge_nodes, bridge_rpcs = get_bridge_nodes_and_rpcs(ctx)
        bridge_rpc = bridge_rpcs[0]
        bitcoind_service = ctx.get_service("bitcoin")
        bitcoin_rpc = bitcoind_service.create_rpc()
        asm_rpc = ctx.get_service("asm_rpc").create_rpc()
        num_operators = len(bridge_nodes)

        dev_cli = DevCli(
            bitcoind_service.props,
            [read_operator_key(i) for i in range(num_operators)],
            bridge_protocol_params=self.bridge_protocol_params,
        )

        deposit_txids = self.funds_in(bridge_rpc, dev_cli, count=3)
        self.funds_out(bridge_rpc, bitcoin_rpc, asm_rpc, dev_cli, 0, deposit_txids[0])
        self.steal(bitcoin_rpc, dev_cli, bridge_nodes, num_operators)

        self.print_attach(bridge_nodes, bitcoind_service)
        self.hold(bitcoin_rpc)
        return True

    def funds_in(self, bridge_rpc, dev_cli, count: int) -> dict[int, str]:
        drt_txids = [dev_cli.send_deposit_request() for _ in range(count)]
        deposit_ids = wait_until_drts_recognized(bridge_rpc, drt_txids)
        txids = {}
        for deposit_id in sorted(deposit_ids):
            info = wait_until_deposit_status(bridge_rpc, deposit_id, RpcDepositStatusComplete)
            assert info is not None, f"deposit {deposit_id} did not complete"
            txids[deposit_id] = info["status"]["deposit_txid"]
        self.logger.info(f"[IN ] deposits {sorted(txids)} complete")
        self.facts.append(f"deposits {sorted(txids)} complete")
        return txids

    def funds_out(self, bridge_rpc, bitcoin_rpc, asm_rpc, dev_cli, deposit_idx, deposit_txid):
        """Mock checkpoint -> ASM assignment -> operator claim -> payout."""
        tip_hash = bitcoin_rpc.proxy.getblockhash(bitcoin_rpc.proxy.getblockcount())
        ckp_txid = dev_cli.send_mock_checkpoint_from_tip(asm_rpc, tip_hash, num_ol_slots=1)
        ckp_block = wait_for_tx_confirmation(bitcoin_rpc, ckp_txid)
        wait_until(
            lambda: len(asm_rpc.strata_asm_getAssignments(ckp_block)) > 0,
            timeout=300,
            error_msg="ASM did not produce an assignment",
        )
        assignment = AssignmentEntry.from_dict(asm_rpc.strata_asm_getAssignments(ckp_block)[0])
        claim = wait_until_active_valid_claim(bridge_rpc)
        wait_for_tx_confirmation(bitcoin_rpc, claim.claim_txid, timeout=300)
        wait_until_deposit_utxo_spent(bitcoin_rpc, deposit_txid, timeout=450)
        self.logger.info(
            f"[OUT] deposit {deposit_idx} withdrawn by operator "
            f"{assignment.current_assignee}, claim {claim.claim_txid}"
        )
        self.facts.append(
            f"deposit {deposit_idx} withdrawn + reimbursed, claim {claim.claim_txid[:16]}..."
        )

    def steal(self, bitcoin_rpc, dev_cli, bridge_nodes, num_operators):
        """Burn every watchtower's capability, then file a claim nobody can answer."""
        claimant_url = f"http://127.0.0.1:{bridge_nodes[CLAIMANT_IDX].props['rpc_port']}"
        claimant_seed = read_operator_key(CLAIMANT_IDX).SEED

        first_contest = post_faulty_claim_contest_and_proof(
            bitcoin_rpc,
            dev_cli,
            deposit_idx=1,
            operator_idx=CLAIMANT_IDX,
            bridge_node_url=claimant_url,
            seed=claimant_seed,
        )
        for slot in range(num_operators - 1):
            wait_until_utxo_spent(
                bitcoin_rpc, first_contest, CONTEST_WATCHTOWER_0_VOUT + slot, timeout=300
            )
        self.logger.info("[STEAL] every watchtower garbler setup consumed")

        claim_txid = dev_cli.send_claim(
            deposit_idx=2,
            operator_idx=CLAIMANT_IDX,
            bridge_node_url=claimant_url,
            seed=claimant_seed,
        )
        block = wait_for_tx_confirmation(bitcoin_rpc, claim_txid, timeout=300)
        self.claim_height = bitcoin_rpc.proxy.getblock(block)["height"]
        self.claim_txid = claim_txid
        self.logger.info(
            f"[STEAL] undefendable claim {claim_txid} at height {self.claim_height}, "
            f"contest deadline {self.claim_height + CONTEST_TIMELOCK}"
        )
        self.facts.append(f"deposit 2 claimed with no possible defender, {claim_txid[:16]}...")

    def print_attach(self, bridge_nodes, bitcoind_service) -> None:
        btc = bitcoind_service.props
        node = bridge_nodes[WATCHTOWER_IDX]
        btc_url = f"http://{btc['rpc_user']}:{btc['rpc_password']}@127.0.0.1:{btc['rpc_port']}"
        lines = ["[DEVNET ---- ATTACH ----]", f"  bitcoin rpc    {btc_url}"]
        for idx, n in enumerate(bridge_nodes):
            lines.append(
                f"  bridge-{idx}       rpc http://127.0.0.1:{n.props['rpc_port']}"
                f"   metrics {n.props.get('metrics_url')}/metrics"
            )
        lines += [
            "",
            f"  claim txid     {self.claim_txid}",
            f"  claim height   {self.claim_height}  deadline "
            f"{self.claim_height + CONTEST_TIMELOCK}",
            "",
            "  on this chain:",
        ]
        lines += [f"      {f}" for f in self.facts]
        lines += [
            "",
            "  python3 analysis/tools/demo_cockpit.py \\",
            f"      --bridge http://127.0.0.1:{node.props['rpc_port']} \\",
            f"      --metrics {node.props.get('metrics_url')}/metrics \\",
            f"      --bitcoind {btc_url} \\",
            "      --dashboard http://127.0.0.1:3000 \\",
            f"      --claim-height {self.claim_height} --timelock {CONTEST_TIMELOCK}",
        ]
        self.logger.info("\n".join(lines))

    def hold(self, bitcoin_rpc) -> None:
        self.logger.info(f"[DEVNET] holding {HOLD_SECS}s — Ctrl-C to tear down early")
        until = time.monotonic() + HOLD_SECS
        while time.monotonic() < until:
            time.sleep(60)
            # A held devnet must never die because a status read failed.
            # Everything polls this bitcoind at once and TIME_WAIT builds up,
            # so connect() intermittently raises Errno 49 "Can't assign
            # requested address". Letting that escape ends main() and
            # flexitest tears the whole devnet down.
            try:
                tip = bitcoin_rpc.proxy.getblockcount()
            except Exception as ex:  # noqa: BLE001
                self.logger.warning(f"[DEVNET] tip read failed, continuing: {ex}")
                continue
            self.logger.info(f"[DEVNET] alive, tip={tip}, {int(until - time.monotonic())}s left")
