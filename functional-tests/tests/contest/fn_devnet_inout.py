import os
import time

import flexitest

from envs import BridgeNetworkEnv
from envs.base_test import StrataTestBase
from factory.bridge_operator.config_cfg import BridgeConfigParams
from factory.bridge_operator.params_cfg import BridgeProtocolParams
from rpc.asm_types import AssignmentEntry
from rpc.types import RpcDepositStatusComplete
from utils.bridge import get_bridge_nodes_and_rpcs
from utils.deposit import (
    wait_until_deposit_status,
    wait_until_deposit_utxo_spent,
    wait_until_drt_recognized,
)
from utils.dev_cli import DevCli
from utils.utils import read_operator_key, wait_for_tx_confirmation, wait_until
from utils.withdrawal import wait_until_active_valid_claim

# Bring the devnet up, drive a full deposit AND withdrawal, then HOLD it so
# external tools (alpen-dashboards, an exporter, a human with curl) can attach
# to a network that has real state in it.
#
#   DEVNET_HOLD_SECS=14400 ./run_test.sh -t fn_devnet_inout
#
# It also settles an open question from DEMO-A-README.md: both reimbursement
# RPCs are gated on the deposit's *assigned* operator, and Demo A only ever saw
# unassigned deposits (dev-cli posts claims directly). So "those RPCs are blind
# to a claim" might have been an artifact of the reproduction rather than a
# property of the API. This run probes them at three points -- before any
# assignment exists, with an assigned claim live, and after payout -- which
# distinguishes the two.
HOLD_SECS = int(os.environ.get("DEVNET_HOLD_SECS", 14400))

# Small enough that the uncontested payout actually completes inside the run.
CONTEST_TIMELOCK = int(os.environ.get("CONTEST_TIMELOCK", 5))


def probe(bridge_rpc, deposit_idx: int) -> dict[str, str]:
    """Call each monitoring RPC and record its answer, error included.

    An error is a result here, not a failure: whether these endpoints answer at
    all is the thing being measured.
    """
    calls = {
        "pendingWithdrawals": lambda: bridge_rpc.stratabridge_pendingWithdrawals(),
        "pendingWithdrawalInfo": lambda: bridge_rpc.stratabridge_pendingWithdrawalInfo(deposit_idx),
        "reimbursementStatus": lambda: bridge_rpc.stratabridge_reimbursementStatus(deposit_idx),
        "withdrawalStatus": lambda: bridge_rpc.stratabridge_withdrawalStatus(deposit_idx),
    }
    out = {}
    for name, call in calls.items():
        try:
            out[name] = repr(call())
        except Exception as ex:
            out[name] = f"ERROR {ex}"
    return out


@flexitest.register
class DevnetInOutTest(StrataTestBase):
    """A held devnet that has moved funds in and back out."""

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
        self.probes: list[tuple[str, dict[str, str]]] = []

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

        deposit_idx, deposit_txid = self.funds_in(bridge_rpc, dev_cli)
        self.probes.append(("after deposit, no assignment", probe(bridge_rpc, deposit_idx)))

        self.funds_out(bridge_rpc, bitcoin_rpc, asm_rpc, dev_cli, deposit_idx, deposit_txid)

        self.print_probes()
        self.print_endpoints(ctx, bridge_nodes, bitcoind_service, num_operators)
        self.hold(bitcoin_rpc)
        return True

    def funds_in(self, bridge_rpc, dev_cli) -> tuple[int, str]:
        drt_txid = dev_cli.send_deposit_request()
        deposit_idx = wait_until_drt_recognized(bridge_rpc, drt_txid)
        info = wait_until_deposit_status(bridge_rpc, deposit_idx, RpcDepositStatusComplete)
        assert info is not None, "deposit did not complete"
        deposit_txid = info["status"]["deposit_txid"]
        self.logger.info(f"[IN ] deposit {deposit_idx} complete, txid {deposit_txid}")
        return deposit_idx, deposit_txid

    def funds_out(self, bridge_rpc, bitcoin_rpc, asm_rpc, dev_cli, deposit_idx, deposit_txid):
        """Mock checkpoint -> ASM assignment -> operator claim -> payout."""
        tip_hash = bitcoin_rpc.proxy.getblockhash(bitcoin_rpc.proxy.getblockcount())
        ckp_txid = dev_cli.send_mock_checkpoint_from_tip(asm_rpc, tip_hash, num_ol_slots=1)
        ckp_block_hash = wait_for_tx_confirmation(bitcoin_rpc, ckp_txid)
        wait_until(
            lambda: len(asm_rpc.strata_asm_getAssignments(ckp_block_hash)) > 0,
            timeout=300,
            error_msg="ASM did not produce an assignment",
        )
        assignment = AssignmentEntry.from_dict(asm_rpc.strata_asm_getAssignments(ckp_block_hash)[0])
        self.logger.info(f"[OUT] assigned to operator {assignment.current_assignee}")

        claim = wait_until_active_valid_claim(bridge_rpc)
        wait_for_tx_confirmation(bitcoin_rpc, claim.claim_txid, timeout=300)
        self.logger.info(f"[OUT] claim {claim.claim_txid} confirmed")
        self.probes.append(("assigned claim live on chain", probe(bridge_rpc, deposit_idx)))

        wait_until_deposit_utxo_spent(bitcoin_rpc, deposit_txid, timeout=450)
        self.logger.info("[OUT] deposit UTXO spent — withdrawal complete")
        self.probes.append(("after payout", probe(bridge_rpc, deposit_idx)))

    def print_probes(self) -> None:
        lines = ["[PROBE ---- assignment-gated monitoring RPCs ----]"]
        for stage, results in self.probes:
            lines.append(f"  {stage}")
            for name, value in results.items():
                lines.append(f"      {name:<24} {value[:160]}")
        self.logger.info("\n".join(lines))

    def print_endpoints(self, ctx, bridge_nodes, bitcoind_service, num_operators) -> None:
        btc = bitcoind_service.props
        lines = [
            "[DEVNET ---- ENDPOINTS ----]",
            f"  bitcoin rpc    http://{btc['rpc_user']}:{btc['rpc_password']}"
            f"@127.0.0.1:{btc['rpc_port']}",
            f"  bitcoin zmq    hashblock tcp://127.0.0.1:{btc['zmq_hashblock']}",
        ]
        for idx, node in enumerate(bridge_nodes):
            lines.append(
                f"  bridge-{idx}       rpc http://127.0.0.1:{node.props['rpc_port']}"
                f"   healthz http://127.0.0.1:{node.props['rpc_port']}/healthz"
            )
            metrics = node.props.get("metrics_url")
            if metrics:
                lines.append(f"                 metrics {metrics}/metrics")
        for idx in range(num_operators):
            try:
                mosaic = ctx.get_service(f"mosaic_{idx}")
            except Exception:
                continue
            lines.append(f"  mosaic-{idx}       rpc {mosaic.props['rpc_url']}")
        self.logger.info("\n".join(lines))

    def hold(self, bitcoin_rpc) -> None:
        self.logger.info(f"[DEVNET] holding for {HOLD_SECS}s — Ctrl-C to tear down early")
        deadline = time.monotonic() + HOLD_SECS
        while time.monotonic() < deadline:
            time.sleep(30)
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
            self.logger.info(
                f"[DEVNET] alive, btc height={tip}, {int(deadline - time.monotonic())}s left"
            )
