//! Getting the voice model onto the PC: a one-time download of the official
//! sherpa-onnx Kokoro English pack, unpacked into the app's data folder.

use async_trait::async_trait;
use std::path::{Path, PathBuf};

pub const KOKORO_DIR: &str = "kokoro-en-v0_19";
pub const KOKORO_URL: &str =
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-en-v0_19.tar.bz2";
/// Speaker id of the British male voice "George" in this pack.
pub const GEORGE: i32 = 9;
const COMPLETE_MARKER: &str = ".complete";
const REQUIRED: &[&str] = &["model.onnx", "voices.bin", "tokens.txt", "espeak-ng-data"];

/// Paths the engine needs.
#[derive(Debug, Clone, PartialEq)]
pub struct KokoroFiles {
    pub model: PathBuf,
    pub voices: PathBuf,
    pub tokens: PathBuf,
    pub data_dir: PathBuf,
}

impl KokoroFiles {
    pub fn in_dir(dir: &Path) -> Self {
        Self {
            model: dir.join("model.onnx"),
            voices: dir.join("voices.bin"),
            tokens: dir.join("tokens.txt"),
            data_dir: dir.join("espeak-ng-data"),
        }
    }
}

pub type Progress<'a> = &'a (dyn Fn(u64, Option<u64>) + Send + Sync);

#[async_trait]
pub trait Download: Send + Sync {
    async fn download(&self, url: &str, dest: &Path, progress: Progress<'_>) -> Result<(), String>;
}

/// Is the voice already installed?
pub fn is_ready(models_dir: &Path) -> bool {
    models_dir.join(KOKORO_DIR).join(COMPLETE_MARKER).is_file()
}

/// Make sure the voice is installed; download and unpack it if not.
/// `percent` is called with 0–100 while downloading.
pub async fn ensure(
    models_dir: &Path,
    url: &str,
    dl: &dyn Download,
    percent: &(dyn Fn(u8) + Send + Sync),
) -> Result<KokoroFiles, String> {
    let dir = ensure_archive(
        models_dir, KOKORO_DIR, url, KOKORO_DIR, REQUIRED, dl, percent,
    )
    .await?;
    Ok(KokoroFiles::in_dir(&dir))
}

/// Make sure a model shipped as a `.tar.bz2` archive is installed in `models_dir/dir_name`:
/// download it, unpack it, check that `inner` (the archive's top folder) holds every
/// `required` entry, and mark it complete. Returns the folder.
pub async fn ensure_archive(
    models_dir: &Path,
    dir_name: &str,
    url: &str,
    inner: &str,
    required: &[&str],
    dl: &dyn Download,
    percent: &(dyn Fn(u8) + Send + Sync),
) -> Result<PathBuf, String> {
    install_archive(
        models_dir, dir_name, url, inner, required, None, dl, percent,
    )
    .await
}

/// Like [`ensure_archive`], but for an archive the app will RUN: once the download finishes,
/// and before anything is unpacked, the file's SHA-256 is compared with `sha256_hex` (64 hex
/// characters, either case). On a mismatch the file is deleted, nothing is unpacked or marked
/// complete, and the error is `the {dir_name} download failed its integrity check`; the next
/// call downloads again. A malformed `sha256_hex` is rejected before any download.
#[allow(clippy::too_many_arguments)]
pub async fn ensure_archive_checked(
    models_dir: &Path,
    dir_name: &str,
    url: &str,
    inner: &str,
    required: &[&str],
    sha256_hex: &str,
    dl: &dyn Download,
    percent: &(dyn Fn(u8) + Send + Sync),
) -> Result<PathBuf, String> {
    check_sha256_format(sha256_hex)?;
    install_archive(
        models_dir,
        dir_name,
        url,
        inner,
        required,
        Some(sha256_hex),
        dl,
        percent,
    )
    .await
}

/// A pin must be exactly 64 hexadecimal characters (either case).
fn check_sha256_format(sha256_hex: &str) -> Result<(), String> {
    if sha256_hex.len() != 64 || !sha256_hex.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("the expected SHA-256 must be exactly 64 hexadecimal characters".to_string());
    }
    Ok(())
}

/// SHA-256 of a file as lower-case hex, read in chunks (never the whole file in memory).
fn file_sha256_hex(path: &Path) -> Result<String, String> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    let mut file = std::fs::File::open(path).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 64 * 1024];
    loop {
        let n = file.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}

