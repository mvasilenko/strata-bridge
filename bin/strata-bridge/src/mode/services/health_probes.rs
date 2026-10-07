//! Periodic bridge component health probes.

use std::{fmt::Display, future::Future, sync::Arc, time::Duration};

use bitcoind_async_client::{Client as BitcoinClient, traits::Reader};
use btc_tracker::tx_driver::TxDriverHealthHandle;
use jsonrpsee::http_client::HttpClient;
use metrics::gauge;
use secret_service_client::SecretServiceClient;
use secret_service_proto::v2::traits::{SchnorrSigner, SecretService};
use strata_asm_rpc::traits::AsmControlApiClient;
use strata_bridge_db::fdb::client::FdbClient;
use strata_bridge_exec::output_handles::NativeWallet;
use strata_bridge_primitives::types::OperatorIdx;
use strata_mosaic_client::MosaicClient;
use strata_mosaic_client_api::{
    MosaicClientApi,
    types::{Role, TablesetReadiness},
};
use strata_p2p::swarm::handle::CommandHandle;
use tokio::{
    sync::RwLock,
    time::{interval, timeout},
};
use tracing::{error, warn};

use crate::{
    constants::DEFAULT_HEALTH_PROBE_TIMEOUT,
    health::{
        COMPONENT_ASM_RPC, COMPONENT_BITCOIN_RPC, COMPONENT_FDB, COMPONENT_MOSAIC,
        COMPONENT_ORCHESTRATOR, COMPONENT_P2P, COMPONENT_S2, COMPONENT_TX_DRIVER, COMPONENT_WALLET,
        HealthRegistry,
    },
    mode::services::mosaic_client::BridgeMosaicIdResolver,
};

type BridgeMosaicClient = MosaicClient<HttpClient, BridgeMosaicIdResolver>;

/// Starts a periodic FoundationDB liveness probe.
pub(in crate::mode) fn spawn_fdb_probe(
    db: Arc<FdbClient>,
    probe_interval: Duration,
    health_registry: HealthRegistry,
) {
    tokio::spawn(async move {
        let mut ticker = interval(nonzero_interval(probe_interval));

        loop {
            ticker.tick().await;
            run_external_probe(
                &health_registry,
                COMPONENT_FDB,
                "fdb",
                db.health_check(),
                "fdb_reachable",
                "fdb_unreachable",
            )
            .await;
        }
    });
}

/// Starts a periodic Bitcoin RPC probe.
pub(in crate::mode) fn spawn_bitcoin_rpc_probe(
    client: BitcoinClient,
    probe_interval: Duration,
    health_registry: HealthRegistry,
) {
    tokio::spawn(async move {
        let mut ticker = interval(nonzero_interval(probe_interval));

        loop {
            ticker.tick().await;
            run_external_probe(
                &health_registry,
                COMPONENT_BITCOIN_RPC,
                "bitcoin_rpc",
                client.get_block_count(),
                "block_count_read",
                "block_count_failed",
            )
            .await;
        }
    });
}

/// Starts a periodic ASM RPC liveness probe.
pub(in crate::mode) fn spawn_asm_rpc_probe(
    client: HttpClient,
    probe_interval: Duration,
    health_registry: HealthRegistry,
) {
    tokio::spawn(async move {
        let mut ticker = interval(nonzero_interval(probe_interval));

        loop {
            ticker.tick().await;
            run_external_probe(
                &health_registry,
                COMPONENT_ASM_RPC,
                "asm_rpc",
                client.get_uptime(),
                "asm_rpc_reachable",
                "asm_rpc_unreachable",
            )
            .await;
        }
    });
}

/// Starts a periodic P2P command/peer connectivity probe.
pub(in crate::mode) fn spawn_p2p_probe(
    command_handle: CommandHandle,
    expected_peer_count: usize,
    probe_interval: Duration,
    health_registry: HealthRegistry,
) {
    tokio::spawn(async move {
        let mut ticker = interval(nonzero_interval(probe_interval));

        loop {
            ticker.tick().await;
            // `get_connected_peers` is internally bounded: it returns an empty set rather than
            // hanging if the swarm task is unresponsive, so it cannot leave the probe stuck.
            let connected_peer_count = command_handle.get_connected_peers(None).await.len();

            if expected_peer_count == 0 {
                health_registry.mark_ok(COMPONENT_P2P, "no_peers_configured");
            } else if connected_peer_count == 0 {
                health_registry.mark_unhealthy(COMPONENT_P2P, "no_connected_peers");
                error!(
                    expected_peer_count,
                    "P2P health probe found no connected peers"
                );
            } else if connected_peer_count < expected_peer_count {
                health_registry.mark_degraded(COMPONENT_P2P, "partial_peer_connectivity");
                warn!(
                    connected_peer_count,
                    expected_peer_count, "P2P health probe found partial peer connectivity"
                );
            } else {
                health_registry.mark_ok(COMPONENT_P2P, "peers_connected");
            }
        }
    });
}

