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
    block_height,
    get_bridge_nodes_and_rpcs,
    post_faulty_claim_contest_and_proof,
    race_payout_against_slash,
)
from utils.deposit import (
    wait_until_deposit_status,
    wait_until_drts_recognized,
    wait_until_utxo_spent,
)
from utils.dev_cli import DevCli
from utils.stake import confirmed_stake_txid_for_operator
from utils.utils import find_utxo_spender_txid, read_operator_key, wait_until

# The attack is a race between two timelocks, both anchored on a contest tx:
#
#   attacker's payout   = contest_2 + ack_timelock
#   round-1 slash       = contest_1 + contested_payout_timelock
#
# `check_slash_timeout` in bridge-sm/src/graph/transitions/common.rs gates the
# slash on contested_payout_timelock, NOT nack_timelock. So the attacker wins
# whenever contested_payout_timelock > ack_timelock + (contest_2 - contest_1).
#
# Shipped params are contested_payout_timelock=1008 against ack_timelock=144,
# so the attacker wins by roughly 864 blocks. It is not a close race.
#
# Sweep the boundary with:
#   ACK_TIMELOCK=60 CONTESTED_PAYOUT_TIMELOCK=60 ./run_test.sh -t fn_tableset_depletion_stake_race
ACK_TIMELOCK_BLOCKS = int(os.environ.get("ACK_TIMELOCK", 20))
CONTESTED_PAYOUT_TIMELOCK_BLOCKS = int(os.environ.get("CONTESTED_PAYOUT_TIMELOCK", 60))

RACE_TIMEOUT_SECS = 900
SLASH_LAG_TIMEOUT_SECS = 300


@flexitest.register
class TablesetDepletionStakeRaceTest(StrataTestBase):
    """Does burning every watchtower tableset actually profit the attacker?

    `fn_skip_counterproof_after_gc_consumed.py` establishes that a second faulty
    claim pays out once every watchtower's garbler setup is consumed. It asserts
    nothing about the stake, so it leaves the economics open.

    An operator has exactly one stake output (`bridge-exec/src/stake/utils.rs`:
    "there is only one stake tx"), and `SlashTx` spends it. Both faulty claims
    therefore draw on the same stake. The attack only profits if the second
    payout confirms before the first round's slash lands.

    This test measures which confirms first and asserts the attacker wins. A
    FAILURE here is good news: it means the attack does not pay, and F1 in the
    analysis should be downgraded.

    Steps:
    1. Complete two deposits and record operator-0's stake outpoint.
    2. Faulty claim on deposit 0, contested, every watchtower counterproofs.
    3. Assert the stake is still unspent — the slash has not landed yet.
    4. Faulty claim on deposit 1, contested, no counterproof possible.
    5. Race the deposit-1 payout against the slash and report both heights.
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

        dishonest_idx = 0
        dishonest_url = f"http://127.0.0.1:{bridge_nodes[dishonest_idx].props['rpc_port']}"
        dishonest_seed = read_operator_key(dishonest_idx).SEED

        stake_txid = confirmed_stake_txid_for_operator(bridge_rpc, bitcoin_rpc, dishonest_idx)
        self.logger.info(f"Operator {dishonest_idx} stake outpoint: {stake_txid}:{STAKE_VOUT}")

        # Round 1 — sacrificial claim that burns every watchtower's garbler tableset.
        first_contest_txid = post_faulty_claim_contest_and_proof(
            bitcoin_rpc,
            dev_cli,
            deposit_idx=0,
            operator_idx=dishonest_idx,
            bridge_node_url=dishonest_url,
            seed=dishonest_seed,
        )
        for slot in range(num_operators - 1):
            wait_until_utxo_spent(
                bitcoin_rpc, first_contest_txid, CONTEST_WATCHTOWER_0_VOUT + slot, timeout=300
            )
        self.logger.info("Round 1: every watchtower garbler setup consumed")

        assert bitcoin_rpc.proxy.gettxout(stake_txid, STAKE_VOUT) is not None, (
            f"stake {stake_txid}:{STAKE_VOUT} was already slashed before the second claim; "
            "the attacker cannot fund a second claim and the attack does not apply"
        )

        # Round 2 — the claim no watchtower can refute.
        post_faulty_claim_contest_and_proof(
            bitcoin_rpc,
            dev_cli,
            deposit_idx=1,
            operator_idx=dishonest_idx,
            bridge_node_url=dishonest_url,
            seed=dishonest_seed,
        )

        payout_height, slash_height = race_payout_against_slash(
            bitcoin_rpc, deposit_txids[1], stake_txid, timeout=RACE_TIMEOUT_SECS
        )

        self.logger.info(
            f"RACE RESULT: deposit-1 payout height={payout_height}, "
            f"slash height={slash_height} "
            f"(ack={ACK_TIMELOCK_BLOCKS}, contested_payout={CONTESTED_PAYOUT_TIMELOCK_BLOCKS})"
        )

        assert payout_height is not None, (
            "deposit-1 never paid out; the attacker gains nothing and F1 is not profitable "
            f"at ack={ACK_TIMELOCK_BLOCKS}, contested_payout={CONTESTED_PAYOUT_TIMELOCK_BLOCKS}"
        )
        assert slash_height is None or payout_height <= slash_height, (
            f"slash confirmed at {slash_height} before the payout at {payout_height}; "
            "the attacker loses its stake without collecting, so the attack does not pay "
            f"at ack={ACK_TIMELOCK_BLOCKS}, contested_payout={CONTESTED_PAYOUT_TIMELOCK_BLOCKS}"
        )

        self.logger.info(
            "Attacker won the race: one sacrificial stake bought one unchallenged deposit"
        )

        # The payout won, but arithmetic says the slash should have become spendable
        # a few blocks earlier (slash = counterproof_1 + nack, payout = contest_2 + ack,
        # and contest_2 is strictly later than counterproof_1). Keep watching to find
        # out whether the slash is merely late or never driven at all.
        self.report_slash_lag(bitcoin_rpc, stake_txid, payout_height)
        return True

    def report_slash_lag(self, bitcoin_rpc, stake_txid: str, payout_height: int) -> None:
        """Log when the slash eventually lands, measured in blocks after the payout."""
        landed: dict[str, int | None] = {"height": None}

        def check():
            if bitcoin_rpc.proxy.gettxout(stake_txid, STAKE_VOUT) is not None:
                return False
            spender = find_utxo_spender_txid(bitcoin_rpc, stake_txid, STAKE_VOUT)
            block_hash = bitcoin_rpc.proxy.getrawtransaction(spender, True).get("blockhash")
            if block_hash is None:
                return False
            landed["height"] = block_height(bitcoin_rpc, block_hash)
            return True

        try:
            wait_until(
                check, timeout=SLASH_LAG_TIMEOUT_SECS, step=1, error_msg="slash never landed"
            )
        except Exception:
            self.logger.info(
                f"SLASH LAG: stake {stake_txid}:{STAKE_VOUT} still unspent "
                f"{SLASH_LAG_TIMEOUT_SECS}s after the payout at height {payout_height} "
                "— slash was due but had not been posted; see analysis/findings.md F1"
            )
            return

        slash_height = landed["height"]
        assert slash_height is not None
        self.logger.info(
            f"SLASH LAG: slash landed at height {slash_height}, "
            f"{slash_height - payout_height} blocks after the payout at {payout_height}"
        )
