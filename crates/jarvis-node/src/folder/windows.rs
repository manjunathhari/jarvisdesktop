//! The Windows test folder: spec §9.4, step for step.
//!
//! **Written and checked by hand against the `windows` 0.61 crate on Linux; it has not yet
//! been compiled or run on Windows.** `tests/folder_windows.rs` proves it on `windows-latest`
//! or on Manj's PC (plan B Task 8, Step 5).
//!
//! - Every file is opened with `FILE_FLAG_OPEN_REPARSE_POINT`, so a link is opened as itself,
//!   never followed, and refused.
//! - Identity (file ID, size, SHA-256, final path) is always read through the handle that the
//!   next step uses, with no gap between check and use.
//! - Staging is `<folder>\.jarvis-pending\<execution_key>`, created by this node with a
//!   protected DACL (inheritance off; the signed-in user and SYSTEM only), read back and
//!   checked again before the recycle.
//! - The recycle is `IFileOperation` with `FOFX_RECYCLEONDELETE`, never a permanent delete,
//!   while this node holds the file open with `FILE_SHARE_DELETE` only; afterwards the same
//!   handle must show the file under `$Recycle.Bin`. `FOF_NO_UI` answers "yes" to the shell's
//!   "delete permanently?" question (Recycle Bin off for the volume, or the file too large),
//!   so a progress sink cancels any delete the shell is not going to recycle: the file then
//!   stays in staging and the receipt is REFUSED `recycle_bin_unavailable`.

use std::ffi::c_void;
use std::fs::{File, OpenOptions};
use std::io::{self, Read};
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::os::windows::io::AsRawHandle;
use std::path::{Component, Path, PathBuf, Prefix};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use chrono::{DateTime, FixedOffset};
use sha2::{Digest, Sha256};
use uuid::Uuid;
use windows::core::{implement, Ref, HSTRING, PCWSTR, PWSTR};
use windows::Win32::Foundation::{CloseHandle, LocalFree, E_ABORT, HANDLE, HLOCAL};
use windows::Win32::Security::Authorization::{
    ConvertSecurityDescriptorToStringSecurityDescriptorW, ConvertSidToStringSidW,
    ConvertStringSecurityDescriptorToSecurityDescriptorW, GetSecurityInfo, SetSecurityInfo,
    SDDL_REVISION_1, SE_FILE_OBJECT,
};
use windows::Win32::Security::{
    GetSecurityDescriptorDacl, GetTokenInformation, TokenUser, ACL, DACL_SECURITY_INFORMATION,
    PROTECTED_DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, TOKEN_QUERY, TOKEN_USER,
};
use windows::Win32::Storage::FileSystem::{
    ExtendedFileIdType, FileIdInfo, FileRenameInfoEx, GetDriveTypeW, GetFileInformationByHandle,
    GetFileInformationByHandleEx, GetFinalPathNameByHandleW, GetVolumeInformationByHandleW,
    OpenFileById, SetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    FILE_FLAGS_AND_ATTRIBUTES, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_ID_128, FILE_ID_DESCRIPTOR, FILE_ID_DESCRIPTOR_0, FILE_ID_INFO, FILE_NAME_NORMALIZED,
    FILE_RENAME_INFO, FILE_SHARE_DELETE, FILE_SHARE_MODE, FILE_SHARE_READ, FILE_SHARE_WRITE,
};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL, COINIT_APARTMENTTHREADED,
};
use windows::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};
use windows::Win32::UI::Shell::{
    FileOperation, IFileOperation, IFileOperationProgressSink, IFileOperationProgressSink_Impl,
    IShellItem, SHCreateItemFromParsingName, FOFX_RECYCLEONDELETE, FOF_ALLOWUNDO, FOF_NO_UI,
    TSF_DELETE_RECYCLE_IF_POSSIBLE,
};

use super::{
    child, in_recycle_bin, sddl_is_protected_for, valid_name, FolderOps, Located, Outcome,
};
use crate::journal::FileFacts;

