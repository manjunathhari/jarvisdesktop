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
    let dir = models_dir.join(KOKORO_DIR);
    if is_ready(models_dir) {
        return Ok(KokoroFiles::in_dir(&dir));
    }
    std::fs::create_dir_all(models_dir).map_err(|e| e.to_string())?;
    let archive = models_dir.join(format!("{KOKORO_DIR}.tar.bz2.part"));
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

    let staging = models_dir.join(".unpacking");
    let (arch, stage) = (archive.clone(), staging.clone());
    let unpacked = tokio::task::spawn_blocking(move || unpack(&arch, &stage))
        .await
        .map_err(|e| e.to_string())
        .and_then(|r| r);
    let _ = std::fs::remove_file(&archive);
    let result = unpacked.and_then(|()| {
        let src = staging.join(KOKORO_DIR);
        for name in REQUIRED {
            if !src.join(name).exists() {
                return Err(format!("the voice download is missing {name}"));
            }
        }
        if dir.exists() {
            std::fs::remove_dir_all(&dir).map_err(|e| e.to_string())?;
        }
        std::fs::rename(&src, &dir).map_err(|e| e.to_string())?;
        std::fs::write(dir.join(COMPLETE_MARKER), b"ok").map_err(|e| e.to_string())
    });
    let _ = std::fs::remove_dir_all(&staging);
    result.map(|()| KokoroFiles::in_dir(&dir))
}

fn unpack(archive: &Path, into: &Path) -> Result<(), String> {
    let _ = std::fs::remove_dir_all(into);
    std::fs::create_dir_all(into).map_err(|e| e.to_string())?;
    let file = std::fs::File::open(archive).map_err(|e| e.to_string())?;
    let bz = bzip2::read::BzDecoder::new(std::io::BufReader::new(file));
    // `unpack` refuses paths that escape the target folder.
    tar::Archive::new(bz)
        .unpack(into)
        .map_err(|e| format!("couldn't unpack the voice: {e}"))
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
    let dir = models_dir.join(dir_name);
    if dir.join(COMPLETE_MARKER).is_file() {
        return Ok(dir);
    }
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let n = files.len().max(1) as u64;
    let last = std::sync::atomic::AtomicU8::new(0);
    for (i, f) in files.iter().enumerate() {
        let target = dir.join(f.name);
        if target.is_file() {
            continue; // finished on an earlier attempt
        }
        let part = dir.join(format!("{}.part", f.name));
        let report = |done: u64, total: Option<u64>| {
            if let Some(t) = total.filter(|t| *t > 0) {
                let p = ((i as u64 * 100 + done.min(t) * 100 / t) / n) as u8;
                if p > last.load(std::sync::atomic::Ordering::Relaxed) {
                    last.store(p, std::sync::atomic::Ordering::Relaxed);
                    percent(p);
                }
            }
        };
        if let Err(e) = dl.download(f.url, &part, &report).await {
            let _ = std::fs::remove_file(&part);
            return Err(e);
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
}
