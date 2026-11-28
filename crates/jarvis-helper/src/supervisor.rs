//! The voice supervisor: decides which voice fills the app's voice slot (the helper first, the
//! built-in voice as the fallback), restarts the helper once per run, and reports a status.
//! Pure logic: the app starts either voice through [`VoiceOps`].

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

use async_trait::async_trait;
use jarvis_core::speaks::Speaks;

use crate::voice_config::VoiceEngine;

/// Where the app keeps whichever voice is speaking now. Empty while none is ready.
pub type VoiceSlot = Arc<Mutex<Option<Arc<dyn Speaks>>>>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Status {
    /// The helper is being downloaded or installed, percent.
    HelperDownloading(u8),
    HelperStarting,
    /// The built-in voice is downloading, percent.
    Downloading(u8),
    /// The built-in voice is loading.
    Loading,
    Ready,
    /// The helper could not run; the built-in voice is in use (or being started).
    HelperUnavailable,
    /// No voice at all.
    Unavailable,
}

impl Status {
    /// The `state` string sent to the window.
    pub fn state(&self) -> &'static str {
        match self {
            Status::HelperDownloading(_) => "helper_downloading",
            Status::HelperStarting => "helper_starting",
            Status::Downloading(_) => "downloading",
            Status::Loading => "loading",
            Status::Ready => "ready",
            Status::HelperUnavailable => "helper_unavailable",
            Status::Unavailable => "unavailable",
        }
    }

    /// The percent for the two downloading states, 0 otherwise.
    pub fn percent(&self) -> u8 {
        match self {
            Status::HelperDownloading(p) | Status::Downloading(p) => *p,
            _ => 0,
        }
    }
}

pub type StatusFn = Arc<dyn Fn(Status) + Send + Sync>;

#[async_trait]
pub trait VoiceOps: Send + Sync {
    /// Install and start the helper and wait until it is ready. Reports `HelperDownloading` /
    /// `HelperStarting` through `status`.
    async fn start_helper(&self, status: &StatusFn) -> Result<Arc<dyn Speaks>, String>;
    /// Download if needed and load the built-in voice. Reports `Downloading` / `Loading` through
    /// `status`.
    async fn start_builtin(&self, status: &StatusFn) -> Result<Arc<dyn Speaks>, String>;
    /// End a running helper, if any (app exit, or before falling back). Safe to call when none
    /// is running.
    fn stop_helper(&self);
}

/// Take a std lock, treating a poisoned one as usable.
fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|p| p.into_inner())
}

/// Records every status, then passes it on.
struct Reporter {
    last: Mutex<Status>,
    set: StatusFn,
}

impl Reporter {
    fn report(&self, s: Status) {
        *lock(&self.last) = s;
        (self.set)(s);
    }
}

/// State only touched while the serialising async lock is held.
struct Inner {
    engine: Option<VoiceEngine>,
    restart_used: bool,
    helper_is_voice: bool,
}

pub struct Supervisor {
    ops: Arc<dyn VoiceOps>,
    reporter: Arc<Reporter>,
    /// The reporter as the `StatusFn` handed to the ops.
    status_fn: StatusFn,
    slot: VoiceSlot,
    shut: AtomicBool,
    inner: tokio::sync::Mutex<Inner>,
}

impl Supervisor {
    pub fn new(ops: Arc<dyn VoiceOps>, set_status: StatusFn, slot: VoiceSlot) -> Arc<Self> {
        let reporter = Arc::new(Reporter {
            last: Mutex::new(Status::Loading),
            set: set_status,
        });
        let r = reporter.clone();
        let status_fn: StatusFn = Arc::new(move |s| r.report(s));
        Arc::new(Self {
            ops,
            reporter,
            status_fn,
            slot,
            shut: AtomicBool::new(false),
            inner: tokio::sync::Mutex::new(Inner {
                engine: None,
                restart_used: false,
                helper_is_voice: false,
            }),
        })
    }

    /// Choose and start a voice. Called once at start-up.
    pub async fn start(&self, engine: VoiceEngine) {
        let mut inner = self.inner.lock().await;
        if self.is_shut() {
            return;
        }
        inner.engine = Some(engine);
        self.begin(&mut inner, engine).await;
    }