/// The staging folder's name, inside the test folder (never listed: it starts with a dot).
pub const STAGING: &str = ".jarvis-pending";

// Access rights and attributes (winnt.h).
const GENERIC_READ: u32 = 0x8000_0000;
const DELETE: u32 = 0x0001_0000;
const READ_CONTROL: u32 = 0x0002_0000;
const WRITE_DAC: u32 = 0x0004_0000;
const FILE_READ_ATTRIBUTES: u32 = 0x0080;
const FILE_ATTRIBUTE_DIRECTORY: u32 = 0x10;
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
const DRIVE_FIXED: u32 = 3;
const ERROR_FILE_NOT_FOUND: i32 = 2;
const ERROR_PATH_NOT_FOUND: i32 = 3;
const ERROR_SHARING_VIOLATION: i32 = 32;

/// The test folder at an absolute path on a local fixed NTFS volume.
pub struct WindowsFolder {
    path: PathBuf,
}

impl WindowsFolder {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    /// The folder, opened as itself: missing, a link, not a folder, not on a local fixed NTFS
    /// volume → refused with that check.
    fn open_folder(&self) -> Result<File, Outcome> {
        if !self.path.is_absolute() {
            return Err(Outcome::Refused("folder_not_absolute"));
        }
        match self.path.components().next() {
            Some(Component::Prefix(p)) => match p.kind() {
                Prefix::Disk(letter) | Prefix::VerbatimDisk(letter) => {
                    let root = HSTRING::from(format!("{}:\\", letter as char));
                    if unsafe { GetDriveTypeW(&root) } != DRIVE_FIXED {
                        return Err(Outcome::Refused("not_fixed_drive"));
                    }
                }
                _ => return Err(Outcome::Refused("network_path")),
            },
            _ => return Err(Outcome::Refused("folder_not_absolute")),
        }
        let folder =
            open_dir(&self.path, FILE_READ_ATTRIBUTES | READ_CONTROL).map_err(|e| {
                match e.raw_os_error() {
                    Some(ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND) => {
                        Outcome::Refused("folder_missing")
                    }
                    _ => Outcome::Refused("folder_unopenable"),
                }
            })?;
        let attrs = attributes(&folder).map_err(|_| Outcome::Refused("folder_unopenable"))?;
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(Outcome::Refused("folder_reparse_point"));
        }
        if attrs & FILE_ATTRIBUTE_DIRECTORY == 0 {
            return Err(Outcome::Refused("folder_not_directory"));
        }
        if file_system(&folder).as_deref() != Some("NTFS") {
            return Err(Outcome::Refused("not_ntfs"));
        }
        Ok(folder)
    }

    fn staging_root(&self) -> PathBuf {
        self.path.join(STAGING)
    }

    /// `.jarvis-pending`, created if missing, not a link, with the protected DACL (set if it
    /// isn't, then read back).
    fn ensure_staging_root(&self, user_sid: &str) -> Result<File, Outcome> {
        let root = self.staging_root();
        match std::fs::create_dir(&root) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {}
            Err(_) => return Err(Outcome::Refused("staging_setup")),
        }
        let dir = open_dir(&root, FILE_READ_ATTRIBUTES | READ_CONTROL | WRITE_DAC)
            .map_err(|_| Outcome::Refused("staging_setup"))?;
        if is_reparse(&dir)? {
            return Err(Outcome::Refused("staging_reparse_point"));
        }
        if !dacl_is_protected_for(&dir, user_sid) {
            set_protected_dacl(&dir, user_sid).map_err(|_| Outcome::Refused("staging_dacl"))?;
            if !dacl_is_protected_for(&dir, user_sid) {
                return Err(Outcome::Refused("staging_dacl"));
            }
        }
        Ok(dir)
    }

    fn staging_dir(&self, key: Uuid) -> PathBuf {
        self.staging_root().join(key.to_string())
    }
}

