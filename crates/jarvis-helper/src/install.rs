//! Putting the voice helper and its voice files on the PC: the helper program (a `.tar.bz2` on
//! this repo's GitHub Releases) and the two Kokoro model files. Both are checked against SHA-256
//! values pinned below: the helper before anything is unpacked, each voice file before it is
//! moved into place.

use jarvis_fetch::{ensure_archive_checked, ensure_files_checked, CheckedFile, Download};
use std::path::{Path, PathBuf};

/// The Release the helper archive is published under.
pub const HELPER_TAG: &str = "voice-helper-v1";
pub const HELPER_URL: &str = "https://github.com/manjunathhari/jarvisdesktop/releases/download/voice-helper-v1/jarvis-voice-helper-win-x64.tar.bz2";
/// SHA-256 of the `voice-helper-v1` archive (`jarvis-voice-helper-win-x64.tar.bz2`). A download
/// that differs is deleted and never unpacked. A new helper version needs a new tag AND a new
/// value here.
pub const HELPER_SHA256: &str = "8e3ff1def8dc35930ce59a44aa849d62480395011d47a5d64b4dd303659fd8ae";
/// The helper installs into `models_dir/<HELPER_TAG>`, so a later helper version (a new tag)
/// is fetched instead of being hidden by an older complete folder.
pub const HELPER_DIR: &str = HELPER_TAG;
/// The archive's single top folder.
pub const HELPER_ARCHIVE_INNER: &str = "jarvis-voice-helper";
/// The entry the archive must contain.
pub const HELPER_EXE: &str = "jarvis-voice-helper.exe";

pub const KOKORO_V1_DIR: &str = "kokoro-onnx-v1_0";
const KOKORO_MODEL: &str = "kokoro-v1.0.onnx";
const KOKORO_VOICES: &str = "voices-v1.0.bin";
/// SHA-256 of `kokoro-v1.0.onnx` (325,532,387 bytes) from the kokoro-onnx `model-files-v1.0`
/// release.
pub const KOKORO_MODEL_SHA256: &str =
    "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5";
/// SHA-256 of `voices-v1.0.bin` (28,214,398 bytes) from the same release.
pub const KOKORO_VOICES_SHA256: &str =
    "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d";
const KOKORO_FILES: &[CheckedFile] = &[
    CheckedFile {
        url: "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
        name: KOKORO_MODEL,
        sha256: KOKORO_MODEL_SHA256,
    },
    CheckedFile {
        url: "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
        name: KOKORO_VOICES,
        sha256: KOKORO_VOICES_SHA256,
    },
];

/// Where everything the helper needs is on disk.
#[derive(Debug, Clone, PartialEq)]
pub struct HelperFiles {
    pub exe: PathBuf,
    pub model: PathBuf,
    pub voices: PathBuf,
}

/// Install the helper (verified against [`HELPER_SHA256`]) and the Kokoro files (each verified
/// against its pin) into `models_dir`. `percent` reports whichever download is running, so it may go 0-100 more than
/// once (the helper, then the voice files).
pub async fn ensure(
    models_dir: &Path,
    dl: &dyn Download,
    percent: &(dyn Fn(u8) + Send + Sync),
) -> Result<HelperFiles, String> {
    ensure_with(models_dir, dl, percent, HELPER_SHA256).await
}

/// [`ensure`] with the expected helper hash supplied, so tests can use their own fake archive.
/// If the helper fails its check, the voice files are not downloaded.
pub async fn ensure_with(
    models_dir: &Path,
    dl: &dyn Download,
    percent: &(dyn Fn(u8) + Send + Sync),
    helper_sha256: &str,
) -> Result<HelperFiles, String> {
    ensure_inner(models_dir, dl, percent, helper_sha256, KOKORO_FILES).await
}

