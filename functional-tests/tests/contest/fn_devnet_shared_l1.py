import os
import time

import flexitest

from envs import BitcoinEnvConfig, ExternalBtcBridgeNetworkEnv
from envs.base_test import StrataTestBase
from factory.bridge_operator.config_cfg import BridgeConfigParams
from factory.bridge_operator.params_cfg import BridgeProtocolParams
from rpc.types import RpcDepositStatusComplete
from utils.bridge import get_bridge_nodes_and_rpcs
from utils.deposit import wait_until_deposit_status, wait_until_drt_recognized
from utils.dev_cli import DevCli
from utils.utils import read_operator_key

# A bridge network attached to the L1 that the L2 stack is already running on,
# so one Bitcoin chain carries both.
#
#   1. start the L2 first (it owns the chain):
#        cd alpen-ee/functional-tests
#        DEVNET_HOLD_SECS=28800 ./run_tests.sh -t test_devnet_l2
#
#   2. export the vars it prints, then:
#        cd strata-bridge/functional-tests
#        BRIDGE_EXTERNAL_BITCOIN=1 DEVNET_HOLD_SECS=28800 \
#          ./run_test.sh -t fn_devnet_shared_l1
#
# Why this matters: with a bridge-only devnet the dashboard's Alpen and Strata
# sequencer rows read `offline`, and `Status` is a two-variant enum so it cannot
# say "not deployed here". Sharing the L1 makes every row on the dashboard
# describe the same network instead of two unrelated ones.
#
# The L2's bitcoind only publishes the five ZMQ streams the bridge needs because
# of a patch to alpen-ee/functional-tests/factories/bitcoin.py; upstream it
# publishes none.
HOLD_SECS = int(os.environ.get("DEVNET_HOLD_SECS", 28800))
CONTEST_TIMELOCK = int(os.environ.get("CONTEST_TIMELOCK", 10))
SEED_DEPOSITS = int(os.environ.get("DEVNET_SEED_DEPOSITS", 1))


@flexitest.register
class DevnetSharedL1Test(StrataTestBase):
    """Bridge operators on the same Bitcoin chain as the OL and EE."""

    def __init__(self, ctx: flexitest.InitContext):
        self.bridge_protocol_params = BridgeProtocolParams(
            contest_timelock=CONTEST_TIMELOCK,
        )
        ctx.set_env(
            ExternalBtcBridgeNetworkEnv(
                bridge_protocol_params=self.bridge_protocol_params,
                bridge_config_params=BridgeConfigParams(
                    cooperative_payout_timeout=0,
                    retry_interval_secs=1,
                    prometheus_metrics=True,
                ),
                btc_config=BitcoinEnvConfig(),
            )
        )

    def main(self, ctx: flexitest.RunContext):
        bridge_nodes, bridge_rpcs = get_bridge_nodes_and_rpcs(ctx)
        bridge_rpc = bridge_rpcs[0]
        bitcoind_service = ctx.get_service("bitcoin")
        bitcoin_rpc = bitcoind_service.create_rpc()
        num_operators = len(bridge_nodes)

        if SEED_DEPOSITS:
            dev_cli = DevCli(
                bitcoind_service.props,
                [read_operator_key(i) for i in range(num_operators)],
                bridge_protocol_params=self.bridge_protocol_params,
            )
            for _ in range(SEED_DEPOSITS):
                drt = dev_cli.send_deposit_request()
                idx = wait_until_drt_recognized(bridge_rpc, drt)
                assert wait_until_deposit_status(bridge_rpc, idx, RpcDepositStatusComplete), (
                    f"seed deposit {idx} did not complete"
                )
                self.logger.info(f"[SHARED] deposit {idx} complete on the shared chain")

        self.print_attach(bridge_nodes, bitcoind_service)
        self.hold(bitcoin_rpc)
        return True

    def print_attach(self, bridge_nodes, bitcoind_service) -> None:
        btc = bitcoind_service.props
        lines = [
            "[SHARED-L1 ---- ATTACH ----]",
            f"  bitcoin rpc (shared with L2)  127.0.0.1:{btc['rpc_port']}",
        ]
        for idx, n in enumerate(bridge_nodes):
            lines.append(
                f"  bridge-{idx}  rpc http://127.0.0.1:{n.props['rpc_port']}"
                f"   metrics {n.props.get('metrics_url')}/metrics"
            )
        lines += [
            "",
            "  the dashboard now has every row on ONE chain:",
            "    bridge panel     <- these operators",
            "    sequencer rows   <- the OL and EE on the same bitcoind",
        ]
        self.logger.info("\n".join(lines))

    def hold(self, bitcoin_rpc) -> None:
        self.logger.info(f"[SHARED] holding {HOLD_SECS}s -- Ctrl-C to tear down early")
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
            self.logger.info(
                f"[SHARED] alive, btc height={tip}, {int(until - time.monotonic())}s left"
            )