impl FolderOps for WindowsFolder {
    fn check_folder(&self) -> Result<(), Outcome> {
        self.open_folder().map(|_| ())
    }

    fn list(&self) -> Result<Vec<FileFacts>, Outcome> {
        let _folder = self.open_folder()?;
        let entries =
            std::fs::read_dir(&self.path).map_err(|_| Outcome::Error("listing_failed"))?;
        let mut files = Vec::new();
        for entry in entries.flatten() {
            let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
                continue;
            };
            if !valid_name(&name) {
                continue;
            }
            // One odd file never fails the listing: anything unreadable is left out.
            let share = FILE_SHARE_READ;
            let Ok(file) = open_file(&self.path.join(&name), GENERIC_READ, share) else {
                continue;
            };
            match (regular(&file), link_count(&file)) {
                (Ok(true), Ok(1)) => {}
                _ => continue,
            }
            if let Ok(facts) = facts_of(&file, &name) {
                files.push(facts);
            }
        }
        Ok(files)
    }

    fn stage(&self, want: &FileFacts, key: Uuid) -> Result<(), Outcome> {
        let folder = self.open_folder()?;
        if !valid_name(&want.name) {
            return Err(Outcome::Refused("bad_name"));
        }
        // Step 1: exclusive, as itself.
        let file = open_file(
            &self.path.join(&want.name),
            GENERIC_READ | DELETE,
            FILE_SHARE_MODE(0),
        )
        .map_err(|e| match e.raw_os_error() {
            Some(ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND) => Outcome::NotFound,
            Some(ERROR_SHARING_VIOLATION) => Outcome::Refused("file_in_use"),
            _ => Outcome::Error("file_unopenable"),
        })?;
        if !regular(&file)? {
            return Err(Outcome::Refused("not_regular_file"));
        }
        if link_count(&file)? != 1 {
            return Err(Outcome::Refused("hard_linked"));
        }
        // Step 2, through that handle.
        let folder_path = final_path(&folder).map_err(|_| Outcome::Error("final_path"))?;
        let file_path = final_path(&file).map_err(|_| Outcome::Error("final_path"))?;
        if file_path != child(&folder_path, &want.name) {
            return Err(Outcome::Mismatch("path"));
        }
        let have = facts_of(&file, &want.name).map_err(|_| Outcome::Error("read_failed"))?;
        if have.file_id != want.file_id {
            return Err(Outcome::Mismatch("file_id"));
        }
        if have.size != want.size {
            return Err(Outcome::Mismatch("size"));
        }
        if have.sha256 != want.sha256 {
            return Err(Outcome::Mismatch("sha256"));
        }
        // Step 4's protected staging (created before the move, so the move lands in it).
        let sid = user_sid().map_err(|_| Outcome::Refused("staging_setup"))?;
        let _root = self.ensure_staging_root(&sid)?;
        let sub = self.staging_dir(key);
        match std::fs::create_dir(&sub) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
                return Err(Outcome::Refused("staging_exists"))
            }
            Err(_) => return Err(Outcome::Refused("staging_setup")),
        }
        let sub_dir = open_dir(&sub, FILE_READ_ATTRIBUTES | READ_CONTROL | WRITE_DAC)
            .map_err(|_| Outcome::Refused("staging_setup"))?;
        if is_reparse(&sub_dir)? {
            return Err(Outcome::Refused("staging_reparse_point"));
        }
        set_protected_dacl(&sub_dir, &sid).map_err(|_| Outcome::Refused("staging_dacl"))?;
        if !dacl_is_protected_for(&sub_dir, &sid) {
            return Err(Outcome::Refused("staging_dacl"));
        }
        // Step 3: rename by handle into the staging subfolder, same volume, atomic.
        let sub_path = final_path(&sub_dir).map_err(|_| Outcome::Error("final_path"))?;
        rename_by_handle(&file, &child(&sub_path, &want.name))
            .map_err(|_| Outcome::Error("stage_rename_failed"))?;
        Ok(())
    }

    fn recycle_staged(
        &self,
        want: &FileFacts,
        key: Uuid,
    ) -> Result<DateTime<FixedOffset>, Outcome> {
        let _folder = self.open_folder()?;
        let sid = user_sid().map_err(|_| Outcome::Refused("staging_dacl"))?;
        // The staging is still ours: not a link, DACL unchanged.
        let root = open_dir(&self.staging_root(), FILE_READ_ATTRIBUTES | READ_CONTROL)
            .map_err(|_| Outcome::Refused("staging_missing"))?;
        let sub = open_dir(&self.staging_dir(key), FILE_READ_ATTRIBUTES | READ_CONTROL)
            .map_err(|_| Outcome::NotFound)?;
        if is_reparse(&root)? || is_reparse(&sub)? {
            return Err(Outcome::Refused("staging_reparse_point"));
        }
        if !dacl_is_protected_for(&root, &sid) || !dacl_is_protected_for(&sub, &sid) {
            return Err(Outcome::Refused("staging_dacl"));
        }
        // Re-open the staged file, sharing only DELETE, and check it through that handle.
        let staged = self.staging_dir(key).join(&want.name);
        // A scanner (antivirus, the indexer) may hold it briefly: a short, bounded retry.
        let mut tries = 0;
        let file = loop {
            match open_file(&staged, GENERIC_READ, FILE_SHARE_DELETE) {
                Ok(f) => break f,
                Err(e) if e.raw_os_error() == Some(ERROR_SHARING_VIOLATION) && tries < 10 => {
                    tries += 1;
                    std::thread::sleep(std::time::Duration::from_millis(200));
                }
                Err(e) => {
                    return Err(match e.raw_os_error() {
                        Some(ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND) => Outcome::NotFound,
                        _ => Outcome::Refused("staged_unopenable"),
                    })
                }
            }
        };
        if !regular(&file)? {
            return Err(Outcome::Refused("not_regular_file"));
        }
        if link_count(&file)? != 1 {
            return Err(Outcome::Refused("hard_linked"));
        }
        let sub_path = final_path(&sub).map_err(|_| Outcome::Refused("staged_path"))?;
        let file_path = final_path(&file).map_err(|_| Outcome::Refused("staged_path"))?;
        if file_path != child(&sub_path, &want.name) {
            return Err(Outcome::Refused("staged_path"));
        }
        let have =
            facts_of(&file, &want.name).map_err(|_| Outcome::Refused("staged_unreadable"))?;
        if have.file_id != want.file_id {
            return Err(Outcome::Refused("file_id"));
        }
        if have.size != want.size || have.sha256 != want.sha256 {
            return Err(Outcome::Refused("sha256"));
        }
        // Recycle, still holding the handle; then check where the handle's object is.
        match recycle(&staged) {
            Ok(()) => {}
            Err(Recycle::WouldDestroy) => return Err(Outcome::Refused("recycle_bin_unavailable")),
            Err(Recycle::Failed) => return Err(Outcome::Error("recycle_failed")),
        }
        let after = final_path(&file).map_err(|_| Outcome::Error("not_in_recycle_bin"))?;
        if !in_recycle_bin(&after) {
            return Err(Outcome::Error("not_in_recycle_bin"));
        }
        Ok(chrono::Utc::now().fixed_offset())
    }

    fn locate(&self, want: &FileFacts, key: Uuid) -> Located {
        if std::fs::symlink_metadata(self.staging_dir(key).join(&want.name)).is_ok() {
            return Located::Staged;
        }
        let Ok(folder) = open_dir(&self.path, FILE_READ_ATTRIBUTES) else {
            return Located::Unknown;
        };
        let Some(id) = parse_file_id(&want.file_id) else {
            return Located::Unknown;
        };
        let Ok(found) = open_by_id(&folder, id) else {
            return Located::Unknown;
        };
        let (Ok(folder_path), Ok(path)) = (final_path(&folder), final_path(&found)) else {
            return Located::Unknown;
        };
        if path == child(&folder_path, &want.name) {
            Located::InFolder
        } else if in_recycle_bin(&path) {
            Located::InRecycleBin
        } else {
            Located::Unknown
        }
    }
}

