//! The gateway connection driver (spec §13): a WebSocket client around `Session`.
//!
//! - Connects to `ws://<gateway>/v1/node` (the tailnet carries the encryption).
//! - Reconnects after any close except 4409, with backoff of 1, 2, 4, 8, 16, then 30 s
//!   (R12-29). After 4409, or a `Stop` that asks for it, it stops and reports
//!   `NeedsReenrol`; the key and state stay on disk until a new enrolment replaces them.
//! - Treats no frame for `heartbeat_timeout` (3 missed 30 s pings: 95 s) as a dead link.
//! - Never waits on the app: verified frames go out on an unbounded channel, so `ping` is
//!   always answered promptly (the gateway times sends out at 10 s, R12-10).
//! - Enrols with a code: makes a fresh key in memory, and saves the key and the new state
//!   only after `welcome` passed every check (§7.2 steps 2 and 5).

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use ed25519_dalek::SigningKey;
use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use tokio::sync::{mpsc, oneshot, watch};
use tokio::time::{sleep, timeout, Instant};
use tokio_tungstenite::tungstenite::Message;
use zeroize::Zeroizing;

use crate::code::EnrolmentCode;
use crate::keystore::KeyStore;
use crate::session::{after_close, backoff, AfterClose, Output, Session, Start, Stop};
use crate::state::{self, NodeState};

/// The gateway's WebSocket path.
pub const ROUTE: &str = "/v1/node";
/// The node's state file inside the data folder.
pub const STATE_FILE: &str = "node-state.json";
/// 3 missed 30 s heartbeats, plus a little slack.
pub const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(95);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Debug, Clone)]
pub struct ClientConfig {
    /// The gateway's `host:port` (its tailnet address); never committed to the repo.
    pub gateway: String,
    /// The app's data folder: holds `node-state.json`.
    pub data_dir: PathBuf,
    pub heartbeat_timeout: Duration,
}

impl ClientConfig {
    pub fn new(gateway: impl Into<String>, data_dir: impl Into<PathBuf>) -> Self {
        Self {
            gateway: gateway.into(),
            data_dir: data_dir.into(),
            heartbeat_timeout: HEARTBEAT_TIMEOUT,
        }
    }
}

/// What the link is doing, for the UI.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LinkStatus {
    /// No enrolled identity yet: waiting for an enrolment code.
    NotEnrolled,
    Connecting,
    Connected,
    /// The server asked for re-enrolment (4409, or its keys changed): waiting for a new code.
    NeedsReenrol,
    /// The state file or key store can't be read; nothing connects until a person looks.
    Failed(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("not connected to the Jarvis server")]
pub struct NotConnected;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum EnrolError {
    #[error("that is not a valid enrolment code")]
    BadCode,
    #[error("this device is already enrolled")]
    AlreadyEnrolled,
    #[error("enrolment failed: {0}")]
    Refused(&'static str),
    #[error("couldn't reach the Jarvis server")]
    Unreachable,
    #[error("enrolment succeeded but saving failed: {0}")]
    Save(String),
    #[error("the connection driver has stopped")]
    Stopped,
}

enum Cmd {
    Send {
        kind: String,
        payload: Value,
        reply: oneshot::Sender<Result<(), NotConnected>>,
    },
    Enrol {
        code: String,
        reply: oneshot::Sender<Result<(), EnrolError>>,
    },
}

/// The handle the app holds. Cheap to clone.
#[derive(Clone)]
pub struct Client {
    cmds: mpsc::UnboundedSender<Cmd>,
    status: watch::Receiver<LinkStatus>,
}

impl Client {
    /// Send a signed message now; fails at once if the link isn't up.
    pub async fn send(&self, kind: &str, payload: Value) -> Result<(), NotConnected> {
        let (reply, rx) = oneshot::channel();
        self.cmds
            .send(Cmd::Send {
                kind: kind.to_owned(),
                payload,
                reply,
            })
            .map_err(|_| NotConnected)?;
        rx.await.unwrap_or(Err(NotConnected))
    }

    /// Enrol with a code; resolves once the server's welcome has been verified and saved.
    pub async fn enrol(&self, code: &str) -> Result<(), EnrolError> {
        let (reply, rx) = oneshot::channel();
        self.cmds
            .send(Cmd::Enrol {
                code: code.to_owned(),
                reply,
            })
            .map_err(|_| EnrolError::Stopped)?;
        rx.await.unwrap_or(Err(EnrolError::Stopped))
    }

