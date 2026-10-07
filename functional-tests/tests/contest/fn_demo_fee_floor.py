import os

import flexitest

from envs import BridgeNetworkEnv
from envs.base_test import StrataTestBase
from factory.bridge_operator.config_cfg import BridgeConfigParams
from factory.bridge_operator.params_cfg import BridgeProtocolParams
from rpc.types import RpcDepositStatusComplete
from utils.bridge import get_bridge_nodes_and_rpcs
from utils.deposit import wait_until_deposit_status, wait_until_drt_recognized
from utils.dev_cli import DevCli
from utils.utils import read_operator_key, wait_until_bitcoind_ready

# Demo B. Raise the Bitcoin relay floor above the bridge's hardcoded presigned
# fee rate and watch the dispute game stop being playable. See
# analysis/DEMO-B-spec.md.
#
#   tx-graph/src/fee.rs:44   pub(crate) const FEE_RATE_SAT_PER_VB: u64 = 2;
#
# Value is BTC/kvB, as bitcoind expects: 0.00005 == 5 sat/vB. Modest on purpose.
# The point is that an *ordinary* fee environment breaks the protocol, not an
# extreme one. Mainnet sits above 5 sat/vB routinely.
DEMO_MIN_RELAY_FEE = os.environ.get("DEMO_MIN_RELAY_FEE", "0.00005")

REJECTION = "min relay fee not met"


@flexitest.register
class DemoFeeFloorTest(StrataTestBase):
    """Is the BitVM2 dispute game playable once ordinary Bitcoin fees rise?

    Every transaction in the game graph has its fee fixed at 2 sat/vB and
    committed inside an n-of-n signature, so it cannot be raised at broadcast
    time. CPFP is the intended escape hatch and is not wired: `build_cpfp_child`
    returns `CpfpChildNotImplemented` with zero callers (STR-3439) and the tx
    driver's per-block arm is an empty TODO (STR-2691).

    This test raises the relay floor to 5 sat/vB after a deposit has completed,
    then tries to post a claim. The claim is the first graph transaction either
    side must broadcast, so it is where the failure surfaces first. A FAILURE
    here is good news: it means the graph still clears at this fee level.

    The floor is raised *after* the deposit, not at startup, because staking
    and the deposit also bottom out at 2 sat/vB here. That is a regtest
    artifact, not the finding: regtest has no fee history, so
    `estimate_smart_fee` returns nothing and `estimate_funding_fee_rate` falls
    back to `fee::FEE_RATE` (`bridge-exec/src/stake/staking.rs:199-226`). Those
    transactions are wallet-funded and do adapt upward where estimation works.
    The presigned graph transactions cannot adapt anywhere.

    Steps:
    1. Complete a deposit at the default 1 sat/vB floor.
    2. Restart bitcoind with the floor above the presigned rate.
    3. Attempt a claim and assert Bitcoin rejects it for fee.
    """

    def __init__(self, ctx: flexitest.InitContext):
        self.bridge_protocol_params = BridgeProtocolParams(contest_timelock=5)
        ctx.set_env(
            BridgeNetworkEnv(
                bridge_protocol_params=self.bridge_protocol_params,
                bridge_config_params=BridgeConfigParams(cooperative_payout_timeout=0),
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

        # 1. Deposit at the default floor, so staking and the deposit graph land.
        drt_txid = dev_cli.send_deposit_request()
        deposit_id = wait_until_drt_recognized(bridge_rpc, drt_txid)
        assert wait_until_deposit_status(bridge_rpc, deposit_id, RpcDepositStatusComplete), (
            "Deposit did not complete at the default fee floor"
        )
        self.logger.info("Deposit completed at the default 1 sat/vB floor")

        # 2. Raise the floor. Everything presigned is now underpriced.
        self.raise_fee_floor(bitcoind_service, bitcoin_rpc)

        # 3. The graph is no longer playable by anyone.
        self.assert_claim_rejected(dev_cli, bridge_nodes)
        return True

    def raise_fee_floor(self, bitcoind_service, bitcoin_rpc):
        """Restart bitcoind with `-minrelaytxfee` above the presigned rate.

        `-minrelaytxfee` is startup-only, so the floor cannot rise under a
        running node. Restarting reuses the same datadir and ports: the chain
        is on disk, the bridge's RPC calls open a fresh connection each time,
        and ZMQ subscribers reconnect on their own.
        """
        self.logger.info(f"Restarting bitcoind with -minrelaytxfee={DEMO_MIN_RELAY_FEE}")
        bitcoind_service.stop()
        bitcoind_service.cmd.append(f"-minrelaytxfee={DEMO_MIN_RELAY_FEE}")
        bitcoind_service.start()
        wait_until_bitcoind_ready(bitcoin_rpc, props=bitcoind_service.props)

        floor = bitcoin_rpc.proxy.getmempoolinfo()["minrelaytxfee"]
        assert float(floor) == float(DEMO_MIN_RELAY_FEE), (
            f"bitcoind reports minrelaytxfee={floor}, expected {DEMO_MIN_RELAY_FEE}; "
            "the restart did not take effect and the demo would prove nothing"
        )
        self.logger.info(f"Fee floor now {floor} BTC/kvB against a presigned 2 sat/vB")

    def assert_claim_rejected(self, dev_cli, bridge_nodes):
        """Fail if the claim still reaches the mempool at the raised floor."""
        try:
            claim_txid = dev_cli.send_claim(
                deposit_idx=0,
                operator_idx=0,
                bridge_node_url=f"http://127.0.0.1:{bridge_nodes[0].props['rpc_port']}",
                seed=read_operator_key(0).SEED,
            )
        except RuntimeError as ex:
            assert REJECTION in str(ex), (
                f"claim failed, but not for fee. Expected {REJECTION!r} in:\n{ex}"
            )
            # The message carries the exact shortfall, e.g. "394 < 985".
            self.logger.info(
                f"Claim rejected by Bitcoin for fee at a {DEMO_MIN_RELAY_FEE} BTC/kvB floor: "
                f"{_rejection_detail(str(ex))}"
            )
            return

        raise AssertionError(
            f"claim {claim_txid} was accepted at a {DEMO_MIN_RELAY_FEE} BTC/kvB floor; "
            "the presigned fee still cleared, so this fee level does not demonstrate "
            "the problem. Raise DEMO_MIN_RELAY_FEE and re-run."
        )


def _rejection_detail(message: str) -> str:
    """The `min relay fee not met, <paid> < <required>` fragment, for the log."""
    start = message.index(REJECTION)
    return message[start : message.index('"', start)]