// ------------------------------------------------------------------ handles and facts

fn handle(f: &File) -> HANDLE {
    HANDLE(f.as_raw_handle())
}

fn open_dir(path: &Path, access: u32) -> io::Result<File> {
    OpenOptions::new()
        .access_mode(access)
        .share_mode((FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE).0)
        .custom_flags((FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT).0)
        .open(path)
}

/// Opened as itself (`FILE_FLAG_OPEN_REPARSE_POINT`); `FILE_FLAG_BACKUP_SEMANTICS` lets a
/// folder or junction standing where a file should be open too, so it is refused as
/// `not_regular_file` rather than failing to open.
fn open_file(path: &Path, access: u32, share: FILE_SHARE_MODE) -> io::Result<File> {
    OpenOptions::new()
        .access_mode(access)
        .share_mode(share.0)
        .custom_flags((FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS).0)
        .open(path)
}

fn attributes(f: &File) -> io::Result<u32> {
    Ok(f.metadata()?.file_attributes())
}

fn is_reparse(f: &File) -> Result<bool, Outcome> {
    attributes(f)
        .map(|a| a & FILE_ATTRIBUTE_REPARSE_POINT != 0)
        .map_err(|_| Outcome::Refused("attributes_unreadable"))
}

/// A plain file: not a folder, not a reparse point.
fn regular(f: &File) -> Result<bool, Outcome> {
    let a = attributes(f).map_err(|_| Outcome::Refused("attributes_unreadable"))?;
    Ok(a & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT) == 0)
}

