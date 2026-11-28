//! `jarvis-cad-runner [--container] --listen ADDR --engine PATH --commands FILE [--work-root DIR]`
//!
//! In the `cad` image: `--container --listen 0.0.0.0:7411 --engine /usr/local/bin/cadcraft-cli
//! --commands /etc/jarvis/cad_commands.txt --work-root /tmp`. `--container` is refused unless
//! the runner is pid 1 or a child of pid 1.

use std::process::ExitCode;

#[cfg(unix)]
const USAGE: &str = "usage: jarvis-cad-runner [--container] --listen ADDR --engine PATH --commands FILE [--work-root DIR]";

#[cfg(not(unix))]
fn main() -> ExitCode {
    eprintln!("jarvis-cad-runner: unsupported on this platform (it runs in a Linux container)");
    ExitCode::FAILURE
}

#[cfg(unix)]
fn main() -> ExitCode {
    use std::path::{Path, PathBuf};

    use jarvis_cad_runner::{load_commands, serve, Config};

    let mut container = false;
    let mut listen = None;
    let mut engine = None;
    let mut commands = None;
    let mut work_root = None;
    let mut args = std::env::args().skip(1);
    while let Some(flag) = args.next() {
        let slot = match flag.as_str() {
            "--container" => {
                container = true;
                continue;
            }
            "--listen" => &mut listen,
            "--engine" => &mut engine,
            "--commands" => &mut commands,
            "--work-root" => &mut work_root,
            _ => {
                eprintln!("{USAGE}");
                return ExitCode::from(2);
            }
        };
        *slot = args.next();
    }
    let (Some(listen), Some(engine), Some(commands)) = (listen, engine, commands) else {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    };
    let allowed = match load_commands(&PathBuf::from(&commands)) {
        Ok(ids) if !ids.is_empty() => ids,
        Ok(_) => {
            eprintln!("jarvis-cad-runner: the commands file lists no commands");
            return ExitCode::from(2);
        }
        Err(e) => {
            eprintln!("jarvis-cad-runner: cannot read the commands file: {e}");
            return ExitCode::from(2);
        }
    };
    // Absolute: the engine starts in its own new directory, where a relative path would not
    // name it.
    let engine = match std::fs::canonicalize(&engine) {
        Ok(path) => path,
        Err(e) => {
            eprintln!("jarvis-cad-runner: no engine at {engine}: {e}");
            return ExitCode::from(2);
        }
    };
    let count = allowed.len();
    let mut cfg = Config::new(engine, allowed);
    if let Some(root) = &work_root {
        cfg = cfg.work_root(PathBuf::from(root));
    }
    if container {
        if work_root
            .as_deref()
            .is_some_and(|r| Path::new(r) != Path::new("/tmp"))
        {
            eprintln!("jarvis-cad-runner: in container mode the work root is /tmp");
            return ExitCode::from(2);
        }
        cfg = match cfg.container_mode() {
            Ok(cfg) => cfg,
            Err(e) => {
                eprintln!("jarvis-cad-runner: {e}");
                return ExitCode::from(2);
            }
        };
    }
    if !cfg.work_root_value().is_dir() {
        eprintln!(
            "jarvis-cad-runner: the work root {} is not a directory",
            cfg.work_root_value().display()
        );
        return ExitCode::from(2);
    }
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("jarvis-cad-runner: no runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    let code = runtime.block_on(async move {
        use tokio::signal::unix::{signal, SignalKind};
        // As pid 1 a signal without a handler is ignored: without this, `docker stop` would
        // wait out its grace period.
        let mut term = match signal(SignalKind::terminate()) {
            Ok(term) => term,
            Err(e) => {
                eprintln!("jarvis-cad-runner: no SIGTERM handler: {e}");
                return ExitCode::FAILURE;
            }
        };
        let listener = match tokio::net::TcpListener::bind(&listen).await {
            Ok(l) => l,
            Err(e) => {
                eprintln!("jarvis-cad-runner: cannot listen on {listen}: {e}");
                return ExitCode::FAILURE;
            }
        };
        let mode = if cfg.is_container() {
            "container mode"
        } else {
            "process-group mode"
        };
        eprintln!("jarvis-cad-runner: listening on {listen}, {count} commands allowed, {mode}");
        let on_term = cfg.clone();
        tokio::select! {
            () = serve(listener, cfg) => ExitCode::SUCCESS,
            _ = term.recv() => {
                eprintln!("jarvis-cad-runner: SIGTERM, stopping");
                jarvis_cad_runner::replay::shutdown(&on_term);
                ExitCode::SUCCESS
            }
        }
    });
    // Do not wait for replay tasks still running: their engine group is already killed.
    runtime.shutdown_timeout(std::time::Duration::from_millis(500));
    code
}