    pub fn status(&self) -> watch::Receiver<LinkStatus> {
        self.status.clone()
    }
}

/// Start the driver on the current tokio runtime. `frames` gets `Ready`, `Frame` and
/// `ClockOff` outputs, in order.
pub fn spawn(
    cfg: ClientConfig,
    keystore: Arc<dyn KeyStore>,
    frames: mpsc::UnboundedSender<Output>,
) -> Client {
    let (cmds, cmd_rx) = mpsc::unbounded_channel();
    let (status_tx, status) = watch::channel(LinkStatus::Connecting);
    tokio::spawn(run(cfg, keystore, frames, cmd_rx, status_tx));
    Client { cmds, status }
}

/// A new key from the operating system's random source.
fn new_key() -> SigningKey {
    let mut seed = Zeroizing::new([0u8; 32]);
    getrandom::fill(seed.as_mut()).expect("the OS random source is available");
    SigningKey::from_bytes(&seed)
}

/// An enrolment in progress.
struct Enrolling {
    code: EnrolmentCode,
    key: SigningKey,
    reply: Option<oneshot::Sender<Result<(), EnrolError>>>,
}

impl Enrolling {
    fn answer(&mut self, result: Result<(), EnrolError>) {
        if let Some(reply) = self.reply.take() {
            let _ = reply.send(result);
        }
    }
}

enum End {
    /// The gateway closed (or the link died) with this code.
    Closed(u16),
    Stopped(Stop),
    /// The app dropped its handle.
    Shutdown,
}

/// The local code for a link that went silent or broke without a close frame.
const LINK_LOST: u16 = 1006;

async fn run(
    cfg: ClientConfig,
    keystore: Arc<dyn KeyStore>,
    frames: mpsc::UnboundedSender<Output>,
    mut cmds: mpsc::UnboundedReceiver<Cmd>,
    status: watch::Sender<LinkStatus>,
) {
    let state_path = cfg.data_dir.join(STATE_FILE);
    let url = format!("ws://{}{ROUTE}", cfg.gateway);
    let mut attempt = 0u32;
    let mut reenrol = false;
    let mut enrolling: Option<Enrolling> = None;

    loop {
        // What to connect as: an enrolment in progress, or the saved identity.
        let start = match &enrolling {
            Some(e) => Some((
                e.key.clone(),
                NodeState::default(),
                Start::Enrol(e.code.clone()),
            )),
            None if reenrol => None,
            None => match (state::load(&state_path), keystore.load()) {
                (Ok(Some(st)), Ok(Some(key))) if st.node_id.is_some() => {
                    Some((key, st, Start::Hello { resume: vec![] }))
                }
                (Err(e), _) => {
                    let _ = status.send(LinkStatus::Failed(e.to_string()));
                    None
                }
                (_, Err(e)) => {
                    let _ = status.send(LinkStatus::Failed(e.to_string()));
                    None
                }
                _ => None,
            },
        };
        let Some((key, st, start)) = start else {
            if !matches!(*status.borrow(), LinkStatus::Failed(_)) {
                let _ = status.send(if reenrol {
                    LinkStatus::NeedsReenrol
                } else {
                    LinkStatus::NotEnrolled
                });
            }
            // Wait for an enrolment code; everything else is refused meanwhile.
            match cmds.recv().await {
                None => return,
                Some(Cmd::Send { reply, .. }) => {
                    let _ = reply.send(Err(NotConnected));
                }
                Some(Cmd::Enrol { code, reply }) => match EnrolmentCode::parse(&code) {
                    Err(_) => {
                        let _ = reply.send(Err(EnrolError::BadCode));
                    }
                    Ok(code) => {
                        // A new enrolment replaces the old identity, re-enrol request included.
                        reenrol = false;
                        enrolling = Some(Enrolling {
                            code,
                            key: new_key(),
                            reply: Some(reply),
                        });
                        attempt = 0;
                    }
                },
            }
            continue;
        };

        let _ = status.send(LinkStatus::Connecting);
        let end = match timeout(
            CONNECT_TIMEOUT,
            tokio_tungstenite::connect_async(url.as_str()),
        )
        .await
        {
            Ok(Ok((ws, _))) => {
                let session = Session::new(key.clone(), st, start);
                connection(
                    ws,
                    session,
                    &key,
                    &cfg,
                    &state_path,
                    keystore.as_ref(),
                    &frames,
                    &mut cmds,
                    &status,
                    &mut enrolling,
                    &mut attempt,
                )
                .await
            }
            _ => End::Closed(LINK_LOST),
        };

        match end {
            End::Shutdown => return,
            End::Closed(code) => {
                if let Some(mut e) = enrolling.take() {
                    // The device stores nothing; a new attempt needs the code again.
                    e.answer(Err(if code == LINK_LOST {
                        EnrolError::Unreachable
                    } else {
                        EnrolError::Refused("closed")
                    }));
                    continue;
                }
                if after_close(code) == AfterClose::Reenrol {
                    reenrol = true;
                    continue;
                }
            }
            End::Stopped(stop) => {
                if let Some(mut e) = enrolling.take() {
                    e.answer(Err(EnrolError::Refused(stop.reason)));
                    continue;
                }
                if stop.reenrol {
                    reenrol = true;
                    continue;
                }
            }
        }
        // Reconnect after a pause; app commands are still answered while waiting.
        let wake = Instant::now() + backoff(attempt);
        attempt = attempt.saturating_add(1);
        loop {
            tokio::select! {
                _ = tokio::time::sleep_until(wake) => break,
                cmd = cmds.recv() => match cmd {
                    None => return,
                    Some(Cmd::Send { reply, .. }) => { let _ = reply.send(Err(NotConnected)); }
                    Some(Cmd::Enrol { reply, .. }) => { let _ = reply.send(Err(EnrolError::AlreadyEnrolled)); }
                },
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn connection<S>(
    ws: S,
    mut session: Session,
    key: &SigningKey,
    cfg: &ClientConfig,
    state_path: &std::path::Path,
    keystore: &dyn KeyStore,
    frames: &mpsc::UnboundedSender<Output>,
    cmds: &mut mpsc::UnboundedReceiver<Cmd>,
    status: &watch::Sender<LinkStatus>,
    enrolling: &mut Option<Enrolling>,
    attempt: &mut u32,
) -> End
where
    S: futures_util::Stream<Item = Result<Message, tokio_tungstenite::tungstenite::Error>>
        + futures_util::Sink<Message>
        + Unpin,
{
    let (mut sink, mut stream) = ws.split();
    let mut deadline = Instant::now() + cfg.heartbeat_timeout;
    loop {
        tokio::select! {
            msg = stream.next() => {
                deadline = Instant::now() + cfg.heartbeat_timeout;
                let text = match msg {
                    Some(Ok(Message::Text(t))) => t,
                    Some(Ok(Message::Close(frame))) => {
                        return End::Closed(frame.map_or(1005, |f| u16::from(f.code)));
                    }
                    Some(Ok(Message::Ping(_) | Message::Pong(_) | Message::Frame(_))) => continue,
                    Some(Ok(Message::Binary(_))) => {
                        let _ = sink.close().await;
                        return End::Stopped(Stop { reason: "malformed", reenrol: false });
                    }
                    Some(Err(_)) | None => return End::Closed(LINK_LOST),
                };
                let now = chrono::Utc::now().fixed_offset();
                let outputs = match session.on_frame(text.as_str(), now) {
                    Ok(outputs) => outputs,
                    Err(stop) => {
                        let _ = sink.close().await;
                        return End::Stopped(stop);
                    }
                };
                for output in outputs {
                    match output {
                        Output::Send(frame) => {
                            if sink.send(Message::text(frame)).await.is_err() {
                                return End::Closed(LINK_LOST);
                            }
                        }
                        Output::Ready { enrolled, .. } => {
                            if enrolled {
                                // The key first: a state without its key is never saved.
                                let saved = keystore
                                    .save(key)
                                    .map_err(|e| e.to_string())
                                    .and_then(|()| {
                                        state::save(state_path, session.state())
                                            .map_err(|e| e.to_string())
                                    });
                                let mut e = enrolling.take().expect("an enrolment was running");
                                if let Err(err) = saved {
                                    e.answer(Err(EnrolError::Save(err)));
                                    let _ = sink.close().await;
                                    return End::Stopped(Stop { reason: "save_failed", reenrol: false });
                                }
                                e.answer(Ok(()));
                            }
                            *attempt = 0;
                            let _ = status.send(LinkStatus::Connected);
                            let _ = frames.send(output);
                        }
                        other => {
                            let _ = frames.send(other);
                        }
                    }
                }
            }
            cmd = cmds.recv() => match cmd {
                None => {
                    let _ = sink.close().await;
                    return End::Shutdown;
                }
                Some(Cmd::Send { kind, payload, reply }) => {
                    let now = chrono::Utc::now().fixed_offset();
                    match session.send(&kind, payload, now) {
                        Ok(frame) => {
                            let ok = sink.send(Message::text(frame)).await.is_ok();
                            let _ = reply.send(if ok { Ok(()) } else { Err(NotConnected) });
                            if !ok {
                                return End::Closed(LINK_LOST);
                            }
                        }
                        Err(_) => { let _ = reply.send(Err(NotConnected)); }
                    }
                }
                Some(Cmd::Enrol { reply, .. }) => {
                    let _ = reply.send(Err(EnrolError::AlreadyEnrolled));
                }
            },
            _ = tokio::time::sleep_until(deadline) => {
                // No frame (not even a ping) for the whole heartbeat window: the link is dead.
                let _ = sink.close().await;
                return End::Closed(LINK_LOST);
            }
        }
    }
}

/// How long to wait before a test or caller gives up on a status (helper for the app).
pub async fn wait_for(
    status: &mut watch::Receiver<LinkStatus>,
    want: impl Fn(&LinkStatus) -> bool,
    within: Duration,
) -> bool {
    timeout(within, async {
        loop {
            if want(&status.borrow_and_update()) {
                return;
            }
            if status.changed().await.is_err() {
                // The driver ended; nothing will change any more.
                sleep(within).await;
            }
        }
    })
    .await
    .is_ok()
}