/// How many names the file has. More than one (a hard link, perhaps outside the folder):
/// "deleting" this name wouldn't remove the file, so it is never listed or deleted.
fn link_count(f: &File) -> Result<u32, Outcome> {
    let mut info = BY_HANDLE_FILE_INFORMATION::default();
    unsafe { GetFileInformationByHandle(handle(f), &mut info) }
        .map_err(|_| Outcome::Refused("attributes_unreadable"))?;
    Ok(info.nNumberOfLinks)
}

/// Name, size, SHA-256 and file ID, all read through `f` (from its start).
fn facts_of(f: &File, name: &str) -> io::Result<FileFacts> {
    let size = f.metadata()?.file_size();
    let mut hasher = Sha256::new();
    let mut reader = f;
    let mut buf = vec![0u8; 64 * 1024];
    let mut read = 0u64;
    loop {
        let n = reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        read += n as u64;
        hasher.update(&buf[..n]);
    }
    if read != size {
        return Err(io::Error::other("size changed while reading"));
    }
    Ok(FileFacts {
        name: name.to_owned(),
        size,
        sha256: data_encoding::HEXLOWER.encode(&hasher.finalize()),
        file_id: file_id(f)?,
    })
}

/// `GetFileInformationByHandleEx(FileIdInfo)`: volume serial (16 hex) `:` file ID (32 hex).
fn file_id(f: &File) -> io::Result<String> {
    let mut info = FILE_ID_INFO::default();
    unsafe {
        GetFileInformationByHandleEx(
            handle(f),
            FileIdInfo,
            &mut info as *mut FILE_ID_INFO as *mut c_void,
            std::mem::size_of::<FILE_ID_INFO>() as u32,
        )
    }
    .map_err(io::Error::other)?;
    let id = u128::from_le_bytes(info.FileId.Identifier);
    Ok(format!("{:016x}:{id:032x}", info.VolumeSerialNumber))
}

fn parse_file_id(s: &str) -> Option<[u8; 16]> {
    let (_, id) = s.split_once(':')?;
    u128::from_str_radix(id, 16).ok().map(u128::to_le_bytes)
}

