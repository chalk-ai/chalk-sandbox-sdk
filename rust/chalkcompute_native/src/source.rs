use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use std::fs::File;
use std::io::{self, Read};

fn hash_file(path: &str) -> io::Result<(String, u64, u32)> {
    let mut file = File::open(path)?;
    let before = file.metadata()?;
    if !before.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "source is not a regular file",
        ));
    }
    let mut hasher = blake3::Hasher::new();
    let mut buffer = [0; 65536];
    let mut size = 0;
    loop {
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hasher.update(&buffer[..n]);
        size += n as u64;
    }
    let after = file.metadata()?;
    if size != before.len()
        || before.len() != after.len()
        || before.modified()? != after.modified()?
    {
        return Err(io::Error::other(
            "source changed while hashing; retry deployment",
        ));
    }
    #[cfg(unix)]
    let mode = {
        use std::os::unix::fs::PermissionsExt;
        before.permissions().mode() & 0o777
    };
    #[cfg(not(unix))]
    let mode = 0o644;
    Ok((hasher.finalize().to_hex().to_string(), size, mode))
}

#[pyfunction]
pub fn source_file_digests(
    py: Python<'_>,
    paths: Vec<String>,
) -> PyResult<Vec<(String, u64, u32)>> {
    py.allow_threads(move || {
        // A single pass with bounded memory; the Python caller can share this
        // result across all methods that use the same generated source image.
        paths
            .iter()
            .map(|path| hash_file(path))
            .collect::<io::Result<Vec<_>>>()
    })
    .map_err(|err| PyIOError::new_err(err.to_string()))
}

#[pyfunction]
pub fn source_tree_digest(mut files: Vec<(String, String, u64, u32)>) -> PyResult<String> {
    files.sort_by(|a, b| a.0.cmp(&b.0));
    let mut hash = blake3::Hasher::new();
    hash.update(b"chalk.function-source.v1\0");
    for (path, digest, size, mode) in files {
        let digest = blake3::Hash::from_hex(digest)
            .map_err(|_| PyValueError::new_err("invalid source digest"))?;
        let length =
            u32::try_from(path.len()).map_err(|_| PyValueError::new_err("source path too long"))?;
        hash.update(&length.to_be_bytes());
        hash.update(path.as_bytes());
        hash.update(digest.as_bytes());
        hash.update(&size.to_be_bytes());
        hash.update(&mode.to_be_bytes());
    }
    Ok(hash.finalize().to_hex().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tree_identity_is_order_independent_and_mode_sensitive() {
        let digest = blake3::hash(b"hello").to_hex().to_string();
        let a = ("a.py".to_string(), digest.clone(), 5, 0o644);
        let b = ("b.py".to_string(), digest, 5, 0o644);
        assert_eq!(
            source_tree_digest(vec![a.clone(), b.clone()]).unwrap(),
            source_tree_digest(vec![b, a.clone()]).unwrap()
        );
        let mut executable = a.clone();
        executable.3 = 0o755;
        assert_ne!(
            source_tree_digest(vec![a]).unwrap(),
            source_tree_digest(vec![executable]).unwrap()
        );
    }
}