#[allow(clippy::too_many_arguments)]
async fn install_archive(
    models_dir: &Path,
    dir_name: &str,
    url: &str,
    inner: &str,
    required: &[&str],
    expected_sha256: Option<&str>,
    dl: &dyn Download,
    percent: &(dyn Fn(u8) + Send + Sync),
) -> Result<PathBuf, String> {
    let dir = models_dir.join(dir_name);
    if dir.join(COMPLETE_MARKER).is_file() {
        return Ok(dir);
    }
    std::fs::create_dir_all(models_dir).map_err(|e| e.to_string())?;
    let archive = models_dir.join(format!("{dir_name}.tar.bz2.part"));
    let report = |done: u64, total: Option<u64>| {
        if let Some(t) = total.filter(|t| *t > 0) {
            percent(((done.min(t) * 100) / t) as u8);
        }
    };
    let downloaded = dl.download(url, &archive, &report).await;
    if let Err(e) = downloaded {
        let _ = std::fs::remove_file(&archive);
        return Err(e);
    }

    if let Some(expected) = expected_sha256 {
        let path = archive.clone();
        let actual = tokio::task::spawn_blocking(move || file_sha256_hex(&path))
            .await
            .map_err(|e| e.to_string())
            .and_then(|r| r);
        let matches = matches!(&actual, Ok(a) if a.eq_ignore_ascii_case(expected));
        if !matches {
            let _ = std::fs::remove_file(&archive);
            return Err(format!(
                "the {dir_name} download failed its integrity check"
            ));
        }
    }

    let staging = models_dir.join(format!(".unpacking-{dir_name}"));
    let (arch, stage) = (archive.clone(), staging.clone());
    let unpacked = tokio::task::spawn_blocking(move || unpack(&arch, &stage))
        .await
        .map_err(|e| e.to_string())
        .and_then(|r| r);
    let _ = std::fs::remove_file(&archive);
    let result = unpacked.and_then(|()| {
        let src = staging.join(inner);
        for name in required {
            if !src.join(name).exists() {
                return Err(format!("the {dir_name} download is missing {name}"));
            }
        }
        if dir.exists() {
            std::fs::remove_dir_all(&dir).map_err(|e| e.to_string())?;
        }
        std::fs::rename(&src, &dir).map_err(|e| e.to_string())?;
        std::fs::write(dir.join(COMPLETE_MARKER), b"ok").map_err(|e| e.to_string())
    });
    let _ = std::fs::remove_dir_all(&staging);
    result.map(|()| dir)
}

fn unpack(archive: &Path, into: &Path) -> Result<(), String> {
    let _ = std::fs::remove_dir_all(into);
    std::fs::create_dir_all(into).map_err(|e| e.to_string())?;
    let file = std::fs::File::open(archive).map_err(|e| e.to_string())?;
    let bz = bzip2::read::BzDecoder::new(std::io::BufReader::new(file));
    // `unpack` refuses paths that escape the target folder.
    tar::Archive::new(bz)
        .unpack(into)
        .map_err(|e| format!("couldn't unpack {}: {e}", archive.display()))
}

/// One file of a model fetched individually (e.g. from Hugging Face).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ModelFile {
    pub url: &'static str,
    pub name: &'static str,
}

/// Make sure every file is in `models_dir/dir_name`; download any set that
/// isn't complete. `percent` runs 0–100 across all files.
pub async fn ensure_files(
    models_dir: &Path,
    dir_name: &str,
    files: &[ModelFile],
    dl: &dyn Download,
    percent: &(dyn Fn(u8) + Send + Sync),
) -> Result<PathBuf, String> {
    let files: Vec<(&str, &str, Option<&str>)> =
        files.iter().map(|f| (f.url, f.name, None)).collect();
    install_files(models_dir, dir_name, &files, dl, percent).await
}

/// One file fetched individually whose SHA-256 is pinned.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CheckedFile {
    pub url: &'static str,
    pub name: &'static str,
    pub sha256: &'static str,
}

