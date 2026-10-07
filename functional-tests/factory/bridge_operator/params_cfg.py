import os
from dataclasses import dataclass

from constants import ASM_MAGIC_BYTES


class ProofPredicate:
    """Identifiers for the `strata_predicate::PredicateTypeId` variants used as the
    bridge proof predicate."""

    ALWAYS_ACCEPT = "AlwaysAccept"
    NEVER_ACCEPT = "NeverAccept"


@dataclass
class ScheduledOperator:
    index: int
    covenant_key: str
    p2p_key: str
    payout_descriptor: str
    activation_height: int
    deactivation_height: int | None = None


@dataclass
class Admin:
    pubkeys: list[str]
    threshold: int


@dataclass
class Keys:
    admin: Admin
    operators: list[ScheduledOperator]


@dataclass
class BridgeProtocolParams:
    bury_depth: int = 2
    magic_bytes: str = ASM_MAGIC_BYTES
    # Must agree with the ASM params or the bridge node exits with
    # "bridge/ASM params mismatch". When sharing an L1 with a real OL these are
    # driven from the OL's asm-params.json (see ASM_DENOMINATION/ASM_OPERATOR_FEE
    # in analysis/tools/devnet.sh), so both sides derive from one source.
    deposit_amount: int = int(os.environ.get("ASM_DENOMINATION", 1_000_000_000))
    stake_amount: int = 100_000_000
    operator_fee: int = int(os.environ.get("ASM_OPERATOR_FEE", 10_000_000))
    sweep_fee_rate: int = 10
    recovery_delay: int = 1_008
    contest_timelock: int = 45
    proof_timelock: int = 15
    ack_timelock: int = 35
    nack_timelock: int = 30
    contested_payout_timelock: int = 60
    unstaking_timelock: int = 75
    bridge_proof_predicate: str | None = None
    counterproof_predicate: str | None = None


@dataclass
class BridgeOperatorParams:
    network: str
    genesis_height: int
    keys: Keys
    protocol: BridgeProtocolParams
