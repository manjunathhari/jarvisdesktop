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

    #[tokio::test]
    async fn a_corrupt_download_is_an_error() {
        let tmp = tempfile::tempdir().unwrap();
        let err = ensure(tmp.path(), "u", &fake(Some(b"not bzip2".to_vec())), &|_| {}).await;
        assert!(err.is_err());
        assert!(!is_ready(tmp.path()));
    }
}