/// Like [`ensure_files`], but for files the app will LOAD: each file is downloaded to
/// `<name>.part`, its SHA-256 compared with its pin (64 hex characters, either case), and only
/// on a match renamed into place. On a mismatch the `.part` is deleted, nothing is marked
/// complete, files verified earlier stay, and the error is
/// `the {dir_name} download failed its integrity check`; the next call fetches again. Every
/// pin is validated before anything is downloaded. A complete folder is returned as is.
pub async fn ensure_files_checked(
    models_dir: &Path,
    dir_name: &str,
    files: &[CheckedFile],
    dl: &dyn Download,
    percent: &(dyn Fn(u8) + Send + Sync),
) -> Result<PathBuf, String> {
    for f in files {
        check_sha256_format(f.sha256)?;
    }
    let files: Vec<(&str, &str, Option<&str>)> = files
        .iter()
        .map(|f| (f.url, f.name, Some(f.sha256)))
        .collect();
    install_files(models_dir, dir_name, &files, dl, percent).await
}

async fn install_files(
    models_dir: &Path,
    dir_name: &str,
    files: &[(&str, &str, Option<&str>)],
    dl: &dyn Download,
    percent: &(dyn Fn(u8) + Send + Sync),
) -> Result<PathBuf, String> {
    let dir = models_dir.join(dir_name);
    if dir.join(COMPLETE_MARKER).is_file() {
        return Ok(dir);
    }
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let n = files.len().max(1) as u64;
    let last = std::sync::atomic::AtomicU8::new(0);
    for (i, (url, name, expected_sha256)) in files.iter().enumerate() {
        let target = dir.join(name);
        if target.is_file() {
            continue; // finished (and verified, if pinned) on an earlier attempt
        }
        let part = dir.join(format!("{name}.part"));
        let report = |done: u64, total: Option<u64>| {
            if let Some(t) = total.filter(|t| *t > 0) {
                let p = ((i as u64 * 100 + done.min(t) * 100 / t) / n) as u8;
                if p > last.load(std::sync::atomic::Ordering::Relaxed) {
                    last.store(p, std::sync::atomic::Ordering::Relaxed);
                    percent(p);
                }
            }
        };
        if let Err(e) = dl.download(url, &part, &report).await {
            let _ = std::fs::remove_file(&part);
            return Err(e);
        }
        if let Some(expected) = expected_sha256 {
            let path = part.clone();
            let actual = tokio::task::spawn_blocking(move || file_sha256_hex(&path))
                .await
                .map_err(|e| e.to_string())
                .and_then(|r| r);
            let matches = matches!(&actual, Ok(a) if a.eq_ignore_ascii_case(expected));
            if !matches {
                let _ = std::fs::remove_file(&part);
                return Err(format!(
                    "the {dir_name} download failed its integrity check"
                ));
            }
        }
        std::fs::rename(&part, &target).map_err(|e| e.to_string())?;
    }
    std::fs::write(dir.join(COMPLETE_MARKER), b"ok").map_err(|e| e.to_string())?;
    if last.load(std::sync::atomic::Ordering::Relaxed) < 100 {
        percent(100);
    }
    Ok(dir)
}

pub struct HttpDownload {
    client: reqwest::Client,
}

impl Default for HttpDownload {
    fn default() -> Self {
        Self {
            client: reqwest::Client::builder()
                .user_agent("JARVIS-Desktop")
                .build()
                .expect("http client"),
        }
    }
}

