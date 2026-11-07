//! Commit acknowledgment requires file data and rename metadata to reach disk.
pub(crate) fn replace(path: &std::path::Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    let parent = path.parent().ok_or_else(|| std::io::Error::other("profile path has no parent"))?;
    std::fs::create_dir_all(parent)?;
    let tmp = path.with_extension("commit.tmp");
    let mut file = std::fs::OpenOptions::new().write(true).create(true).truncate(true).open(&tmp)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    std::fs::rename(&tmp, path)?;
    std::fs::File::open(parent)?.sync_all()
}
