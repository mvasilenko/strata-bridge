import contextlib
import shutil
from pathlib import Path

import flexitest

from constants import CONTEST_WATCHTOWER_0_VOUT
from envs import BridgeNetworkEnv
from envs.base_test import StrataTestBase
from factory.bridge_operator.config_cfg import BridgeConfigParams
from factory.bridge_operator.params_cfg import BridgeProtocolParams
from rpc.types import RpcDepositStatusComplete
from utils.bridge import get_bridge_nodes_and_rpcs, post_faulty_claim_contest_and_proof
from utils.demo_watcher import ZERO_INSTANCE
from utils.deposit import wait_until_deposit_status, wait_until_drt_recognized
from utils.dev_cli import DevCli
from utils.mosaic import get_peer_ids
from utils.utils import read_operator_key, wait_until

# Demo C. Lose the garbling material but keep the metadata, and ask the node
# whether it can still defend. See analysis/DEMO-C-README.md.
#
# Mosaic state lives in two places that can desynchronise:
#
#   FoundationDB                    tableset status, the metadata
#   <datadir>/garbling-tables/      the tables themselves, ~25 MB/peer here
#
# A disk loss, a half-completed restore, or a backup that covered one and not
# the other leaves the first saying `SetupComplete` while the second is gone.
TABLE_STORE_DIRNAME = "garbling-tables"

VICTIM_IDX = 1  # watchtower that loses its material
HEALTHY_IDX = 2  # watchtower that keeps its material
CLAIMANT_IDX = 0

COUNTERPROOF_WAIT_SECS = 240