    /// The running helper's pipe closed (it exited or was killed).
    pub async fn helper_closed(&self) {
        let mut inner = self.inner.lock().await;
        if self.is_shut() || !inner.helper_is_voice {
            return;
        }
        inner.helper_is_voice = false;
        self.set_slot(None);
        if inner.restart_used {
            tracing::info!("the voice helper closed again; using the built-in voice");
            self.fallback(&mut inner).await;
            return;
        }
        inner.restart_used = true;
        tracing::info!("the voice helper closed; restarting it once");
        self.report(Status::HelperStarting);
        self.try_helper(&mut inner).await;
    }

    /// The user pressed "Try again".
    pub async fn retry(&self) {
        let mut inner = self.inner.lock().await;
        if self.is_shut() {
            return;
        }
        if !matches!(
            self.status(),
            Status::Unavailable | Status::HelperUnavailable
        ) {
            return;
        }
        let Some(engine) = inner.engine else {
            return;
        };
        tracing::info!("retrying the voice from the top");
        inner.helper_is_voice = false;
        self.set_slot(None);
        self.ops.stop_helper();
        self.begin(&mut inner, engine).await;
    }

    /// The app is exiting.
    pub fn shutdown(&self) {
        self.shut.store(true, Ordering::SeqCst);
        *lock(&self.slot) = None;
        self.ops.stop_helper();
    }

    /// The most recent status reported (so a window that opens late can ask).
    pub fn status(&self) -> Status {
        *lock(&self.reporter.last)
    }

    fn is_shut(&self) -> bool {
        self.shut.load(Ordering::SeqCst)
    }

    fn report(&self, s: Status) {
        self.reporter.report(s);
    }

    /// Fill (or empty) the slot. After shutdown the slot stays empty.
    fn set_slot(&self, voice: Option<Arc<dyn Speaks>>) {
        let voice = if self.is_shut() { None } else { voice };
        *lock(&self.slot) = voice;
    }

    async fn begin(&self, inner: &mut Inner, engine: VoiceEngine) {
        match engine {
            VoiceEngine::Builtin => match self.ops.start_builtin(&self.status_fn).await {
                Ok(v) => {
                    self.set_slot(Some(v));
                    self.report(Status::Ready);
                    tracing::info!("voice ready: built-in");
                }
                Err(e) => {
                    tracing::info!("the built-in voice could not start: {e}");
                    self.set_slot(None);
                    self.report(Status::Unavailable);
                }
            },
            VoiceEngine::Pipecat => self.try_helper(inner).await,
        }
    }

    /// Start the helper; on success it is the voice, on failure fall back.
    async fn try_helper(&self, inner: &mut Inner) {
        match self.ops.start_helper(&self.status_fn).await {
            Ok(v) => {
                if self.is_shut() {
                    self.ops.stop_helper();
                    return;
                }
                self.set_slot(Some(v));
                inner.helper_is_voice = true;
                self.report(Status::Ready);
                tracing::info!("voice ready: helper");
            }
            Err(e) => {
                tracing::info!("the voice helper could not run: {e}");
                self.fallback(inner).await;
            }
        }
    }