#[async_trait]
impl Download for HttpDownload {
    async fn download(&self, url: &str, dest: &Path, progress: Progress<'_>) -> Result<(), String> {
        use futures::StreamExt;
        use tokio::io::AsyncWriteExt;
        let resp = self
            .client
            .get(url)
            .send()
            .await
            .map_err(|e| e.to_string())?;
        if !resp.status().is_success() {
            return Err(format!("download failed: HTTP {}", resp.status()));
        }
        let total = resp.content_length();
        let mut file = tokio::fs::File::create(dest)
            .await
            .map_err(|e| e.to_string())?;
        let mut done = 0u64;
        let mut stream = resp.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|e| e.to_string())?;
            file.write_all(&chunk).await.map_err(|e| e.to_string())?;
            done += chunk.len() as u64;
            progress(done, total);
        }
        file.flush().await.map_err(|e| e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    /// A tiny stand-in for the real archive, built in memory.
    fn archive(with: &[&str]) -> Vec<u8> {
        let mut tar = tar::Builder::new(Vec::new());
        for name in with {
            let path = format!("{KOKORO_DIR}/{name}");
            if *name == "espeak-ng-data" {
                let mut h = tar::Header::new_gnu();
                h.set_entry_type(tar::EntryType::Directory);
                h.set_size(0);
                h.set_mode(0o755);
                tar.append_data(&mut h, format!("{path}/"), std::io::empty())
                    .unwrap();
                let body = b"dict";
                let mut h = tar::Header::new_gnu();
                h.set_size(body.len() as u64);
                h.set_mode(0o644);
                tar.append_data(&mut h, format!("{path}/en_dict"), &body[..])
                    .unwrap();
            } else {
                let body = name.as_bytes();
                let mut h = tar::Header::new_gnu();
                h.set_size(body.len() as u64);
                h.set_mode(0o644);
                tar.append_data(&mut h, path, body).unwrap();
            }
        }
        let tar_bytes = tar.into_inner().unwrap();
        let mut enc = bzip2::write::BzEncoder::new(Vec::new(), bzip2::Compression::fast());
        std::io::Write::write_all(&mut enc, &tar_bytes).unwrap();
        enc.finish().unwrap()
    }

    struct Fake {
        bytes: Option<Vec<u8>>,
        calls: AtomicUsize,
    }
    #[async_trait]
    impl Download for Fake {
        async fn download(
            &self,
            _: &str,
            dest: &Path,
            progress: Progress<'_>,
        ) -> Result<(), String> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let bytes = self.bytes.clone().ok_or("no internet")?;
            let half = bytes.len() / 2;
            progress(half as u64, Some(bytes.len() as u64));
            std::fs::write(dest, &bytes).map_err(|e| e.to_string())?;
            progress(bytes.len() as u64, Some(bytes.len() as u64));
            Ok(())
        }
    }

    fn fake(bytes: Option<Vec<u8>>) -> Fake {
        Fake {
            bytes,
            calls: AtomicUsize::new(0),
        }
    }

    #[tokio::test]
    async fn downloads_unpacks_and_marks_the_voice_ready() {
        let tmp = tempfile::tempdir().unwrap();
        let dl = fake(Some(archive(REQUIRED)));
        let seen = Arc::new(Mutex::new(Vec::new()));
        let s = seen.clone();
        let files = ensure(tmp.path(), "u", &dl, &move |p| s.lock().unwrap().push(p))
            .await
            .unwrap();

        assert!(files.model.is_file());
        assert!(files.data_dir.join("en_dict").is_file());
        assert!(is_ready(tmp.path()));
        assert_eq!(seen.lock().unwrap().last(), Some(&100));
        assert!(seen.lock().unwrap().contains(&50));
        let leftovers: Vec<_> = std::fs::read_dir(tmp.path())
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .filter(|n| n != KOKORO_DIR)
            .collect();
        assert!(
            leftovers.is_empty(),
            "archive and temp folders cleaned up: {leftovers:?}"
        );
    }

    #[tokio::test]
    async fn a_second_start_does_not_download_again() {
        let tmp = tempfile::tempdir().unwrap();
        let dl = fake(Some(archive(REQUIRED)));
        ensure(tmp.path(), "u", &dl, &|_| {}).await.unwrap();
        ensure(tmp.path(), "u", &dl, &|_| {}).await.unwrap();
        assert_eq!(dl.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn an_incomplete_archive_is_rejected_and_not_marked_ready() {
        let tmp = tempfile::tempdir().unwrap();
        let dl = fake(Some(archive(&["model.onnx", "tokens.txt"])));
        let err = ensure(tmp.path(), "u", &dl, &|_| {}).await.unwrap_err();
        assert!(err.contains("voices.bin"), "{err}");
        assert!(!is_ready(tmp.path()));
    }

    #[tokio::test]
    async fn no_internet_is_an_error_not_a_crash() {
        let tmp = tempfile::tempdir().unwrap();
        let err = ensure(tmp.path(), "u", &fake(None), &|_| {})
            .await
            .unwrap_err();
        assert!(err.contains("no internet"));
        assert!(!is_ready(tmp.path()));
    }

    // ── individual files ─────────────────────────────────────────

    const FILES: &[ModelFile] = &[
        ModelFile {
            url: "https://x/encoder",
            name: "encoder.onnx",
        },
        ModelFile {
            url: "https://x/decoder",
            name: "decoder.onnx",
        },
    ];

    /// Writes the URL itself as the file's content; fails for listed URLs.
    struct UrlEcho {
        fail: Vec<&'static str>,
        calls: AtomicUsize,
    }
    #[async_trait]
    impl Download for UrlEcho {
        async fn download(
            &self,
            url: &str,
            dest: &Path,
            progress: Progress<'_>,
        ) -> Result<(), String> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if self.fail.contains(&url) {
                return Err(format!("couldn't fetch {url}"));
            }
            progress(1, Some(2));
            std::fs::write(dest, url).map_err(|e| e.to_string())?;
            progress(2, Some(2));
            Ok(())
        }
    }

    #[tokio::test]
    async fn fetches_each_file_and_reports_overall_progress() {
        let tmp = tempfile::tempdir().unwrap();
        let dl = UrlEcho {
            fail: vec![],
            calls: AtomicUsize::new(0),
        };
        let seen = Arc::new(Mutex::new(Vec::new()));
        let s = seen.clone();
        let dir = ensure_files(tmp.path(), "whisper", FILES, &dl, &move |p| {
            s.lock().unwrap().push(p)
        })
        .await
        .unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.join("decoder.onnx")).unwrap(),
            "https://x/decoder"
        );
        let seen = seen.lock().unwrap();
        assert_eq!(seen.last(), Some(&100));
        assert!(
            seen.contains(&25) && seen.contains(&50) && seen.contains(&75),
            "{seen:?}"
        );
        assert!(
            seen.windows(2).all(|w| w[0] <= w[1]),
            "never goes backwards: {seen:?}"
        );
    }

    #[tokio::test]
    async fn complete_file_sets_are_not_fetched_again() {
        let tmp = tempfile::tempdir().unwrap();
        let dl = UrlEcho {
            fail: vec![],
            calls: AtomicUsize::new(0),
        };
        ensure_files(tmp.path(), "whisper", FILES, &dl, &|_| {})
            .await
            .unwrap();
        ensure_files(tmp.path(), "whisper", FILES, &dl, &|_| {})
            .await
            .unwrap();
        assert_eq!(dl.calls.load(Ordering::SeqCst), 2, "two files, once each");
    }

    #[tokio::test]
    async fn a_failed_file_leaves_the_set_incomplete_and_retries_next_time() {
        let tmp = tempfile::tempdir().unwrap();
        let bad = UrlEcho {
            fail: vec!["https://x/decoder"],
            calls: AtomicUsize::new(0),
        };
        assert!(ensure_files(tmp.path(), "whisper", FILES, &bad, &|_| {})
            .await
            .is_err());
        assert!(!tmp.path().join("whisper").join("decoder.onnx").exists());
        assert!(!tmp.path().join("whisper").join(".complete").exists());
        let good = UrlEcho {
            fail: vec![],
            calls: AtomicUsize::new(0),
        };
        let dir = ensure_files(tmp.path(), "whisper", FILES, &good, &|_| {})
            .await
            .unwrap();
        assert!(dir.join("decoder.onnx").is_file());
    }

    #[tokio::test]
    async fn a_corrupt_download_is_an_error() {
        let tmp = tempfile::tempdir().unwrap();
        let err = ensure(tmp.path(), "u", &fake(Some(b"not bzip2".to_vec())), &|_| {}).await;
        assert!(err.is_err());
        assert!(!is_ready(tmp.path()));
    }

    // ── the integrity check ──────────────────────────────────────

    fn sha256_hex(bytes: &[u8]) -> String {
        use sha2::{Digest, Sha256};
        Sha256::digest(bytes)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect()
    }

    async fn checked(dir: &Path, hash: &str, dl: &dyn Download) -> Result<PathBuf, String> {
        ensure_archive_checked(
            dir,
            KOKORO_DIR,
            "u",
            KOKORO_DIR,
            REQUIRED,
            hash,
            dl,
            &|_| {},
        )
        .await
    }

    fn entries(dir: &Path) -> Vec<String> {
        std::fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect()
    }

    #[tokio::test]
    async fn a_matching_hash_installs_and_marks_complete() {
        let tmp = tempfile::tempdir().unwrap();
        let bytes = archive(REQUIRED);
        let dl = fake(Some(bytes.clone()));
        let dir = checked(tmp.path(), &sha256_hex(&bytes), &dl).await.unwrap();
        assert!(dir.join("model.onnx").is_file());
        assert!(dir.join(COMPLETE_MARKER).is_file());
        assert_eq!(entries(tmp.path()), vec![KOKORO_DIR.to_string()]);
    }

    #[tokio::test]
    async fn a_wrong_hash_installs_nothing_and_leaves_no_file() {
        let tmp = tempfile::tempdir().unwrap();
        let dl = fake(Some(archive(REQUIRED)));
        let err = checked(tmp.path(), &"0".repeat(64), &dl).await.unwrap_err();
        assert_eq!(
            err,
            format!("the {KOKORO_DIR} download failed its integrity check")
        );
        assert!(entries(tmp.path()).is_empty(), "{:?}", entries(tmp.path()));
        assert!(!tmp.path().join(KOKORO_DIR).exists());
    }

    #[tokio::test]
    async fn a_failed_install_is_retried_on_the_next_call() {
        let tmp = tempfile::tempdir().unwrap();
        let bytes = archive(REQUIRED);
        let dl = fake(Some(bytes.clone()));
        assert!(checked(tmp.path(), &"0".repeat(64), &dl).await.is_err());
        let dir = checked(tmp.path(), &sha256_hex(&bytes), &dl).await.unwrap();
        assert!(dir.join(COMPLETE_MARKER).is_file());
        assert_eq!(dl.calls.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn a_hash_that_is_not_64_hex_characters_is_rejected_without_downloading() {
        let tmp = tempfile::tempdir().unwrap();
        let dl = fake(Some(archive(REQUIRED)));
        let too_short = "abc123";
        let not_hex = format!("{}g", "a".repeat(63));
        for bad in [too_short, not_hex.as_str(), ""] {
            let err = checked(tmp.path(), bad, &dl).await.unwrap_err();
            assert!(err.contains("SHA-256"), "{err}");
        }
        assert_eq!(dl.calls.load(Ordering::SeqCst), 0);
        assert!(entries(tmp.path()).is_empty());
    }

    #[tokio::test]
    async fn an_upper_case_hash_matches() {
        let tmp = tempfile::tempdir().unwrap();
        let bytes = archive(REQUIRED);
        let dl = fake(Some(bytes.clone()));
        let hash = sha256_hex(&bytes).to_ascii_uppercase();
        assert!(checked(tmp.path(), &hash, &dl).await.is_ok());
    }

    #[tokio::test]
    async fn an_already_complete_folder_is_not_downloaded_again() {
        let tmp = tempfile::tempdir().unwrap();
        let bytes = archive(REQUIRED);
        let dl = fake(Some(bytes.clone()));
        checked(tmp.path(), &sha256_hex(&bytes), &dl).await.unwrap();
        // Even a hash that would fail is not consulted: nothing is fetched.
        checked(tmp.path(), &"0".repeat(64), &dl).await.unwrap();
        assert_eq!(dl.calls.load(Ordering::SeqCst), 1);
    }

    // ── individual files with pinned hashes ──────────────────────

    /// A pinned file whose expected content is its own URL text (what `UrlEcho` writes).
    /// Leaks the hash: fine in a test.
    fn pinned(url: &'static str, name: &'static str) -> CheckedFile {
        CheckedFile {
            url,
            name,
            sha256: Box::leak(sha256_hex(url.as_bytes()).into_boxed_str()),
        }
    }

    fn echo(fail: Vec<&'static str>) -> UrlEcho {
        UrlEcho {
            fail,
            calls: AtomicUsize::new(0),
        }
    }

    const MISMATCH: &str = "the whisper download failed its integrity check";

    #[tokio::test]
    async fn checked_files_with_matching_hashes_install_and_mark_complete() {
        let tmp = tempfile::tempdir().unwrap();
        let files = [
            pinned("https://x/encoder", "encoder.onnx"),
            pinned("https://x/decoder", "decoder.onnx"),
        ];
        let seen = Arc::new(Mutex::new(Vec::new()));
        let s = seen.clone();
        let dl = echo(vec![]);
        let dir = ensure_files_checked(tmp.path(), "whisper", &files, &dl, &move |p| {
            s.lock().unwrap().push(p)
        })
        .await
        .unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.join("decoder.onnx")).unwrap(),
            "https://x/decoder"
        );
        assert!(dir.join(COMPLETE_MARKER).is_file());
        let mut names = entries(&dir);
        names.sort();
        assert_eq!(names, vec![".complete", "decoder.onnx", "encoder.onnx"]);
        assert_eq!(seen.lock().unwrap().last(), Some(&100));
        // A complete folder is returned without another download.
        ensure_files_checked(tmp.path(), "whisper", &files, &dl, &|_| {})
            .await
            .unwrap();
        assert_eq!(dl.calls.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn a_checked_file_with_a_wrong_hash_is_deleted_and_nothing_is_marked_complete() {
        let tmp = tempfile::tempdir().unwrap();
        let mut files = [
            pinned("https://x/encoder", "encoder.onnx"),
            pinned("https://x/decoder", "decoder.onnx"),
        ];
        files[1].sha256 = "0".repeat(64).leak();
        let err = ensure_files_checked(tmp.path(), "whisper", &files, &echo(vec![]), &|_| {})
            .await
            .unwrap_err();
        assert_eq!(err, MISMATCH);
        let dir = tmp.path().join("whisper");
        assert!(!dir.join("decoder.onnx").exists());
        assert!(!dir.join("decoder.onnx.part").exists());
        assert!(!dir.join(COMPLETE_MARKER).exists());

        let good = [
            pinned("https://x/encoder", "encoder.onnx"),
            pinned("https://x/decoder", "decoder.onnx"),
        ];
        let dir = ensure_files_checked(tmp.path(), "whisper", &good, &echo(vec![]), &|_| {})
            .await
            .unwrap();
        assert!(dir.join("decoder.onnx").is_file());
        assert!(dir.join(COMPLETE_MARKER).is_file());
    }

    /// Records every URL requested.
    struct Recording {
        urls: Mutex<Vec<String>>,
    }
    #[async_trait]
    impl Download for Recording {
        async fn download(&self, url: &str, dest: &Path, _: Progress<'_>) -> Result<(), String> {
            self.urls.lock().unwrap().push(url.to_string());
            std::fs::write(dest, url).map_err(|e| e.to_string())
        }
    }

    #[tokio::test]
    async fn a_verified_file_survives_a_later_files_failure_and_is_not_downloaded_again() {
        let tmp = tempfile::tempdir().unwrap();
        let mut files = [
            pinned("https://x/encoder", "encoder.onnx"),
            pinned("https://x/decoder", "decoder.onnx"),
        ];
        let good_pin = files[1].sha256;
        files[1].sha256 = "0".repeat(64).leak();
        let dl = Recording {
            urls: Mutex::new(Vec::new()),
        };
        assert!(
            ensure_files_checked(tmp.path(), "whisper", &files, &dl, &|_| {})
                .await
                .is_err()
        );
        let dir = tmp.path().join("whisper");
        assert!(dir.join("encoder.onnx").is_file(), "verified file stays");

        files[1].sha256 = good_pin;
        dl.urls.lock().unwrap().clear();
        ensure_files_checked(tmp.path(), "whisper", &files, &dl, &|_| {})
            .await
            .unwrap();
        assert_eq!(*dl.urls.lock().unwrap(), vec!["https://x/decoder"]);
    }

    #[tokio::test]
    async fn a_malformed_pin_is_rejected_before_any_download() {
        let tmp = tempfile::tempdir().unwrap();
        let mut files = [
            pinned("https://x/encoder", "encoder.onnx"),
            pinned("https://x/decoder", "decoder.onnx"),
        ];
        files[1].sha256 = "abc123";
        let dl = Recording {
            urls: Mutex::new(Vec::new()),
        };
        let err = ensure_files_checked(tmp.path(), "whisper", &files, &dl, &|_| {})
            .await
            .unwrap_err();
        assert_eq!(
            err,
            "the expected SHA-256 must be exactly 64 hexadecimal characters"
        );
        assert!(dl.urls.lock().unwrap().is_empty());
        assert!(entries(tmp.path()).is_empty(), "no folder created");
    }

    #[tokio::test]
    async fn an_upper_case_pin_matches() {
        let tmp = tempfile::tempdir().unwrap();
        let mut f = pinned("https://x/encoder", "encoder.onnx");
        f.sha256 = Box::leak(f.sha256.to_ascii_uppercase().into_boxed_str());
        assert!(
            ensure_files_checked(tmp.path(), "whisper", &[f], &echo(vec![]), &|_| {})
                .await
                .is_ok()
        );
    }
}