/// The object with this file ID on the folder's volume, opened as itself, read attributes only.
fn open_by_id(volume_hint: &File, id: [u8; 16]) -> io::Result<File> {
    use std::os::windows::io::FromRawHandle;
    let desc = FILE_ID_DESCRIPTOR {
        dwSize: std::mem::size_of::<FILE_ID_DESCRIPTOR>() as u32,
        Type: ExtendedFileIdType,
        Anonymous: FILE_ID_DESCRIPTOR_0 {
            ExtendedFileId: FILE_ID_128 { Identifier: id },
        },
    };
    let h = unsafe {
        OpenFileById(
            handle(volume_hint),
            &desc,
            FILE_READ_ATTRIBUTES,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            FILE_FLAGS_AND_ATTRIBUTES(
                FILE_FLAG_BACKUP_SEMANTICS.0 | FILE_FLAG_OPEN_REPARSE_POINT.0,
            ),
        )
    }
    .map_err(io::Error::other)?;
    // The File owns (and closes) the handle from here.
    Ok(unsafe { File::from_raw_handle(h.0) })
}

/// `GetFinalPathNameByHandleW`, normalised, DOS volume name (`\\?\C:\...`).
fn final_path(f: &File) -> io::Result<String> {
    let mut buf = vec![0u16; 512];
    loop {
        let n = unsafe { GetFinalPathNameByHandleW(handle(f), &mut buf, FILE_NAME_NORMALIZED) }
            as usize;
        if n == 0 {
            return Err(io::Error::last_os_error());
        }
        if n < buf.len() {
            return String::from_utf16(&buf[..n]).map_err(io::Error::other);
        }
        buf.resize(n + 1, 0);
    }
}

/// The volume's file system name (`NTFS`).
fn file_system(f: &File) -> Option<String> {
    let mut name = [0u16; 64];
    unsafe {
        GetVolumeInformationByHandleW(handle(f), None, None, None, None, Some(&mut name[..]))
    }
    .ok()?;
    let len = name.iter().position(|c| *c == 0).unwrap_or(name.len());
    String::from_utf16(&name[..len]).ok()
}

/// `SetFileInformationByHandle(FileRenameInfoEx)` to `target` (a full `\\?\` path), never
/// replacing an existing file.
fn rename_by_handle(f: &File, target: &str) -> io::Result<()> {
    let name: Vec<u16> = target.encode_utf16().collect();
    let header = std::mem::size_of::<FILE_RENAME_INFO>();
    let size = header + name.len() * 2;
    // u64 storage keeps the struct (which holds a HANDLE) 8-byte aligned.
    let mut storage = vec![0u64; size.div_ceil(8)];
    let info = storage.as_mut_ptr() as *mut FILE_RENAME_INFO;
    unsafe {
        (*info).Anonymous.Flags = 0; // no FILE_RENAME_FLAG_REPLACE_IF_EXISTS
        (*info).RootDirectory = HANDLE::default();
        (*info).FileNameLength = (name.len() * 2) as u32;
        let dst = std::ptr::addr_of_mut!((*info).FileName) as *mut u16;
        std::ptr::copy_nonoverlapping(name.as_ptr(), dst, name.len());
        SetFileInformationByHandle(
            handle(f),
            FileRenameInfoEx,
            info as *const c_void,
            size as u32,
        )
    }
    .map_err(io::Error::other)
}

enum Recycle {
    /// The shell would have deleted it permanently; cancelled, the file is untouched.
    WouldDestroy,
    Failed,
}

/// Cancels any delete the shell won't send to the Recycle Bin.
#[implement(IFileOperationProgressSink)]
struct RecycleOnly {
    cancelled: Arc<AtomicBool>,
}