    /// The helper cannot be used: say so, stop it, start the built-in voice.
    async fn fallback(&self, inner: &mut Inner) {
        inner.helper_is_voice = false;
        self.report(Status::HelperUnavailable);
        self.ops.stop_helper();
        match self.ops.start_builtin(&self.status_fn).await {
            Ok(v) => {
                self.set_slot(Some(v));
                self.report(Status::HelperUnavailable);
                tracing::info!("voice ready: built-in (helper unavailable)");
            }
            Err(e) => {
                tracing::info!("the built-in voice could not start either: {e}");
                self.set_slot(None);
                self.report(Status::Unavailable);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Duration;

    use tokio::sync::Notify;
    use tokio::time::timeout;

    use super::*;

    const WAIT: Duration = Duration::from_secs(5);

    struct FakeVoice {
        id: u32,
    }

    impl Speaks for FakeVoice {
        fn speak(&self, _text: &str) {}
        fn stop(&self) {}
        fn is_speaking(&self) -> bool {
            self.id == u32::MAX
        }
    }

    fn voice(id: u32) -> Arc<FakeVoice> {
        Arc::new(FakeVoice { id })
    }

    #[derive(Debug, Clone, PartialEq, Eq)]
    enum Ev {
        Status(Status),
        Call(&'static str),
    }

    type Log = Arc<Mutex<Vec<Ev>>>;

    struct Step {
        reports: Vec<Status>,
        result: Result<Arc<FakeVoice>, String>,
        gate: Option<Arc<Notify>>,
        entered: Option<Arc<Notify>>,
    }

    fn ok(v: &Arc<FakeVoice>) -> Step {
        Step {
            reports: vec![],
            result: Ok(v.clone()),
            gate: None,
            entered: None,
        }
    }

    fn err(msg: &str) -> Step {
        Step {
            reports: vec![],
            result: Err(msg.to_string()),
            gate: None,
            entered: None,
        }
    }

    struct FakeOps {
        helper: Mutex<VecDeque<Step>>,
        builtin: Mutex<VecDeque<Step>>,
        helper_calls: AtomicUsize,
        builtin_calls: AtomicUsize,
        stops: AtomicUsize,
        log: Log,
    }

    impl FakeOps {
        fn total_calls(&self) -> usize {
            self.helper_calls.load(Ordering::SeqCst)
                + self.builtin_calls.load(Ordering::SeqCst)
                + self.stops.load(Ordering::SeqCst)
        }
        fn helper_calls(&self) -> usize {
            self.helper_calls.load(Ordering::SeqCst)
        }
        fn builtin_calls(&self) -> usize {
            self.builtin_calls.load(Ordering::SeqCst)
        }
        fn stops(&self) -> usize {
            self.stops.load(Ordering::SeqCst)
        }
    }

    async fn run(
        queue: &Mutex<VecDeque<Step>>,
        log: &Log,
        status: &StatusFn,
    ) -> Result<Arc<dyn Speaks>, String> {
        let step = queue
            .lock()
            .unwrap()
            .pop_front()
            .expect("the test scripted too few results");
        if let Some(e) = &step.entered {
            e.notify_one();
        }
        for s in &step.reports {
            status(*s);
        }
        if let Some(g) = &step.gate {
            g.notified().await;
        }
        let _ = log;
        step.result.map(|v| v as Arc<dyn Speaks>)
    }

    #[async_trait]
    impl VoiceOps for FakeOps {
        async fn start_helper(&self, status: &StatusFn) -> Result<Arc<dyn Speaks>, String> {
            self.helper_calls.fetch_add(1, Ordering::SeqCst);
            self.log.lock().unwrap().push(Ev::Call("start_helper"));
            run(&self.helper, &self.log, status).await
        }
        async fn start_builtin(&self, status: &StatusFn) -> Result<Arc<dyn Speaks>, String> {
            self.builtin_calls.fetch_add(1, Ordering::SeqCst);
            self.log.lock().unwrap().push(Ev::Call("start_builtin"));
            run(&self.builtin, &self.log, status).await
        }
        fn stop_helper(&self) {
            self.stops.fetch_add(1, Ordering::SeqCst);
            self.log.lock().unwrap().push(Ev::Call("stop_helper"));
        }
    }

    struct Rig {
        ops: Arc<FakeOps>,
        sup: Arc<Supervisor>,
        slot: VoiceSlot,
        log: Log,
    }

    fn rig(helper: Vec<Step>, builtin: Vec<Step>) -> Rig {
        let log: Log = Arc::new(Mutex::new(Vec::new()));
        let ops = Arc::new(FakeOps {
            helper: Mutex::new(helper.into()),
            builtin: Mutex::new(builtin.into()),
            helper_calls: AtomicUsize::new(0),
            builtin_calls: AtomicUsize::new(0),
            stops: AtomicUsize::new(0),
            log: log.clone(),
        });
        let slot: VoiceSlot = Arc::new(Mutex::new(None));
        let l = log.clone();
        let set: StatusFn = Arc::new(move |s| l.lock().unwrap().push(Ev::Status(s)));
        let sup = Supervisor::new(ops.clone(), set, slot.clone());
        Rig {
            ops,
            sup,
            slot,
            log,
        }
    }

    impl Rig {
        fn statuses(&self) -> Vec<Status> {
            self.log
                .lock()
                .unwrap()
                .iter()
                .filter_map(|e| match e {
                    Ev::Status(s) => Some(*s),
                    Ev::Call(_) => None,
                })
                .collect()
        }
        fn holds(&self, v: &Arc<FakeVoice>) -> bool {
            match &*self.slot.lock().unwrap() {
                Some(s) => std::ptr::eq(Arc::as_ptr(s) as *const (), Arc::as_ptr(v) as *const ()),
                None => false,
            }
        }
        fn empty(&self) -> bool {
            self.slot.lock().unwrap().is_none()
        }
    }

    #[tokio::test]
    async fn helper_ok_fills_the_slot_and_reports_ready() {
        let h = voice(1);
        let r = rig(vec![ok(&h)], vec![]);
        r.sup.start(VoiceEngine::Pipecat).await;
        assert_eq!(r.statuses().last(), Some(&Status::Ready));
        assert!(r.holds(&h));
        assert_eq!(r.ops.builtin_calls(), 0);
    }

    #[tokio::test]
    async fn helper_failing_falls_back_to_builtin_and_says_so() {
        let b = voice(2);
        let mut step = ok(&b);
        step.reports = vec![Status::Loading];
        let r = rig(vec![err("no python")], vec![step]);
        r.sup.start(VoiceEngine::Pipecat).await;
        let log = r.log.lock().unwrap().clone();
        let unavailable = log
            .iter()
            .position(|e| *e == Ev::Status(Status::HelperUnavailable))
            .expect("HelperUnavailable reported");
        let builtin = log
            .iter()
            .position(|e| *e == Ev::Call("start_builtin"))
            .expect("built-in started");
        assert!(unavailable < builtin);
        drop(log);
        let st = r.statuses();
        assert!(st.contains(&Status::Loading));
        assert_eq!(st.last(), Some(&Status::HelperUnavailable));
        assert!(r.holds(&b));
        assert_eq!(r.ops.stops(), 1);
        assert_eq!(r.sup.status(), Status::HelperUnavailable);
    }

    #[tokio::test]
    async fn a_helper_that_closes_is_restarted_once() {
        let (h1, h2) = (voice(1), voice(2));
        let r = rig(vec![ok(&h1), ok(&h2)], vec![]);
        r.sup.start(VoiceEngine::Pipecat).await;
        r.sup.helper_closed().await;
        let st = r.statuses();
        assert!(st.contains(&Status::HelperStarting));
        assert_eq!(st.last(), Some(&Status::Ready));
        assert!(r.holds(&h2));
        assert_eq!(r.ops.helper_calls(), 2);
        assert_eq!(r.ops.builtin_calls(), 0);
    }

    #[tokio::test]
    async fn the_slot_is_emptied_at_once_when_the_helper_closes() {
        let (h1, h2) = (voice(1), voice(2));
        let gate = Arc::new(Notify::new());
        let entered = Arc::new(Notify::new());
        let mut second = ok(&h2);
        second.gate = Some(gate.clone());
        second.entered = Some(entered.clone());
        let r = rig(vec![ok(&h1), second], vec![]);
        r.sup.start(VoiceEngine::Pipecat).await;
        let sup = r.sup.clone();
        let t = tokio::spawn(async move { sup.helper_closed().await });
        timeout(WAIT, entered.notified()).await.unwrap();
        assert!(r.empty());
        gate.notify_one();
        timeout(WAIT, t).await.unwrap().unwrap();
        assert!(r.holds(&h2));
    }

    #[tokio::test]
    async fn a_second_close_goes_to_builtin_for_good() {
        let (h1, h2, b) = (voice(1), voice(2), voice(3));
        let r = rig(vec![ok(&h1), ok(&h2)], vec![ok(&b)]);
        r.sup.start(VoiceEngine::Pipecat).await;
        r.sup.helper_closed().await;
        r.sup.helper_closed().await;
        assert_eq!(r.ops.helper_calls(), 2);
        assert!(r.holds(&b));
        assert_eq!(r.sup.status(), Status::HelperUnavailable);
        let before = r.ops.total_calls();
        r.sup.helper_closed().await;
        assert_eq!(r.ops.total_calls(), before);
        assert!(r.holds(&b));
    }

    #[tokio::test]
    async fn a_failed_restart_falls_back() {
        let (h1, b) = (voice(1), voice(3));
        let r = rig(vec![ok(&h1), err("died again")], vec![ok(&b)]);
        r.sup.start(VoiceEngine::Pipecat).await;
        r.sup.helper_closed().await;
        assert!(r.holds(&b));
        assert_eq!(r.sup.status(), Status::HelperUnavailable);
        assert_eq!(r.statuses().last(), Some(&Status::HelperUnavailable));
    }

    #[tokio::test]
    async fn both_failing_reports_unavailable_and_retry_starts_over() {
        let h = voice(1);
        let r = rig(vec![err("helper"), ok(&h)], vec![err("builtin")]);
        r.sup.start(VoiceEngine::Pipecat).await;
        assert_eq!(r.sup.status(), Status::Unavailable);
        assert!(r.empty());
        r.sup.retry().await;
        assert!(r.holds(&h));
        assert_eq!(r.sup.status(), Status::Ready);
    }

    #[tokio::test]
    async fn retry_does_nothing_when_a_voice_is_ready() {
        let h = voice(1);
        let r = rig(vec![ok(&h)], vec![]);
        r.sup.start(VoiceEngine::Pipecat).await;
        let before = r.ops.total_calls();
        r.sup.retry().await;
        assert_eq!(r.ops.total_calls(), before);
        assert!(r.holds(&h));
    }

    #[tokio::test]
    async fn retry_before_start_does_nothing() {
        let r = rig(vec![], vec![]);
        r.sup.retry().await;
        assert_eq!(r.ops.total_calls(), 0);
    }

    #[tokio::test]
    async fn retry_from_helper_unavailable_tries_the_helper_again() {
        let (h, b) = (voice(1), voice(2));
        let r = rig(vec![err("nope"), ok(&h)], vec![ok(&b)]);
        r.sup.start(VoiceEngine::Pipecat).await;
        assert!(r.holds(&b));
        r.sup.retry().await;
        assert!(r.holds(&h));
        assert_eq!(r.sup.status(), Status::Ready);
    }

    #[tokio::test]
    async fn retry_does_not_refill_the_restart_budget() {
        let (h1, h2, h3, b) = (voice(1), voice(2), voice(3), voice(4));
        let r = rig(vec![ok(&h1), ok(&h2), ok(&h3)], vec![ok(&b), ok(&voice(5))]);
        r.sup.start(VoiceEngine::Pipecat).await;
        r.sup.helper_closed().await; // restart used
        r.sup.helper_closed().await; // fallback
        assert!(r.holds(&b));
        r.sup.retry().await; // helper h3
        assert!(r.holds(&h3));
        assert_eq!(r.ops.helper_calls(), 3);
        r.sup.helper_closed().await; // straight to built-in
        assert_eq!(r.ops.helper_calls(), 3);
        assert_eq!(r.sup.status(), Status::HelperUnavailable);
        assert_eq!(r.ops.builtin_calls(), 2);
    }

    #[tokio::test]
    async fn builtin_engine_never_starts_the_helper() {
        let b = voice(1);
        let r = rig(vec![], vec![ok(&b)]);
        r.sup.start(VoiceEngine::Builtin).await;
        assert_eq!(r.sup.status(), Status::Ready);
        assert!(r.holds(&b));
        assert_eq!(r.ops.helper_calls(), 0);

        let r = rig(vec![], vec![err("no model")]);
        r.sup.start(VoiceEngine::Builtin).await;
        assert_eq!(r.sup.status(), Status::Unavailable);
        assert!(r.empty());
        assert_eq!(r.ops.helper_calls(), 0);
    }

    #[tokio::test]
    async fn a_close_before_the_helper_is_the_voice_is_ignored() {
        let b = voice(2);
        let r = rig(vec![err("x")], vec![ok(&b)]);
        r.sup.helper_closed().await;
        assert_eq!(r.ops.total_calls(), 0);
        assert!(r.empty());
        r.sup.start(VoiceEngine::Pipecat).await;
        let before = r.ops.total_calls();
        r.sup.helper_closed().await;
        assert_eq!(r.ops.total_calls(), before);
        assert!(r.holds(&b));
        assert_eq!(r.sup.status(), Status::HelperUnavailable);
    }

    #[tokio::test]
    async fn a_close_during_start_waits_its_turn() {
        let (h1, h2) = (voice(1), voice(2));
        let gate = Arc::new(Notify::new());
        let entered = Arc::new(Notify::new());
        let mut first = ok(&h1);
        first.gate = Some(gate.clone());
        first.entered = Some(entered.clone());
        let r = rig(vec![first, ok(&h2)], vec![]);
        let s1 = r.sup.clone();
        let start = tokio::spawn(async move { s1.start(VoiceEngine::Pipecat).await });
        timeout(WAIT, entered.notified()).await.unwrap();
        let s2 = r.sup.clone();
        let close = tokio::spawn(async move { s2.helper_closed().await });
        for _ in 0..20 {
            tokio::task::yield_now().await;
        }
        assert_eq!(r.ops.helper_calls(), 1, "the close must wait for the start");
        gate.notify_one();
        timeout(WAIT, start).await.unwrap().unwrap();
        timeout(WAIT, close).await.unwrap().unwrap();
        assert_eq!(r.ops.helper_calls(), 2);
        assert!(r.holds(&h2));
        assert_eq!(r.sup.status(), Status::Ready);
        assert_eq!(r.ops.builtin_calls(), 0);
    }

    #[tokio::test]
    async fn status_is_recorded_including_what_the_ops_report() {
        let h = voice(1);
        let mut step = ok(&h);
        step.reports = vec![Status::HelperDownloading(40), Status::HelperStarting];
        let r = rig(vec![step], vec![]);
        assert_eq!(r.sup.status(), Status::Loading);
        r.sup.start(VoiceEngine::Pipecat).await;
        assert_eq!(
            r.statuses(),
            vec![
                Status::HelperDownloading(40),
                Status::HelperStarting,
                Status::Ready
            ]
        );
        assert_eq!(r.sup.status(), Status::Ready);
    }

    #[tokio::test]
    async fn shutdown_empties_the_slot_and_stops_the_helper_and_later_calls_do_nothing() {
        let (h, b) = (voice(1), voice(2));
        let r = rig(vec![ok(&h), ok(&voice(3))], vec![ok(&b)]);
        r.sup.start(VoiceEngine::Pipecat).await;
        r.sup.shutdown();
        assert!(r.empty());
        assert_eq!(r.ops.stops(), 1);
        let before = r.ops.total_calls();
        r.sup.start(VoiceEngine::Pipecat).await;
        r.sup.helper_closed().await;
        r.sup.retry().await;
        assert_eq!(r.ops.total_calls(), before);
        assert!(r.empty());
    }

    #[tokio::test]
    async fn shutdown_during_start_leaves_the_slot_empty() {
        let h = voice(1);
        let gate = Arc::new(Notify::new());
        let entered = Arc::new(Notify::new());
        let mut step = ok(&h);
        step.gate = Some(gate.clone());
        step.entered = Some(entered.clone());
        let r = rig(vec![step], vec![]);
        let s = r.sup.clone();
        let t = tokio::spawn(async move { s.start(VoiceEngine::Pipecat).await });
        timeout(WAIT, entered.notified()).await.unwrap();
        r.sup.shutdown();
        gate.notify_one();
        timeout(WAIT, t).await.unwrap().unwrap();
        assert!(r.empty());
    }

    #[test]
    fn state_strings_and_percent() {
        assert_eq!(Status::HelperDownloading(7).state(), "helper_downloading");
        assert_eq!(Status::HelperStarting.state(), "helper_starting");
        assert_eq!(Status::Downloading(9).state(), "downloading");
        assert_eq!(Status::Loading.state(), "loading");
        assert_eq!(Status::Ready.state(), "ready");
        assert_eq!(Status::HelperUnavailable.state(), "helper_unavailable");
        assert_eq!(Status::Unavailable.state(), "unavailable");
        assert_eq!(Status::HelperDownloading(7).percent(), 7);
        assert_eq!(Status::Downloading(9).percent(), 9);
        assert_eq!(Status::Ready.percent(), 0);
        assert_eq!(Status::HelperStarting.percent(), 0);
    }
}
