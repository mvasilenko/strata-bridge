import logging

from constants import BRIDGE_NETWORK_SIZE, DT_DEPOSIT_VOUT, STAKE_VOUT
from utils.deposit import wait_until_utxo_spent
from utils.network import wait_until_p2p_connected
from utils.stake import wait_until_all_operators_staked
from utils.utils import find_utxo_spender_txid, wait_for_tx_confirmation, wait_until


def get_bridge_nodes_and_rpcs(ctx, num_operators=BRIDGE_NETWORK_SIZE, stake_timeout=300):
    """Get bridge nodes and their RPC clients for the network."""
    bridge_nodes = [ctx.get_service(f"bridge_node_{idx}") for idx in range(num_operators)]
    bridge_rpcs = [bridge_node.create_rpc() for bridge_node in bridge_nodes]

    # Verify operator connectivity
    wait_until_p2p_connected(bridge_rpcs)

    bitcoind_service = ctx.get_service("bitcoin")
    bitcoin_rpc = bitcoind_service.create_rpc()

    logging.info("Waiting for all operators to complete staking")
    wait_until_all_operators_staked(
        bridge_rpcs[0], bitcoin_rpc, expected_operator_count=num_operators, timeout=stake_timeout
    )

    return bridge_nodes, bridge_rpcs


def block_height(bitcoin_rpc, block_hash: str) -> int:
    """Height of the block with the given hash."""
    return int(bitcoin_rpc.proxy.getblock(block_hash)["height"])


def confirmed_spender_height(bitcoin_rpc, txid: str, vout: int) -> int | None:
    """Height at which `txid:vout` was spent, or None while unspent or unconfirmed."""
    if bitcoin_rpc.proxy.gettxout(txid, vout) is not None:
        return None
    spender = find_utxo_spender_txid(bitcoin_rpc, txid, vout)
    block_hash = bitcoin_rpc.proxy.getrawtransaction(spender, True).get("blockhash")
    return None if block_hash is None else block_height(bitcoin_rpc, block_hash)


def race_payout_against_slash(
    bitcoin_rpc, deposit_txid: str, stake_txid: str, timeout: int
) -> tuple[int | None, int | None]:
    """Wait until the deposit payout or the operator's stake slash confirms.

    Returns `(payout_height, slash_height)`, either of which may be None if that
    transaction had not confirmed by the time the other did.
    """
    seen: dict[str, int | None] = {"payout": None, "slash": None}

    def check():
        seen["payout"] = confirmed_spender_height(bitcoin_rpc, deposit_txid, DT_DEPOSIT_VOUT)
        seen["slash"] = confirmed_spender_height(bitcoin_rpc, stake_txid, STAKE_VOUT)
        return seen["payout"] is not None or seen["slash"] is not None

    wait_until(
        check,
        timeout=timeout,
        step=1,
        error_msg="neither the deposit payout nor the stake slash confirmed",
    )
    return seen["payout"], seen["slash"]


def post_faulty_claim_contest_and_proof(
    bitcoin_rpc,
    dev_cli,
    deposit_idx: int,
    operator_idx: int,
    bridge_node_url: str,
    seed: str,
) -> str:
    """Drive one faulty-claim round to a confirmed contest plus bridge proof.

    Returns the contest txid.

    `tests/contest/fn_skip_counterproof_after_gc_consumed.py` carries its own copy
    of this flow as a private method; collapsing the two is a separate cleanup.
    """
    claim_txid = dev_cli.send_claim(
        deposit_idx=deposit_idx,
        operator_idx=operator_idx,
        bridge_node_url=bridge_node_url,
        seed=seed,
    )
    logging.info(f"Faulty claim from op-{operator_idx} for deposit {deposit_idx}: {claim_txid}")
    wait_for_tx_confirmation(bitcoin_rpc, claim_txid, timeout=300)

    wait_until_utxo_spent(bitcoin_rpc, claim_txid, vout=0, timeout=300)
    contest_txid = find_utxo_spender_txid(bitcoin_rpc, claim_txid, 0)
    wait_for_tx_confirmation(bitcoin_rpc, contest_txid, timeout=300)
    logging.info(f"Claim {claim_txid} contested by {contest_txid}")

    bridge_proof_txid = dev_cli.send_bridge_proof(
        deposit_idx=deposit_idx,
        operator_idx=operator_idx,
        bridge_node_url=bridge_node_url,
        seed=seed,
    )
    wait_for_tx_confirmation(bitcoin_rpc, bridge_proof_txid, timeout=300)
    logging.info(f"Faulty bridge proof {bridge_proof_txid} confirmed")

    return contest_txid