impl IFileOperationProgressSink_Impl for RecycleOnly_Impl {
    fn StartOperations(&self) -> windows::core::Result<()> {
        Ok(())
    }
    fn FinishOperations(&self, _: windows::core::HRESULT) -> windows::core::Result<()> {
        Ok(())
    }
    fn PreRenameItem(
        &self,
        _: u32,
        _: Ref<'_, IShellItem>,
        _: &PCWSTR,
    ) -> windows::core::Result<()> {
        Ok(())
    }
    fn PostRenameItem(
        &self,
        _: u32,
        _: Ref<'_, IShellItem>,
        _: &PCWSTR,
        _: windows::core::HRESULT,
        _: Ref<'_, IShellItem>,
    ) -> windows::core::Result<()> {
        Ok(())
    }
    fn PreMoveItem(
        &self,
        _: u32,
        _: Ref<'_, IShellItem>,
        _: Ref<'_, IShellItem>,
        _: &PCWSTR,
    ) -> windows::core::Result<()> {
        Ok(())
    }
    fn PostMoveItem(
        &self,
        _: u32,
        _: Ref<'_, IShellItem>,
        _: Ref<'_, IShellItem>,
        _: &PCWSTR,
        _: windows::core::HRESULT,
        _: Ref<'_, IShellItem>,
    ) -> windows::core::Result<()> {
        Ok(())
    }
    fn PreCopyItem(
        &self,
        _: u32,
        _: Ref<'_, IShellItem>,
        _: Ref<'_, IShellItem>,
        _: &PCWSTR,
    ) -> windows::core::Result<()> {
        Ok(())
    }
    fn PostCopyItem(
        &self,
        _: u32,
        _: Ref<'_, IShellItem>,
        _: Ref<'_, IShellItem>,
        _: &PCWSTR,
        _: windows::core::HRESULT,
        _: Ref<'_, IShellItem>,
    ) -> windows::core::Result<()> {
        Ok(())
    }
    fn PreDeleteItem(&self, dwflags: u32, _: Ref<'_, IShellItem>) -> windows::core::Result<()> {
        if dwflags & TSF_DELETE_RECYCLE_IF_POSSIBLE.0 as u32 == 0 {
            // It would be destroyed, not recycled: cancel this delete.
            self.cancelled.store(true, Ordering::SeqCst);
            return Err(windows::core::Error::from_hresult(E_ABORT));
        }
        Ok(())
    }
    fn PostDeleteItem(
        &self,
        _: u32,
        _: Ref<'_, IShellItem>,
        _: windows::core::HRESULT,
        _: Ref<'_, IShellItem>,
    ) -> windows::core::Result<()> {
        Ok(())
    }
    fn PreNewItem(&self, _: u32, _: Ref<'_, IShellItem>, _: &PCWSTR) -> windows::core::Result<()> {
        Ok(())
    }
    fn PostNewItem(
        &self,
        _: u32,
        _: Ref<'_, IShellItem>,
        _: &PCWSTR,
        _: &PCWSTR,
        _: u32,
        _: windows::core::HRESULT,
        _: Ref<'_, IShellItem>,
    ) -> windows::core::Result<()> {
        Ok(())
    }
    fn UpdateProgress(&self, _: u32, _: u32) -> windows::core::Result<()> {
        Ok(())
    }
    fn ResetTimer(&self) -> windows::core::Result<()> {
        Ok(())
    }
    fn PauseTimer(&self) -> windows::core::Result<()> {
        Ok(())
    }
    fn ResumeTimer(&self) -> windows::core::Result<()> {
        Ok(())
    }
}

/// `IFileOperation` with `FOFX_RECYCLEONDELETE`: to the Recycle Bin, never a permanent delete
/// (the sink cancels one that would be).
fn recycle(path: &Path) -> Result<(), Recycle> {
    let cancelled = Arc::new(AtomicBool::new(false));
    let sink: IFileOperationProgressSink = RecycleOnly {
        cancelled: cancelled.clone(),
    }
    .into();
    let result = unsafe {
        let init = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
        let result = (|| -> windows::core::Result<bool> {
            let op: IFileOperation = CoCreateInstance(&FileOperation, None, CLSCTX_ALL)?;
            op.SetOperationFlags(FOF_NO_UI | FOF_ALLOWUNDO | FOFX_RECYCLEONDELETE)?;
            let item: IShellItem = SHCreateItemFromParsingName(&HSTRING::from(path), None)?;
            op.DeleteItem(&item, &sink)?;
            let performed = op.PerformOperations();
            let aborted = op.GetAnyOperationsAborted()?.as_bool();
            Ok(performed.is_ok() && !aborted)
        })();
        if init.is_ok() {
            CoUninitialize();
        }
        result
    };
    if cancelled.load(Ordering::SeqCst) {
        return Err(Recycle::WouldDestroy);
    }
    match result {
        Ok(true) => Ok(()),
        _ => Err(Recycle::Failed),
    }
}

