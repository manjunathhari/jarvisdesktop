//! DPAPI key storage on a real Windows machine (CI `windows-latest`, or Manj's PC).
#![cfg(windows)]

use ed25519_dalek::SigningKey;
use jarvis_node::keystore::{DpapiKeyStore, KeyStore, KeyStoreError};

fn store() -> (tempfile::TempDir, DpapiKeyStore) {
    let dir = tempfile::tempdir().unwrap();
    let store = DpapiKeyStore::new(dir.path().join(DpapiKeyStore::FILE_NAME));
    (dir, store)
}

#[test]
fn dpapi_round_trip() {
    let (_dir, store) = store();
    assert!(store.load().unwrap().is_none(), "no key yet");
    let key = SigningKey::from_bytes(&[0x5a; 32]);
    store.save(&key).unwrap();
    assert_eq!(store.load().unwrap().unwrap().to_bytes(), key.to_bytes());
    // Saving again replaces the key.
    let other = SigningKey::from_bytes(&[0x33; 32]);
    store.save(&other).unwrap();
    assert_eq!(store.load().unwrap().unwrap().to_bytes(), other.to_bytes());
    store.delete().unwrap();
    assert!(store.load().unwrap().is_none());
    store.delete().unwrap(); // deleting a missing key is fine
}

#[test]
fn dpapi_blob_does_not_contain_the_seed() {
    let (_dir, store) = store();
    let seed = [0xc3u8; 32];
    store.save(&SigningKey::from_bytes(&seed)).unwrap();
    let blob = std::fs::read(store.path()).unwrap();
    assert!(blob.len() > seed.len());
    assert!(
        !blob.windows(seed.len()).any(|w| w == seed),
        "the raw seed must not appear in the file"
    );
    let no_tmp = store
        .path()
        .with_file_name(format!("{}.tmp", DpapiKeyStore::FILE_NAME));
    assert!(!no_tmp.exists());
}

#[test]
fn a_tampered_blob_is_an_error_not_a_missing_key() {
    let (_dir, store) = store();
    store.save(&SigningKey::from_bytes(&[0x11; 32])).unwrap();
    let mut blob = std::fs::read(store.path()).unwrap();
    let last = blob.len() - 1;
    blob[last] ^= 0xff;
    std::fs::write(store.path(), &blob).unwrap();
    assert!(matches!(store.load(), Err(KeyStoreError::Unprotect(_))));
    std::fs::write(store.path(), b"not a dpapi blob").unwrap();
    assert!(matches!(store.load(), Err(KeyStoreError::Unprotect(_))));
}
