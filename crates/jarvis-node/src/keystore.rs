//! Where the node's private Ed25519 key lives (spec §7.2 step 2).
//!
//! On Windows it is DPAPI-protected for the signed-in user (`CryptProtectData`, user scope,
//! no UI) and kept as `node-key.dpapi` in the app's data folder; only that user on that PC can
//! unprotect it. A blob that won't unprotect, or doesn't hold exactly 32 bytes, is an error,
//! never a missing key, so the node can't silently re-enrol with a new one.
//!
//! `MemoryKeyStore` is a test double, compiled only for tests and the `test-support` feature.

use ed25519_dalek::SigningKey;

#[derive(Debug, thiserror::Error)]
pub enum KeyStoreError {
    #[error("key store I/O: {0}")]
    Io(#[from] std::io::Error),
    #[error("the protected key could not be read: {0}")]
    Unprotect(String),
    #[error("the key could not be protected: {0}")]
    Protect(String),
    #[error("the stored key is not a 32-byte Ed25519 seed")]
    Corrupt,
}

/// Stores the node's one private key.
pub trait KeyStore: Send + Sync {
    /// The stored key, `None` if there is none yet.
    fn load(&self) -> Result<Option<SigningKey>, KeyStoreError>;
    /// Store `key`, replacing any earlier one.
    fn save(&self, key: &SigningKey) -> Result<(), KeyStoreError>;
    /// Remove the key; removing a missing key is fine.
    fn delete(&self) -> Result<(), KeyStoreError>;
}

/// An in-memory key store for tests.
#[cfg(any(test, feature = "test-support"))]
#[derive(Default)]
pub struct MemoryKeyStore {
    seed: std::sync::Mutex<Option<zeroize::Zeroizing<[u8; 32]>>>,
}

#[cfg(any(test, feature = "test-support"))]
impl KeyStore for MemoryKeyStore {
    fn load(&self) -> Result<Option<SigningKey>, KeyStoreError> {
        let seed = self.seed.lock().expect("not poisoned");
        Ok(seed.as_ref().map(|s| SigningKey::from_bytes(s)))
    }

    fn save(&self, key: &SigningKey) -> Result<(), KeyStoreError> {
        *self.seed.lock().expect("not poisoned") = Some(zeroize::Zeroizing::new(key.to_bytes()));
        Ok(())
    }

    fn delete(&self) -> Result<(), KeyStoreError> {
        *self.seed.lock().expect("not poisoned") = None;
        Ok(())
    }
}

#[cfg(windows)]
pub use dpapi::DpapiKeyStore;

#[cfg(windows)]
mod dpapi {
    use std::io;
    use std::path::{Path, PathBuf};

    use ed25519_dalek::SigningKey;
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{LocalFree, HLOCAL};
    use windows::Win32::Security::Cryptography::{
        CryptProtectData, CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
    };
    use zeroize::Zeroizing;

    use super::{KeyStore, KeyStoreError};

    /// Extra entropy bound into the blob, so another app's DPAPI data can't stand in for it.
    const ENTROPY: &[u8] = b"jarvis-node-key-v1";

    /// The node key, DPAPI-protected for the signed-in user, in one file.
    pub struct DpapiKeyStore {
        path: PathBuf,
    }

    impl DpapiKeyStore {
        /// The file name to use inside the app's data folder.
        pub const FILE_NAME: &'static str = "node-key.dpapi";

        pub fn new(path: impl Into<PathBuf>) -> Self {
            Self { path: path.into() }
        }

        pub fn path(&self) -> &Path {
            &self.path
        }
    }

    fn blob(bytes: &[u8]) -> CRYPT_INTEGER_BLOB {
        CRYPT_INTEGER_BLOB {
            cbData: bytes.len() as u32,
            pbData: bytes.as_ptr() as *mut u8,
        }
    }

    /// Copy DPAPI's output out, wipe its buffer, and free it.
    ///
    /// SAFETY: `out` must be a blob DPAPI just filled (allocated with `LocalAlloc`).
    unsafe fn take(out: CRYPT_INTEGER_BLOB) -> Zeroizing<Vec<u8>> {
        let len = out.cbData as usize;
        let copy = Zeroizing::new(unsafe { std::slice::from_raw_parts(out.pbData, len) }.to_vec());
        unsafe {
            std::ptr::write_bytes(out.pbData, 0, len);
            LocalFree(Some(HLOCAL(out.pbData.cast())));
        }
        copy
    }

    fn protect(plain: &[u8]) -> Result<Vec<u8>, KeyStoreError> {
        let input = blob(plain);
        let entropy = blob(ENTROPY);
        let mut out = CRYPT_INTEGER_BLOB::default();
        // SAFETY: `input` and `entropy` point at live slices for the call; DPAPI only reads
        // them, and fills `out` with a LocalAlloc'd buffer that `take` frees.
        unsafe {
            CryptProtectData(
                &input,
                PCWSTR::null(),
                Some(&entropy),
                None,
                None,
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out,
            )
        }
        .map_err(|e| KeyStoreError::Protect(e.to_string()))?;
        Ok(unsafe { take(out) }.to_vec())
    }

    fn unprotect(sealed: &[u8]) -> Result<Zeroizing<Vec<u8>>, KeyStoreError> {
        let input = blob(sealed);
        let entropy = blob(ENTROPY);
        let mut out = CRYPT_INTEGER_BLOB::default();
        // SAFETY: as in `protect`.
        unsafe {
            CryptUnprotectData(
                &input,
                None,
                Some(&entropy),
                None,
                None,
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out,
            )
        }
        .map_err(|e| KeyStoreError::Unprotect(e.to_string()))?;
        Ok(unsafe { take(out) })
    }

    impl KeyStore for DpapiKeyStore {
        fn load(&self) -> Result<Option<SigningKey>, KeyStoreError> {
            let sealed = match std::fs::read(&self.path) {
                Ok(bytes) => bytes,
                Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
                Err(e) => return Err(e.into()),
            };
            let plain = unprotect(&sealed)?;
            if plain.len() != 32 {
                return Err(KeyStoreError::Corrupt);
            }
            // Copy straight into a zeroised buffer, with no unzeroised temporary.
            let mut seed = Zeroizing::new([0u8; 32]);
            seed.copy_from_slice(&plain);
            Ok(Some(SigningKey::from_bytes(&seed)))
        }

        fn save(&self, key: &SigningKey) -> Result<(), KeyStoreError> {
            let seed = Zeroizing::new(key.to_bytes());
            let sealed = protect(seed.as_slice())?;
            crate::state::write_atomic(&self.path, &sealed)?;
            Ok(())
        }

        fn delete(&self) -> Result<(), KeyStoreError> {
            match std::fs::remove_file(&self.path) {
                Ok(()) => Ok(()),
                Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
                Err(e) => Err(e.into()),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn memory_store_round_trips() {
        let store = MemoryKeyStore::default();
        assert!(store.load().unwrap().is_none());
        let key = SigningKey::from_bytes(&[9; 32]);
        store.save(&key).unwrap();
        assert_eq!(store.load().unwrap().unwrap().to_bytes(), key.to_bytes());
        store.delete().unwrap();
        assert!(store.load().unwrap().is_none());
        store.delete().unwrap();
    }
}
