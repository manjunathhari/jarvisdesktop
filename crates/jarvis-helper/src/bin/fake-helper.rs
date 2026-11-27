//! A stand-in for the voice helper, for tests. Std only.
//!
//! Flags: `--never-ready` (reads but never answers), `--die-while-speaking` (on the first `say`
//! writes `speaking` on, then exits 1 without writing off), `--never-read` (never reads stdin or
//! answers; sleeps until killed, or two minutes so a failed test cannot leave it behind).

use std::io::{BufRead, Write};

/// The raw (still escaped) string value of `"key":"..."` in `line`.
fn str_field<'a>(line: &'a str, key: &str) -> Option<&'a str> {
    let pat = format!("\"{key}\":\"");
    let start = line.find(&pat)? + pat.len();
    let rest = &line[start..];
    let mut escaped = false;
    for (i, c) in rest.char_indices() {
        match (escaped, c) {
            (true, _) => escaped = false,
            (false, '\\') => escaped = true,
            (false, '"') => return Some(&rest[..i]),
            _ => {}
        }
    }
    None
}

fn num_field(line: &str, key: &str) -> Option<u64> {
    let pat = format!("\"{key}\":");
    let rest = &line[line.find(&pat)? + pat.len()..];
    let digits: String = rest.chars().take_while(char::is_ascii_digit).collect();
    digits.parse().ok()
}

fn out(line: &str) {
    let mut o = std::io::stdout().lock();
    let _ = writeln!(o, "{line}");
    let _ = o.flush();
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let never_ready = args.iter().any(|a| a == "--never-ready");
    let die = args.iter().any(|a| a == "--die-while-speaking");
    eprintln!("fake-helper started");
    if args.iter().any(|a| a == "--never-read") {
        std::thread::sleep(std::time::Duration::from_secs(120));
        return;
    }

    for line in std::io::stdin().lock().lines() {
        let Ok(line) = line else { break };
        if line.starts_with(r#"{"type":"hello""#) && line.contains(r#""protocol":1"#) {
            if !never_ready {
                let voice = str_field(&line, "voice").unwrap_or("");
                out(&format!(
                    r#"{{"type":"ready","protocol":1,"helper_version":"fake","voice":"{voice}"}}"#
                ));
            }
        } else if line.starts_with(r#"{"type":"say""#) {
            let turn = num_field(&line, "turn").unwrap_or(0);
            out(&format!(r#"{{"type":"speaking","on":true,"turn":{turn}}}"#));
            if die {
                std::process::exit(1);
            }
            out(&format!(
                r#"{{"type":"speaking","on":false,"turn":{turn}}}"#
            ));
        } else if line.starts_with(r#"{"type":"shutdown""#) {
            break;
        }
    }
}
