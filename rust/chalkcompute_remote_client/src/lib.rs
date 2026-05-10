use bytes::Bytes;
use chalk_remote_call::{
    proto::chalk::auth::v1::Permission, BlockingRemoteCallClient, CallStatus, ChannelSecurity,
    PollBackoff, PollResult, RemoteCallClientConfig, RemoteCallError,
};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::collections::HashMap;
use std::time::Duration;

#[pyclass(name = "RemoteCallClient", module = "chalkcompute._remote_call_native")]
struct PyRemoteCallClient {
    inner: BlockingRemoteCallClient,
}

#[pymethods]
impl PyRemoteCallClient {
    #[new]
    #[pyo3(signature = (
        endpoint,
        metadata=None,
        use_tls=None,
        initial_poll_delay_ms=50,
        max_poll_delay_ms=5000,
        poll_backoff_multiplier=1.7,
        max_message_size=None,
        auth_endpoint=None,
        exchange_token=None,
        requested_permissions=None,
        auth_rpc_timeout_ms=None,
        auth_retry_interval_ms=None
    ))]
    fn new(
        endpoint: String,
        metadata: Option<Vec<(String, String)>>,
        use_tls: Option<bool>,
        initial_poll_delay_ms: u64,
        max_poll_delay_ms: u64,
        poll_backoff_multiplier: f64,
        max_message_size: Option<usize>,
        auth_endpoint: Option<String>,
        exchange_token: Option<String>,
        requested_permissions: Option<Vec<i32>>,
        auth_rpc_timeout_ms: Option<u64>,
        auth_retry_interval_ms: Option<u64>,
    ) -> PyResult<Self> {
        let mut config = RemoteCallClientConfig::new(endpoint).with_poll_backoff(PollBackoff::new(
            Duration::from_millis(initial_poll_delay_ms),
            Duration::from_millis(max_poll_delay_ms),
            poll_backoff_multiplier,
        ));

        if let Some(use_tls) = use_tls {
            config = if use_tls {
                config.with_security(ChannelSecurity::Tls)
            } else {
                config.with_security(ChannelSecurity::Insecure)
            };
        }

        if let Some(max_message_size) = max_message_size {
            config = config.with_max_message_size(max_message_size);
        }

        match (auth_endpoint, exchange_token) {
            (Some(auth_endpoint), Some(exchange_token)) => {
                let permissions = requested_permissions
                    .unwrap_or_default()
                    .into_iter()
                    .map(permission_from_i32)
                    .collect::<PyResult<Vec<_>>>()?;
                config = config.with_service_auth(auth_endpoint, exchange_token, permissions);
                if let Some(timeout) = auth_rpc_timeout_ms {
                    config = config.with_service_auth_rpc_timeout(Duration::from_millis(timeout));
                }
                if let Some(interval) = auth_retry_interval_ms {
                    config =
                        config.with_service_auth_retry_interval(Duration::from_millis(interval));
                }
            }
            (None, None) => {}
            _ => {
                return Err(PyRuntimeError::new_err(
                    "auth_endpoint and exchange_token must be provided together",
                ));
            }
        }

        for (key, value) in metadata.unwrap_or_default() {
            config = config.with_metadata(key, value);
        }

        let inner = BlockingRemoteCallClient::connect(config).map_err(to_py_err)?;
        Ok(Self { inner })
    }

    fn call_ipc(&self, py: Python<'_>, name: String, input_ipc: &[u8]) -> PyResult<Py<PyList>> {
        let input_ipc = Bytes::copy_from_slice(input_ipc);
        let chunks = py
            .allow_threads(|| self.inner.call_ipc(name, input_ipc))
            .map_err(to_py_err)?;
        chunks_to_pylist(py, chunks)
    }

    fn enqueue(&self, py: Python<'_>, name: String, input_ipc: &[u8]) -> PyResult<String> {
        let input_ipc = Bytes::copy_from_slice(input_ipc);
        py.allow_threads(|| self.inner.enqueue(name, input_ipc))
            .map_err(to_py_err)
    }

    #[pyo3(signature = (call_id, cursor=None, poll_count=0))]
    fn poll_next(
        &self,
        py: Python<'_>,
        call_id: String,
        cursor: Option<String>,
        poll_count: u64,
    ) -> PyResult<Py<PyDict>> {
        let cursor = cursor.unwrap_or_default();
        let result = py
            .allow_threads(|| self.inner.poll_next(call_id, cursor, poll_count))
            .map_err(to_py_err)?;
        poll_result_to_pydict(py, result)
    }

    fn get_result_chunks(&self, py: Python<'_>, call_id: String) -> PyResult<Py<PyList>> {
        let chunks = py
            .allow_threads(|| self.inner.get_result_chunks(call_id))
            .map_err(to_py_err)?;
        chunks_to_pylist(py, chunks)
    }

    fn purge_function(&self, py: Python<'_>, function_name: String) -> PyResult<u64> {
        py.allow_threads(|| self.inner.purge_function(function_name))
            .map_err(to_py_err)
    }

    fn purge_all(&self, py: Python<'_>) -> PyResult<HashMap<String, u64>> {
        py.allow_threads(|| self.inner.purge_all())
            .map_err(to_py_err)
    }
}

fn chunks_to_pylist(py: Python<'_>, chunks: Vec<Bytes>) -> PyResult<Py<PyList>> {
    let py_chunks = PyList::empty(py);
    for chunk in chunks {
        py_chunks.append(PyBytes::new(py, &chunk))?;
    }
    Ok(py_chunks.unbind())
}

fn poll_result_to_pydict(py: Python<'_>, result: PollResult) -> PyResult<Py<PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("status", status_code(result.status))?;
    dict.set_item("status_name", status_name(result.status))?;
    dict.set_item("chunks", chunks_to_pylist(py, result.chunks)?)?;
    dict.set_item("cursor", result.cursor)?;
    dict.set_item("error_message", result.error_message)?;
    dict.set_item("poll_count", result.poll_count)?;
    Ok(dict.unbind())
}

fn status_code(status: CallStatus) -> i32 {
    match status {
        CallStatus::Unspecified => 0,
        CallStatus::Pending => 1,
        CallStatus::Running => 2,
        CallStatus::Completed => 3,
        CallStatus::Failed => 4,
        CallStatus::Unknown(raw) => raw,
    }
}

fn status_name(status: CallStatus) -> &'static str {
    match status {
        CallStatus::Unspecified => "unspecified",
        CallStatus::Pending => "pending",
        CallStatus::Running => "running",
        CallStatus::Completed => "completed",
        CallStatus::Failed => "failed",
        CallStatus::Unknown(_) => "unknown",
    }
}

fn to_py_err(error: RemoteCallError) -> PyErr {
    PyRuntimeError::new_err(error.to_string())
}

fn permission_from_i32(raw: i32) -> PyResult<Permission> {
    Permission::from_i32(raw)
        .ok_or_else(|| PyRuntimeError::new_err(format!("unknown permission value: {raw}")))
}

#[pymodule]
fn _remote_call_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyRemoteCallClient>()?;
    Ok(())
}