/// Starts a periodic watchtower-capability probe.
///
/// The mosaic probe answers "is the process up". This answers "can this operator still
/// counterproof each peer" — a reachable mosaic whose tableset is consumed reports `ok` and
/// still cannot defend the bridge.
///
/// **Self-reported**, so it is sound for operating this node and unsound as a network safety
/// figure. A trustless ceiling must come from Bitcoin, where a counterproof spends a
/// per-watchtower output of the contest tx.
pub(in crate::mode) fn spawn_watchtower_capability_probe(
    client: Arc<BridgeMosaicClient>,
    peers: Vec<OperatorIdx>,
    probe_interval: Duration,
) {
    tokio::spawn(async move {
        let mut ticker = interval(nonzero_interval(probe_interval));

        loop {
            ticker.tick().await;

            let mut capable_peers = 0u64;
            for &peer in &peers {
                // Garbler is the watchtower side (`counterproof.rs` takes that path), so only it
                // feeds coverage. Evaluator is exported too — same loop, same RPC — so a
                // half-set-up pair is visible.
                for role in [Role::Garbler, Role::Evaluator] {
                    // The client retries internally for longer than the probe interval, which
                    // stalls the loop and freezes every gauge at its last value. A frozen
                    // "ready" is the worst reading this metric can give, so bound each call.
                    let readiness = timeout(
                        DEFAULT_HEALTH_PROBE_TIMEOUT,
                        client.tableset_readiness(peer, role),
                    )
                    .await;

                    match readiness {
                        Ok(Ok(readiness)) => {
                            emit_tableset_metric(peer, role, Some(readiness));
                            if role == Role::Garbler && readiness.is_ready() {
                                capable_peers += 1;
                            }
                        }
                        Ok(Err(err)) => {
                            // Clear every series rather than leave the last good value
                            // standing: unknown must not read as ready.
                            emit_tableset_metric(peer, role, None);
                            warn!(%peer, %role, %err, "watchtower capability probe failed");
                        }
                        Err(_) => {
                            emit_tableset_metric(peer, role, None);
                            warn!(%peer, %role, "watchtower capability probe timed out");
                        }
                    }
                }
            }

            // The bridge's safety margin in one number: how many peers this operator could
            // still produce a counterproof against.
            gauge!("strata_bridge_watchtower_coverage").set(capable_peers as f64);
        }
    });
}

/// Emits the readiness series one-hot. `None` clears every state, so a failed probe reads as
/// "unknown" instead of leaving a stale value.
fn emit_tableset_metric(peer: OperatorIdx, role: Role, readiness: Option<TablesetReadiness>) {
    let peer = peer.to_string();
    let role = role.to_string();
    for state in TablesetReadiness::ALL {
        let value = if readiness == Some(state) { 1.0 } else { 0.0 };
        gauge!(
            "strata_bridge_tableset_readiness",
            "peer" => peer.clone(),
            "role" => role.clone(),
            "state" => state.as_label(),
        )
        .set(value);
    }
    gauge!(
        "strata_bridge_watchtower_capable",
        "peer" => peer,
        "role" => role,
    )
    .set(readiness.is_some_and(TablesetReadiness::is_ready) as u8 as f64);
}

/// Starts a periodic Mosaic RPC probe.
pub(in crate::mode) fn spawn_mosaic_probe(
    client: Arc<BridgeMosaicClient>,
    probe_interval: Duration,
    health_registry: HealthRegistry,
) {
    tokio::spawn(async move {
        let mut ticker = interval(nonzero_interval(probe_interval));

        loop {
            ticker.tick().await;
            run_external_probe(
                &health_registry,
                COMPONENT_MOSAIC,
                "mosaic",
                client.health_check(),
                "rpc_reachable",
                "rpc_unreachable",
            )
            .await;
        }
    });
}