/// [`ensure_with`] with the voice files supplied too, so tests can pin their own fake bytes.
async fn ensure_inner(
    models_dir: &Path,
    dl: &dyn Download,
    percent: &(dyn Fn(u8) + Send + Sync),
    helper_sha256: &str,
    kokoro: &[CheckedFile],
) -> Result<HelperFiles, String> {
    let helper_dir = ensure_archive_checked(
        models_dir,
        HELPER_DIR,
        HELPER_URL,
        HELPER_ARCHIVE_INNER,
        &[HELPER_EXE],
        helper_sha256,
        dl,
        percent,
    )
    .await?;
    let voice_dir = ensure_files_checked(models_dir, KOKORO_V1_DIR, kokoro, dl, percent).await?;
    Ok(HelperFiles {
        exe: helper_dir.join(HELPER_EXE),
        model: voice_dir.join(KOKORO_MODEL),
        voices: voice_dir.join(KOKORO_VOICES),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use jarvis_fetch::Progress;
    use sha2::{Digest, Sha256};
    use std::sync::Mutex;

    fn helper_archive() -> Vec<u8> {
        let mut tar = tar::Builder::new(Vec::new());
        let body = b"MZ fake exe";
        let mut h = tar::Header::new_gnu();
        h.set_size(body.len() as u64);
        h.set_mode(0o755);
        tar.append_data(
            &mut h,
            format!("{HELPER_ARCHIVE_INNER}/{HELPER_EXE}"),
            &body[..],
        )
        .unwrap();
        let tar_bytes = tar.into_inner().unwrap();
        let mut enc = bzip2::write::BzEncoder::new(Vec::new(), bzip2::Compression::fast());
        std::io::Write::write_all(&mut enc, &tar_bytes).unwrap();
        enc.finish().unwrap()
    }

    fn hex(bytes: &[u8]) -> String {
        Sha256::digest(bytes)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect()
    }

    /// The two real Kokoro entries, with pins that match what `Fake` serves (the URL text).
    fn fake_kokoro() -> Vec<CheckedFile> {
        KOKORO_FILES
            .iter()
            .map(|f| CheckedFile {
                sha256: Box::leak(hex(f.url.as_bytes()).into_boxed_str()),
                ..*f
            })
            .collect()
    }

    /// Serves the helper archive for `HELPER_URL` and the URL text for every other URL,
    /// remembering what was requested.
    struct Fake {
        archive: Vec<u8>,
        requested: Mutex<Vec<String>>,
    }
    impl Fake {
        fn new() -> Self {
            Self {
                archive: helper_archive(),
                requested: Mutex::new(Vec::new()),
            }
        }
        fn requests(&self) -> Vec<String> {
            self.requested.lock().unwrap().clone()
        }
    }
    #[async_trait]
    impl Download for Fake {
        async fn download(
            &self,
            url: &str,
            dest: &Path,
            progress: Progress<'_>,
        ) -> Result<(), String> {
            self.requested.lock().unwrap().push(url.to_string());
            let bytes = if url == HELPER_URL {
                self.archive.clone()
            } else {
                url.as_bytes().to_vec()
            };
            progress(bytes.len() as u64, Some(bytes.len() as u64));
            std::fs::write(dest, bytes).map_err(|e| e.to_string())
        }
    }

    #[tokio::test]
    async fn ensure_returns_the_three_paths_and_downloads_once() {
        let tmp = tempfile::tempdir().unwrap();
        let dl = Fake::new();
        let hash = hex(&dl.archive);
        let first = ensure_inner(tmp.path(), &dl, &|_| {}, &hash, &fake_kokoro())
            .await
            .unwrap();
        assert!(first.exe.is_file() && first.exe.ends_with(HELPER_EXE));
        assert!(first.model.is_file() && first.voices.is_file());
        assert_eq!(dl.requests().len(), 3, "helper plus two voice files");

        let second = ensure_inner(tmp.path(), &dl, &|_| {}, &hash, &fake_kokoro())
            .await
            .unwrap();
        assert_eq!(second, first);
        assert_eq!(dl.requests().len(), 3, "the second call downloads nothing");
    }

    #[tokio::test]
    async fn a_helper_that_fails_its_check_stops_before_the_voice_files() {
        let tmp = tempfile::tempdir().unwrap();
        let dl = Fake::new();
        let err = ensure_inner(tmp.path(), &dl, &|_| {}, &"1".repeat(64), &fake_kokoro())
            .await
            .unwrap_err();
        assert!(err.contains("integrity check"), "{err}");
        assert_eq!(dl.requests(), vec![HELPER_URL.to_string()]);
    }

    #[tokio::test]
    async fn a_download_that_does_not_match_the_real_pin_never_installs() {
        // The fake archive's hash is not the real pin, so the real `ensure` must refuse it.
        assert_ne!(hex(&Fake::new().archive), HELPER_SHA256);
        let tmp = tempfile::tempdir().unwrap();
        let dl = Fake::new();
        let err = ensure(tmp.path(), &dl, &|_| {}).await.unwrap_err();
        assert_eq!(
            err,
            format!("the {HELPER_DIR} download failed its integrity check")
        );
        assert!(!tmp.path().join(HELPER_DIR).exists());
    }

    #[test]
    fn the_helper_hash_is_pinned() {
        assert_eq!(HELPER_SHA256.len(), 64);
        assert!(HELPER_SHA256
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)));
        assert_ne!(HELPER_SHA256, "0".repeat(64));
        // The SHA-256 of the published voice-helper-v1 archive. A later accidental edit of the
        // constant must fail here.
        assert_eq!(
            HELPER_SHA256,
            "8e3ff1def8dc35930ce59a44aa849d62480395011d47a5d64b4dd303659fd8ae"
        );
    }

    #[test]
    fn the_kokoro_pins_are_real() {
        for pin in [KOKORO_MODEL_SHA256, KOKORO_VOICES_SHA256] {
            assert_eq!(pin.len(), 64);
            assert!(pin
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)));
            assert_ne!(pin, "0".repeat(64));
        }
        let pairs: Vec<_> = KOKORO_FILES.iter().map(|f| (f.name, f.sha256)).collect();
        assert_eq!(
            pairs,
            vec![
                ("kokoro-v1.0.onnx", KOKORO_MODEL_SHA256),
                ("voices-v1.0.bin", KOKORO_VOICES_SHA256),
            ]
        );
    }

    #[tokio::test]
    async fn a_tampered_voice_file_fails_the_install() {
        let tmp = tempfile::tempdir().unwrap();
        let dl = Fake::new();
        let mut kokoro = fake_kokoro();
        // The voices file's pin is for different bytes than the fake serves.
        kokoro[1].sha256 = Box::leak(hex(b"something else").into_boxed_str());
        let err = ensure_inner(tmp.path(), &dl, &|_| {}, &hex(&dl.archive), &kokoro)
            .await
            .unwrap_err();
        assert_eq!(
            err,
            format!("the {KOKORO_V1_DIR} download failed its integrity check")
        );
        assert!(!tmp.path().join(KOKORO_V1_DIR).join(".complete").exists());
    }

    #[test]
    fn the_url_names_the_tag() {
        assert!(HELPER_URL.contains(HELPER_TAG));
    }
}