// ------------------------------------------------------------------ the protected DACL

/// The signed-in user's SID, as a string (`S-1-5-21-…`).
fn user_sid() -> windows::core::Result<String> {
    unsafe {
        let mut token = HANDLE::default();
        OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token)?;
        let mut len = 0u32;
        let _ = GetTokenInformation(token, TokenUser, None, 0, &mut len);
        let mut buf = vec![0u64; (len as usize).div_ceil(8)];
        let got = GetTokenInformation(
            token,
            TokenUser,
            Some(buf.as_mut_ptr() as *mut c_void),
            len,
            &mut len,
        );
        let _ = CloseHandle(token);
        got?;
        let user = &*(buf.as_ptr() as *const TOKEN_USER);
        let mut s = PWSTR::null();
        ConvertSidToStringSidW(user.User.Sid, &mut s)?;
        let sid = s.to_string();
        let _ = LocalFree(Some(HLOCAL(s.0 as *mut c_void)));
        sid.map_err(|_| {
            windows::core::Error::from_hresult(windows::core::HRESULT(0x8007_000d_u32 as i32))
        })
    }
}

/// Full control for SYSTEM and the user, inherited by children; inheritance from above off.
fn sddl_for(user_sid: &str) -> String {
    format!("D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{user_sid})")
}

fn set_protected_dacl(dir: &File, user_sid: &str) -> windows::core::Result<()> {
    unsafe {
        let mut sd = PSECURITY_DESCRIPTOR::default();
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            &HSTRING::from(sddl_for(user_sid)),
            SDDL_REVISION_1,
            &mut sd,
            None,
        )?;
        let mut present = windows::core::BOOL(0);
        let mut defaulted = windows::core::BOOL(0);
        let mut dacl: *mut ACL = std::ptr::null_mut();
        let got = GetSecurityDescriptorDacl(sd, &mut present, &mut dacl, &mut defaulted);
        let set = got.and_then(|()| {
            SetSecurityInfo(
                handle(dir),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                Some(dacl as *const ACL),
                None,
            )
            .ok()
        });
        let _ = LocalFree(Some(HLOCAL(sd.0)));
        set
    }
}

/// The DACL as SDDL (`D:P(…)(…)`), read back from the handle.
fn dacl_sddl(dir: &File) -> windows::core::Result<String> {
    unsafe {
        let mut sd = PSECURITY_DESCRIPTOR::default();
        GetSecurityInfo(
            handle(dir),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            Some(&mut sd),
        )
        .ok()?;
        let mut s = PWSTR::null();
        let conv = ConvertSecurityDescriptorToStringSecurityDescriptorW(
            sd,
            SDDL_REVISION_1,
            DACL_SECURITY_INFORMATION,
            &mut s,
            None,
        );
        let text = conv.and_then(|()| {
            s.to_string().map_err(|_| {
                windows::core::Error::from_hresult(windows::core::HRESULT(0x8007_000d_u32 as i32))
            })
        });
        if !s.is_null() {
            let _ = LocalFree(Some(HLOCAL(s.0 as *mut c_void)));
        }
        let _ = LocalFree(Some(HLOCAL(sd.0)));
        text
    }
}

fn dacl_is_protected_for(dir: &File, user_sid: &str) -> bool {
    dacl_sddl(dir).is_ok_and(|s| sddl_is_protected_for(&s, user_sid))
}