/// Starts a periodic secret-service probe.
pub(in crate::mode) fn spawn_s2_probe(
    client: SecretServiceClient,
    probe_interval: Duration,
    health_registry: HealthRegistry,
) {
    tokio::spawn(async move {
        let mut ticker = interval(nonzero_interval(probe_interval));

        loop {
            ticker.tick().await;
            run_external_probe(
                &health_registry,
                COMPONENT_S2,
                "secret_service",
                client.musig2_signer().pubkey(),
                "s2_read_succeeded",
                "s2_read_failed",
            )
            .await;
        }
    });
}

/// Starts a periodic operator-wallet liveness probe.
///
/// Reads the wallet's local chain tip through a shared read lock — a non-mutating, in-memory
/// check. It deliberately does not call `sync()`: syncing takes the exclusive write lock and
/// drives backend I/O, which would let a slow or stuck backend serialize wallet-dependent
/// duties. The read-lock acquisition is bounded so a stuck writer cannot leave the component
/// reporting a stale `ok` state.
pub(in crate::mode) fn spawn_wallet_probe(
    wallet: Arc<RwLock<NativeWallet>>,
    probe_interval: Duration,
    health_registry: HealthRegistry,
) {
    tokio::spawn(async move {
        let mut ticker = interval(nonzero_interval(probe_interval));

        loop {
            ticker.tick().await;
            match timeout(DEFAULT_HEALTH_PROBE_TIMEOUT, wallet.read()).await {
                Ok(guard) => {
                    let height = guard.local_chain_tip_height();
                    drop(guard);
                    health_registry.mark_ok(COMPONENT_WALLET, format!("synced_to_height_{height}"));
                }
                Err(_) => {
                    health_registry.mark_unhealthy(COMPONENT_WALLET, "probe_timed_out");
                    error!("operator wallet health probe timed out acquiring read lock");
                }
            }
        }
    });
}

/// Starts a periodic transaction-driver liveness probe.
pub(in crate::mode) fn spawn_tx_driver_probe(
    tx_driver_health: TxDriverHealthHandle,
    probe_interval: Duration,
    health_registry: HealthRegistry,
) {
    tokio::spawn(async move {
        let mut ticker = interval(nonzero_interval(probe_interval));

        loop {
            ticker.tick().await;
            if tx_driver_health.is_accepting_jobs() {
                health_registry.mark_ok(COMPONENT_TX_DRIVER, "driver_accepting_jobs");
            } else {
                health_registry.mark_unhealthy(COMPONENT_TX_DRIVER, "driver_not_accepting_jobs");
                error!("transaction driver health probe found driver not accepting jobs");
            }
        }
    });
}

/// Starts a monitor that marks the orchestrator unhealthy when its heartbeat becomes stale.
pub(in crate::mode) fn spawn_orchestrator_stale_monitor(
    stale_after: Duration,
    health_registry: HealthRegistry,
) {
    tokio::spawn(async move {
        let stale_after = nonzero_interval(stale_after);
        let probe_interval = stale_after
            .checked_div(2)
            .filter(|interval| !interval.is_zero())
            .unwrap_or(stale_after);
        let mut ticker = interval(probe_interval);

        loop {
            ticker.tick().await;
            health_registry.mark_unhealthy_if_stale(
                COMPONENT_ORCHESTRATOR,
                stale_after,
                "pipeline_stale",
            );
        }
    });
}

const fn nonzero_interval(interval: Duration) -> Duration {
    if interval.is_zero() {
        Duration::from_secs(1)
    } else {
        interval
    }
}

async fn run_external_probe<T, E, F>(
    health_registry: &HealthRegistry,
    component: &'static str,
    probe_name: &'static str,
    probe: F,
    ok_reason: &'static str,
    error_reason: &'static str,
) where
    E: Display,
    F: Future<Output = Result<T, E>>,
{
    match timeout(DEFAULT_HEALTH_PROBE_TIMEOUT, probe).await {
        Ok(Ok(_)) => health_registry.mark_ok(component, ok_reason),
        Ok(Err(err)) => {
            health_registry.mark_unhealthy(component, error_reason);
            error!(component, probe_name, %err, "health probe failed");
        }
        Err(_) => {
            health_registry.mark_unhealthy(component, "probe_timed_out");
            error!(component, probe_name, "health probe timed out");
        }
    }
}