@flexitest.register
class DemoStateLossTest(StrataTestBase):
    """What actually breaks when a watchtower loses its garbling tables?

    `DELIVERABLE-OPERATOR-MEMORY.md` argues that bridge state has no
    backup tooling and that the garbling material is the irreplaceable asset.
    Both claims were reasoned, not measured. This measures them, and the second
    one did not survive.

    Two independent results, both from deleting operator-1's entire
    garbling-table store while leaving FoundationDB untouched:

    1. **The status field does not reflect the material.**
       `mosaic_getTablesetStatus` still returns `SetupComplete` with the store
       gone, and mosaic logs nothing. Demo A's watcher and the planned exporter
       both read exactly this field (`utils/demo_watcher.py`, mirroring
       `is_setup_available` in `mosaic-client/src/client.rs:357`), so both
       would report `can_defend=True`. That gap is real regardless of result 2.

    2. **The watchtower counterproofs anyway.** A watchtower defends in the
       *garbler* role ("we are the garbler/watchtower",
       `bridge-exec/src/graph/counterproof.rs:180`), and the garbler generates
       and ships tables rather than retaining them. The store was never
       recreated and the counterproof still landed, so the object store is not
       on the watchtower defence path — the material that matters is in
       FoundationDB.

    Result 2 inverts that backup priority. It does NOT cover
    evaluator-role resilience, and it says nothing about production scale,
    where the circuit file is ~130 GB rather than ~21 MB.

    Steps:
    1. Complete a deposit so every tableset reaches SetupComplete.
    2. Record operator-1's garbler tableset status against the claimant.
    3. Stop operator-1's mosaic, delete its whole table store, restart it.
    4. Record the status again. This is result 1.
    5. Post a faulty claim and count the watchtower slots that counterproof.
       This is result 2.
    """

    def __init__(self, ctx: flexitest.InitContext):
        self.bridge_protocol_params = BridgeProtocolParams(contest_timelock=5)
        ctx.set_env(
            BridgeNetworkEnv(
                bridge_protocol_params=self.bridge_protocol_params,
                bridge_config_params=BridgeConfigParams(
                    cooperative_payout_timeout=0,
                    retry_interval_secs=1,
                ),
            )
        )

    def main(self, ctx: flexitest.RunContext):
        bridge_nodes, bridge_rpcs = get_bridge_nodes_and_rpcs(ctx)
        bridge_rpc = bridge_rpcs[0]

        bitcoind_service = ctx.get_service("bitcoin")
        bitcoin_rpc = bitcoind_service.create_rpc()

        num_operators = len(bridge_nodes)
        peer_ids = get_peer_ids(num_operators)
        operator_key_infos = [read_operator_key(i) for i in range(num_operators)]
        dev_cli = DevCli(
            bitcoind_service.props,
            operator_key_infos,
            bridge_protocol_params=self.bridge_protocol_params,
        )

        mosaic_service = ctx.get_service(f"mosaic_{VICTIM_IDX}")

        # 1. Deposit, so every tableset reaches SetupComplete.
        drt_txid = dev_cli.send_deposit_request()
        deposit_id = wait_until_drt_recognized(bridge_rpc, drt_txid)
        assert wait_until_deposit_status(bridge_rpc, deposit_id, RpcDepositStatusComplete), (
            "Deposit did not complete"
        )
        self.logger.info("Deposit completed; tablesets are set up")

        # 2. Baseline capability, read exactly as Demo A's watcher reads it.
        claimant_peer = peer_ids[CLAIMANT_IDX]
        before = self.tableset_status(mosaic_service, claimant_peer)
        self.logger.info(
            f"[STATE] before loss: operator-{VICTIM_IDX} garbler vs "
            f"operator-{CLAIMANT_IDX} = {before}"
        )
        assert before == "SetupComplete", (
            f"expected SetupComplete before the demo starts, got {before}"
        )

        # 3. Lose the material, keep the metadata.
        deleted = self.destroy_table_store(mosaic_service)

        # 4. Ask again. This is the measurement.
        after = self.tableset_status(mosaic_service, claimant_peer)
        self.logger.info(
            f"[STATE] after loss:  operator-{VICTIM_IDX} garbler vs "
            f"operator-{CLAIMANT_IDX} = {after}  ({deleted} removed)"
        )

        if after == "SetupComplete":
            self.logger.info(
                "[STATE] DETECTION GAP: the node reports SetupComplete with its entire "
                "table store deleted, and logs nothing. The field is metadata only — it "
                "does not check that the material exists. Demo A's watcher and the "
                "planned exporter both read exactly this field, so neither can "
                "distinguish healthy material from absent material. Step 5 measures "
                "whether that mattered here."
            )
        else:
            self.logger.info(
                f"[STATE] GOOD NEWS: the node reports {after!r} after losing its tables, "
                "so the capability check does notice. The plan should be softened."
            )

        # 5. Can it actually still defend? Count the watchtower slots that counterproof.
        expected = num_operators - 1
        spent = self.count_counterproofs(bitcoin_rpc, dev_cli, bridge_nodes, num_operators)
        self.logger.info(f"[STATE] {spent} of {expected} watchtower slots counterproofed")

        assert spent == expected, (
            f"only {spent} of {expected} watchtower slots counterproofed after "
            f"operator-{VICTIM_IDX} lost its table store. That reverses the measured "
            "result: the garbling tables ARE on the counterproof path, and "
            "the premise about backing up the object store is right "
            "after all. Re-check before trusting the FDB-only backup conclusion."
        )
        self.logger.info(
            f"[STATE] Operator-{VICTIM_IDX} counterproofed with no table store on disk, "
            f"same as operator-{HEALTHY_IDX} which kept its ~21 MB. The watchtower's "
            "garbler-role material lives in FoundationDB; the object store is not on "
            "this path. Evaluator-role resilience is NOT covered by this test."
        )
        return True

    def tableset_status(self, mosaic_service, peer_id: str):
        """Garbler-role tableset status against `peer_id`, as the watcher reads it."""
        rpc = mosaic_service.create_rpc()
        tsid = rpc.mosaic_getRpcTablesetId("garbler", peer_id, ZERO_INSTANCE)
        status = rpc.mosaic_getTablesetStatus(tsid)
        return status if isinstance(status, str) else next(iter(status), None)

    def destroy_table_store(self, mosaic_service) -> str:
        """Delete the whole garbling-table store from disk, leaving FDB untouched.

        Deliberately the entire directory rather than one peer's subtree: an
        earlier version deleted only the claimant's subtree and the watchtower
        counterproofed anyway, which does not distinguish "the tables are not
        needed" from "the wrong half was deleted". Losing the volume is also
        the realistic incident.

        The service command is `["mosaic", <config.toml>]` and the factory sets
        the table-store root to the same directory, so the config path locates
        the data.
        """
        datadir = Path(mosaic_service.cmd[1]).parent
        target = datadir / TABLE_STORE_DIRNAME
        assert target.is_dir(), (
            f"expected a garbling-table store at {target}; the mosaic table-store "
            "layout changed and this demo needs updating"
        )
        size_mb = sum(f.stat().st_size for f in target.rglob("*") if f.is_file()) / 1e6

        self.logger.info(f"Stopping mosaic_{VICTIM_IDX} and deleting {target}")
        mosaic_service.stop()
        shutil.rmtree(target)
        mosaic_service.start()

        rpc = mosaic_service.create_rpc()
        wait_until(
            lambda: bool(rpc.mosaic_getRpcPeerId()),
            timeout=60,
            error_msg=f"mosaic_{VICTIM_IDX} did not come back after the restart",
        )
        return f"{size_mb:.1f} MB"

    def count_counterproofs(self, bitcoin_rpc, dev_cli, bridge_nodes, num_operators) -> int:
        """Post a faulty claim and count watchtower slots spent before the timeout."""
        contest_txid = post_faulty_claim_contest_and_proof(
            bitcoin_rpc,
            dev_cli,
            deposit_idx=0,
            operator_idx=CLAIMANT_IDX,
            bridge_node_url=f"http://127.0.0.1:{bridge_nodes[CLAIMANT_IDX].props['rpc_port']}",
            seed=read_operator_key(CLAIMANT_IDX).SEED,
        )
        slots = [CONTEST_WATCHTOWER_0_VOUT + i for i in range(num_operators - 1)]

        def spent_count() -> int:
            return sum(1 for v in slots if bitcoin_rpc.proxy.gettxout(contest_txid, v) is None)

        # Give the healthy watchtower time to act, then count whatever landed.
        # The timeout is the expected path: not every slot should counterproof.
        with contextlib.suppress(Exception):
            wait_until(
                lambda: spent_count() >= num_operators - 1,
                timeout=COUNTERPROOF_WAIT_SECS,
                error_msg="not every slot counterproofed",
            )
        return spent_count()
