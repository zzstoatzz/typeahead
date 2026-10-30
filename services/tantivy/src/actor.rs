use crate::{Error, Result, normalizer};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
fn empty_array() -> Value {
    json!([])
}
fn empty_object() -> Value {
    json!({})
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Actor {
    pub did: String,
    #[serde(default)]
    pub handle: String,
    #[serde(default, alias = "display", alias = "displayName")]
    pub display_name: String,
    #[serde(default, alias = "avatar")]
    pub avatar_url: String,
    #[serde(default = "empty_array")]
    pub labels: Value,
    #[serde(default = "empty_object")]
    pub associated: Value,
    #[serde(default, alias = "createdAt")]
    pub created_at: String,
    #[serde(default)]
    pub pds: String,
    #[serde(default)]
    pub pds_host: String,
    pub quality_score: f32,
    pub version: u64,
    #[serde(default)]
    pub deleted: bool,
    #[serde(default)]
    pub hidden: bool,
    #[serde(default)]
    pub followers_count: i64,
    #[serde(default)]
    pub follows_count: i64,
    #[serde(default)]
    pub posts_count: i64,
    #[serde(default)]
    pub last_activity_at: i64,
}
impl Actor {
    pub fn validate(mut self) -> Result<Self> {
        if !self.did.starts_with("did:")
            || self.did.len() > 2048
            || self.did.bytes().any(|b| b <= 0x20 || b == b'|')
        {
            return Err(Error::Invalid("invalid did".into()));
        }
        if !self.quality_score.is_finite() {
            return Err(Error::Invalid("quality score must be finite f32".into()));
        }
        if self.quality_score == 0.0 {
            self.quality_score = 0.0;
        }
        if self.handle.len() > 65535
            || self.display_name.len() > 65535
            || self.avatar_url.len() > 65535
            || self.created_at.len() > 65535
            || self.pds.len() > 65535
        {
            return Err(Error::Invalid("profile field too large".into()));
        }
        if self.labels.is_string() {
            self.labels = serde_json::from_str(self.labels.as_str().unwrap())?;
        }
        if self.associated.is_string() {
            self.associated = serde_json::from_str(self.associated.as_str().unwrap())?;
        }
        if !self.labels.is_array() || !self.associated.is_object() {
            return Err(Error::Invalid(
                "labels/associated have invalid shape".into(),
            ));
        }
        self.pds_host = normalizer::source_host(&self.pds)?;
        if !self.deleted && self.handle.is_empty() {
            return Err(Error::Invalid("live actor needs handle".into()));
        }
        Ok(self)
    }
    pub fn differing_fields(&self, other: &Self) -> Vec<&'static str> {
        let mut fields = Vec::new();
        macro_rules! compare {
            ($($field:ident),+ $(,)?) => {$(
                if self.$field != other.$field { fields.push(stringify!($field)); }
            )+};
        }
        compare!(
            did,
            handle,
            display_name,
            avatar_url,
            labels,
            associated,
            created_at,
            pds,
            pds_host,
            quality_score,
            version,
            deleted,
            hidden,
            followers_count,
            follows_count,
            posts_count,
            last_activity_at
        );
        fields
    }
    pub fn invalid_pds_reason(&self) -> Option<&'static str> {
        if self.pds.is_empty() || !self.pds_host.is_empty() {
            None
        } else if self.pds.len() > 263 {
            Some("too_long")
        } else {
            Some("invalid_hostname")
        }
    }
    pub fn visible(&self) -> bool {
        !self.hidden
            && !self.deleted
            && !self.handle.is_empty()
            && !self.handle.ends_with(".invalid")
    }
    pub fn rank(&self) -> String {
        let bits = self.quality_score.to_bits();
        let ordered = if bits & 0x8000_0000 != 0 {
            !bits
        } else {
            bits ^ 0x8000_0000
        };
        format!("{:08x}|{}", !ordered, self.did)
    }
    pub fn public(&self) -> Value {
        let mut actor = json!({"did":self.did,"handle":self.handle,"labels":self.labels});
        if !self.display_name.is_empty() {
            actor["displayName"] = json!(self.display_name);
        }
        if !self.created_at.is_empty() {
            actor["createdAt"] = json!(self.created_at);
        }
        if self.associated != json!({}) {
            actor["associated"] = self.associated.clone();
        }
        if !self.avatar_url.is_empty() {
            let cid = avatar_cid(&self.avatar_url);
            if !cid.is_empty() {
                let url = format!("https://cdn.bsky.app/img/avatar/plain/{}/{}", self.did, cid);
                actor["avatar"] = if url.len() <= 512 {
                    json!(url)
                } else {
                    Value::Null
                };
                if self.avatar_url.starts_with("https://")
                    && !self.avatar_url.contains("cdn.bsky.app")
                {
                    actor["avatarFallback"] = json!(self.avatar_url);
                } else if !self.avatar_url.starts_with("https://")
                    && !self.pds_host.is_empty()
                    && !self.pds_host.ends_with(".bsky.network")
                {
                    let fallback = format!(
                        "{}/xrpc/com.atproto.sync.getBlob?did={}&cid={}",
                        if self.pds.starts_with("https://") || self.pds.starts_with("http://") {
                            self.pds.trim_end_matches('/').to_owned()
                        } else {
                            format!("https://{}", self.pds_host)
                        },
                        self.did,
                        cid
                    );
                    if fallback.len() <= 512 {
                        actor["avatarFallback"] = json!(fallback);
                    }
                }
            } else if self.avatar_url.starts_with("https://") {
                actor["avatar"] = json!(self.avatar_url);
            } else {
                actor["avatar"] = Value::Null;
            }
        }
        actor
    }
}
fn avatar_cid(stored: &str) -> &str {
    if !stored.contains('/') {
        return stored;
    }
    if let Some((_, value)) = stored.split_once("cid=") {
        return value.split('&').next().unwrap_or("");
    }
    let part = stored
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('@')
        .next()
        .unwrap_or("");
    if part.contains('?') { "" } else { part }
}
