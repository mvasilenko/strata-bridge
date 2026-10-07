//! Common types for mosaic client

use bitcoin::XOnlyPublicKey;
pub use bitcoin::secp256k1::schnorr::Signature;
pub use mosaic_common::constants::{
    N_DEPOSIT_INPUT_WIRES, N_SETUP_INPUT_WIRES, N_WITHDRAWAL_INPUT_WIRES,
};
pub use strata_bridge_primitives::types::{DepositIdx, OperatorIdx};

/// A Txn sighash to be signed.
pub type Sighash = [u8; 32];
/// Tweak for schnorr signatures
pub type Tweak = [u8; 32];
/// A Public key for Schnorr signatures.
pub type PubKey = XOnlyPublicKey;

/// The role of the *local* (self) operator in a mosaic operation.
///
/// In [`MosaicClientApi`](crate::api::MosaicClientApi) methods, `Role`
/// always refers to the caller's own role — garbler (watchtower) or
/// evaluator (operator) — never the remote party's role.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Role {
    /// The garbler (watchtower) side of a tableset.
    Garbler,
    /// The evaluator (operator) side of a tableset.
    Evaluator,
}

impl std::fmt::Display for Role {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?}", self)
    }
}

/// Inputs for setup input wires. Corresponds to operator pubkey.
pub type SetupInputs = [u8; N_SETUP_INPUT_WIRES];
/// Inputs for deposit input wires. Corresponds to deposit idx.
pub type DepositInputs = [u8; N_DEPOSIT_INPUT_WIRES];
/// Inputs for withdrawal input wires. Corresponds to counterproof.
pub type WithdrawalInputs = [u8; N_WITHDRAWAL_INPUT_WIRES];

/// Txn sighashes to be used in adaptor signatures.
pub type DepositSighashes = [Sighash; N_DEPOSIT_INPUT_WIRES + N_WITHDRAWAL_INPUT_WIRES];
/// Completed adaptor signatures corresponding to the [`DepositSighashes`].
pub type CompletedSignatures = [Signature; N_DEPOSIT_INPUT_WIRES + N_WITHDRAWAL_INPUT_WIRES];

/// Raw Groth16 proof bytes.
#[derive(Debug, Clone, Copy)]
pub struct G16ProofRaw(pub [u8; N_WITHDRAWAL_INPUT_WIRES]);

/// Whether a tableset can still back a fresh counterproof.
///
/// [`is_setup_available`](crate::api::MosaicClientApi::is_setup_available) answers for one
/// specific game; this is the standing question monitoring needs — is this `(peer, role)` pair
/// armed right now.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TablesetReadiness {
    /// Setup complete and unspent. This pair can produce a counterproof.
    Ready,
    /// Setup still running. Not yet usable.
    PendingSetup,
    /// Currently resolving a contested withdrawal. Cannot back another one.
    InContest,
    /// Already spent on a contested withdrawal. Cannot back another one.
    Consumed,
    /// Aborted on a protocol violation. A new setup is required.
    Aborted,
}

impl TablesetReadiness {
    /// Whether this pair can produce a counterproof right now.
    pub const fn is_ready(self) -> bool {
        matches!(self, Self::Ready)
    }

    /// Stable, low-cardinality label for metrics.
    pub const fn as_label(self) -> &'static str {
        match self {
            Self::Ready => "ready",
            Self::PendingSetup => "pending_setup",
            Self::InContest => "in_contest",
            Self::Consumed => "consumed",
            Self::Aborted => "aborted",
        }
    }

    /// Every variant, so the metric can be emitted one-hot and a state that
    /// has been left shows as 0 rather than as a gap in the series.
    pub const ALL: [Self; 5] = [
        Self::Ready,
        Self::PendingSetup,
        Self::InContest,
        Self::Consumed,
        Self::Aborted,
    ];
}
