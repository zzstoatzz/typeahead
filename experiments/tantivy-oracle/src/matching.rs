use crate::{ffi, wide::Exact};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{
    cmp::{Ordering, Reverse},
    collections::{BTreeMap, BinaryHeap, HashMap},
};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Row {
    pub did: String,
    pub handle: String,
    #[serde(default, alias = "display")]
    pub display_name: String,
    #[serde(default)]
    pub quality_score: f32,
    #[serde(default)]
    pub pds: String,
    #[serde(default)]
    pub hidden: bool,
    #[serde(default)]
    pub deleted: bool,
    pub version: u64,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Case {
    pub id: String,
    pub q: String,
    pub limit: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pds: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prefer_pds: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub preference: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub boost: Option<f32>,
    pub category: String,
    #[serde(default)]
    pub native_limitations: Vec<String>,
    #[serde(default)]
    pub native_shared_features: bool,
}
#[derive(Clone)]
pub struct Group {
    pub cjk: bool,
    pub text: String,
}
pub struct Prepared {
    pub case: Case,
    pub status: u16,
    pub normalized: String,
    pub groups: Vec<Group>,
    pub or: bool,
    pub pds: Option<String>,
    pub preferred: Option<String>,
    pub first: bool,
    pub boost: Exact,
}
impl Prepared {
    pub fn new(case: Case) -> Result<Self, String> {
        let mut out = Self {
            case,
            status: 200,
            normalized: String::new(),
            groups: vec![],
            or: false,
            pds: None,
            preferred: None,
            first: false,
            boost: Exact::from_f32(0.0),
        };
        if out.case.q.trim().is_empty()
            || out.case.q.len() > 512
            || !(1..=100).contains(&out.case.limit)
        {
            out.status = 400;
            return Ok(out);
        }
        if let Some(value) = &out.case.pds {
            match ffi::host(value) {
                Ok(host) => out.pds = Some(host),
                Err(_) => {
                    out.status = 400;
                    return Ok(out);
                }
            }
        }
        if let Some(value) = &out.case.prefer_pds {
            match ffi::host(value) {
                Ok(host) => out.preferred = Some(host),
                Err(_) => {
                    out.status = 400;
                    return Ok(out);
                }
            }
        }
        if out.case.boost.is_some_and(|v| !v.is_finite() || v < 0.0)
            || (out.preferred.is_none()
                && (out.case.preference.is_some() || out.case.boost.is_some()))
        {
            out.status = 400;
            return Ok(out);
        }
        match out
            .case
            .preference
            .as_deref()
            .unwrap_or(if out.case.boost.is_some() {
                "boost"
            } else {
                "pds_first"
            }) {
            "boost" if out.case.boost.is_some() && out.preferred.is_some() => {
                out.boost = Exact::from_f32(out.case.boost.unwrap())
            }
            "pds_first" if out.case.boost.is_none() => out.first = out.preferred.is_some(),
            _ => {
                out.status = 400;
                return Ok(out);
            }
        }
        let plan = ffi::plan(json!({"op":"query","q":out.case.q}))?;
        if plan["status"] == "empty" {
            return Ok(out);
        }
        if plan["status"] != "ok" {
            out.status = 400;
            return Ok(out);
        }
        out.normalized = plan["normalized"]
            .as_str()
            .ok_or("missing normalized query")?
            .into();
        out.or = plan["mode"] == "or";
        for group in plan["groups"].as_array().ok_or("missing query groups")? {
            out.groups.push(Group {
                cjk: group["kind"] == "cjk",
                text: group["text"].as_str().ok_or("missing group text")?.into(),
            });
        }
        Ok(out)
    }
    pub fn matches(&self, handle: &str, runs: &[(bool, &str)]) -> bool {
        if self.status != 200 || self.normalized.is_empty() {
            return false;
        }
        if self.or
            && (handle.starts_with(&self.normalized)
                || handle
                    .match_indices('.')
                    .any(|(i, _)| handle[i + 1..].starts_with(&self.normalized)))
        {
            return true;
        }
        !self.groups.is_empty()
            && self.groups.iter().all(|q| {
                runs.iter().any(|(cjk, text)| {
                    *cjk == q.cjk
                        && if !q.cjk || q.text.chars().count() == 1 {
                            text.starts_with(&q.text)
                        } else {
                            text.contains(&q.text)
                        }
                })
            })
    }
}
pub fn runs(value: &str) -> Vec<(bool, &str)> {
    let mut out = vec![];
    for token in value.split(' ').filter(|t| !t.is_empty()) {
        let mut start = 0;
        let mut kind = None;
        for (offset, ch) in token.char_indices() {
            let current = !ch.is_ascii();
            if kind.is_some_and(|old| old != current) {
                out.push((kind.unwrap(), &token[start..offset]));
                start = offset
            }
            kind = Some(current);
        }
        if let Some(cjk) = kind {
            out.push((cjk, &token[start..]))
        }
    }
    out
}
#[derive(Default)]
struct Node {
    children: HashMap<u8, usize>,
    ids: Vec<usize>,
}
pub struct Trie {
    nodes: Vec<Node>,
}
impl Trie {
    fn new() -> Self {
        Self {
            nodes: vec![Node::default()],
        }
    }
    fn insert(&mut self, text: &str, id: usize) {
        let mut at = 0;
        for b in text.bytes() {
            at = if let Some(next) = self.nodes[at].children.get(&b) {
                *next
            } else {
                let next = self.nodes.len();
                self.nodes.push(Node::default());
                self.nodes[at].children.insert(b, next);
                next
            };
        }
        self.nodes[at].ids.push(id);
    }
    fn lookup(&self, text: &str, mask: &mut [u64; 8]) {
        let mut at = 0;
        for b in text.bytes() {
            let Some(next) = self.nodes[at].children.get(&b) else {
                break;
            };
            at = *next;
            for id in &self.nodes[at].ids {
                mask[*id / 64] |= 1 << (*id % 64)
            }
        }
    }
}
pub struct Dispatch {
    handles: Trie,
    latin: Trie,
    cjk: HashMap<String, Vec<usize>>,
    single: HashMap<String, Vec<usize>>,
}
fn first(value: &str, n: usize) -> &str {
    let end = value.char_indices().nth(n).map_or(value.len(), |(i, _)| i);
    &value[..end]
}
impl Dispatch {
    pub fn new(queries: &[Prepared]) -> Self {
        let mut out = Self {
            handles: Trie::new(),
            latin: Trie::new(),
            cjk: HashMap::new(),
            single: HashMap::new(),
        };
        for (id, q) in queries
            .iter()
            .enumerate()
            .filter(|(_, q)| q.status == 200 && !q.normalized.is_empty())
        {
            if q.or {
                out.handles.insert(&q.normalized, id)
            }
            if let Some(anchor) = q
                .groups
                .iter()
                .max_by_key(|g| (g.text.chars().count(), !g.cjk))
            {
                if !anchor.cjk {
                    out.latin.insert(&anchor.text, id)
                } else if anchor.text.chars().count() == 1 {
                    out.single.entry(anchor.text.clone()).or_default().push(id)
                } else {
                    out.cjk
                        .entry(first(&anchor.text, 2).to_owned())
                        .or_default()
                        .push(id)
                }
            }
        }
        out
    }
    pub fn candidates(&self, handle: &str, runs: &[(bool, &str)]) -> [u64; 8] {
        let mut mask = [0; 8];
        self.handles.lookup(handle, &mut mask);
        for (i, _) in handle.match_indices('.') {
            self.handles.lookup(&handle[i + 1..], &mut mask)
        }
        for (cjk, text) in runs {
            if !cjk {
                self.latin.lookup(text, &mut mask)
            } else {
                if let Some(ids) = self.single.get(first(text, 1)) {
                    for id in ids {
                        mask[*id / 64] |= 1 << (*id % 64)
                    }
                }
                for (i, _) in text.char_indices() {
                    if let Some(ids) = self.cjk.get(first(&text[i..], 2)) {
                        for id in ids {
                            mask[*id / 64] |= 1 << (*id % 64)
                        }
                    }
                }
            }
        }
        mask
    }
}
#[derive(Clone, Eq, PartialEq)]
pub struct Winner {
    pub exact: bool,
    pub first: bool,
    pub score: Exact,
    pub did: String,
}
impl Ord for Winner {
    fn cmp(&self, other: &Self) -> Ordering {
        self.exact
            .cmp(&other.exact)
            .then(self.first.cmp(&other.first))
            .then(self.score.cmp(&other.score))
            .then_with(|| other.did.cmp(&self.did))
    }
}
impl PartialOrd for Winner {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
pub type Heap = BinaryHeap<Reverse<Winner>>;
pub fn add(heap: &mut Heap, winner: Winner, limit: usize) {
    if heap.len() < limit {
        heap.push(Reverse(winner))
    } else if heap.peek().is_some_and(|old| winner > old.0) {
        *heap.peek_mut().unwrap() = Reverse(winner)
    }
}
pub fn evaluate(
    row: &Row,
    queries: &[Prepared],
    dispatch: &Dispatch,
    heaps: &mut [Heap],
    quarantine: &[String],
) -> Result<u64, String> {
    let projection = ffi::Projection::new(&row.handle, &row.display_name, &row.pds)?;
    evaluate_projected(row, &projection, queries, dispatch, heaps, quarantine)
}
pub fn evaluate_projected(
    row: &Row,
    projection: &ffi::Projection,
    queries: &[Prepared],
    dispatch: &Dispatch,
    heaps: &mut [Heap],
    quarantine: &[String],
) -> Result<u64, String> {
    if !row.quality_score.is_finite()
        || (!row.deleted && row.handle.is_empty())
        || row.pds.len() > 65535
    {
        return Err("invalid raw actor score/handle/PDS bounds".into());
    }
    if row.hidden
        || row.deleted
        || row.handle.to_ascii_lowercase().ends_with(".invalid")
        || projection.handle() == "handle.invalid"
        || quarantine.iter().any(|h| h == projection.handle())
    {
        return Ok(0);
    }
    let parts = runs(projection.display());
    let mask = dispatch.candidates(projection.handle(), &parts);
    let base = Exact::from_f32(row.quality_score);
    let mut matched = 0;
    for (word, bits) in mask.into_iter().enumerate() {
        let mut bits = bits;
        while bits != 0 {
            let id = word * 64 + bits.trailing_zeros() as usize;
            bits &= bits - 1;
            let q = &queries[id];
            if q.pds
                .as_deref()
                .is_some_and(|host| host != projection.host())
                || !q.matches(projection.handle(), &parts)
            {
                continue;
            }
            matched += 1;
            let local = q.preferred.as_deref() == Some(projection.host());
            let winner = Winner {
                exact: q.or && projection.handle() == q.normalized,
                first: q.first && local,
                score: if local { base.add(q.boost) } else { base },
                did: row.did.clone(),
            };
            add(&mut heaps[id], winner, q.case.limit);
        }
    }
    Ok(matched)
}
#[derive(Serialize)]
pub struct Answer<'a> {
    pub status: u16,
    pub ids: Vec<&'a str>,
    pub native_limitations: &'a [String],
    pub native_shared_features: bool,
}
pub fn answers<'a>(queries: &'a [Prepared], heaps: &'a [Heap]) -> BTreeMap<&'a str, Answer<'a>> {
    let mut out = BTreeMap::new();
    for (q, heap) in queries.iter().zip(heaps) {
        let mut winners = heap.iter().map(|w| &w.0).collect::<Vec<_>>();
        winners.sort_by(|a, b| b.cmp(a));
        out.insert(
            q.case.id.as_str(),
            Answer {
                status: q.status,
                ids: winners.iter().map(|w| w.did.as_str()).collect(),
                native_limitations: &q.case.native_limitations,
                native_shared_features: q.case.native_shared_features,
            },
        );
    }
    out
}
