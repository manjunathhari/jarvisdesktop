//! `jarvis-cad-runner --listen ADDR --engine PATH --commands FILE`
//!
//! In the `cad` image: `--listen 0.0.0.0:7411 --engine /usr/local/bin/cadcraft-cli
//! --commands /etc/jarvis/cad_commands.txt`.

use std::path::PathBuf;
use std::process::ExitCode;

use jarvis_cad_runner::{load_commands, serve, Config};

const USAGE: &str = "usage: jarvis-cad-runner --listen ADDR --engine PATH --commands FILE";

fn main() -> ExitCode {
    let mut listen = None;
    let mut engine = None;
    let mut commands = None;
    let mut args = std::env::args().skip(1);
    while let Some(flag) = args.next() {
        let slot = match flag.as_str() {
            "--listen" => &mut listen,
            "--engine" => &mut engine,
            "--commands" => &mut commands,
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
    let cfg = Config::new(engine, allowed);
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
    runtime.block_on(async move {
        let listener = match tokio::net::TcpListener::bind(&listen).await {
            Ok(l) => l,
            Err(e) => {
                eprintln!("jarvis-cad-runner: cannot listen on {listen}: {e}");
                return ExitCode::FAILURE;
            }
        };
        eprintln!("jarvis-cad-runner: listening on {listen}, {count} commands allowed");
        serve(listener, cfg).await;
        ExitCode::SUCCESS
    })
}
