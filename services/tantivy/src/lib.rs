pub mod actor;
pub mod engine;
pub mod moderation;
pub mod normalizer;
pub mod server;
use std::fmt;
pub type Result<T> = std::result::Result<T, Error>;
#[derive(Debug)]
pub enum Error {
    Invalid(String),
    Conflict(String),
    Budget,
    Busy,
    NotReady,
    Storage(String),
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(s) | Self::Conflict(s) | Self::Storage(s) => write!(f, "{s}"),
            Self::Budget => write!(f, "SearchBudgetExceeded"),
            Self::Busy => write!(f, "search unavailable"),
            Self::NotReady => write!(f, "not ready"),
        }
    }
}
impl std::error::Error for Error {}
impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Self::Storage(e.to_string())
    }
}
impl From<serde_json::Error> for Error {
    fn from(e: serde_json::Error) -> Self {
        Self::Invalid(e.to_string())
    }
}
impl From<tantivy::TantivyError> for Error {
    fn from(e: tantivy::TantivyError) -> Self {
        Self::Storage(e.to_string())
    }
}
