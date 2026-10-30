use levenshtein_automata::{Distance, LevenshteinAutomatonBuilder, DFA, SINK_STATE};
use memmap2::Mmap;
use serde::{Deserialize, Serialize};
#[cfg(unix)]
use std::os::unix::fs::FileExt;
use std::{
    collections::{BTreeSet, HashSet},
    fs::{self, File},
    io::{BufRead, BufReader, BufWriter, Write},
    path::Path,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
use tantivy_fst::{Automaton, IntoStreamer, Map, MapBuilder, Streamer};

struct Matcher<'a>(&'a DFA);
impl Automaton for Matcher<'_> {
    type State = u32;
    fn start(&self) -> u32 {
        self.0.initial_state()
    }
    fn is_match(&self, s: &u32) -> bool {
        matches!(self.0.distance(*s), Distance::Exact(_))
    }
    fn can_match(&self, s: &u32) -> bool {
        *s != SINK_STATE
    }
    fn accept(&self, s: &u32, b: u8) -> u32 {
        self.0.transition(*s, b)
    }
}
fn u16at(b: &[u8], p: usize) -> usize {
    u16::from_le_bytes(b[p..p + 2].try_into().unwrap()) as usize
}
fn u64at(b: &[u8], p: usize) -> u64 {
    u64::from_le_bytes(b[p..p + 8].try_into().unwrap())
}
fn mapfile(p: impl AsRef<Path>) -> Mmap {
    unsafe { Mmap::map(&File::open(p).unwrap()).unwrap() }
}
fn build(root: &Path) {
    let mut records = BufWriter::new(File::create(root.join("records.bin")).unwrap());
    let mut offsets = BufWriter::new(File::create(root.join("offsets.bin")).unwrap());
    let mut fst = MapBuilder::new(BufWriter::new(
        File::create(root.join("handles.fst")).unwrap(),
    ))
    .unwrap();
    let mut previous = String::new();
    let mut position = 0u64;
    let mut ordinal = 0u64;
    for line in BufReader::new(File::open(root.join("sorted.tsv")).unwrap()).lines() {
        let line = line.unwrap();
        let v: Vec<_> = line.split('\t').collect();
        assert_eq!(v.len(), 7);
        assert!(previous.as_str() <= v[0]);
        if previous != v[0] {
            fst.insert(v[0], position).unwrap();
            previous = v[0].into();
        }
        offsets.write_all(&position.to_le_bytes()).unwrap();
        let score: f32 = v[2].parse().unwrap();
        assert!(score.is_finite());
        let mut b = Vec::new();
        b.extend(score.to_le_bytes());
        b.extend(v[4].parse::<u64>().unwrap().to_le_bytes());
        b.extend(v[5].parse::<u32>().unwrap().to_le_bytes());
        b.push(v[6].parse::<u8>().unwrap());
        for s in [v[0], v[1], v[3]] {
            assert!(s.len() <= 65535);
            b.extend((s.len() as u16).to_le_bytes());
        }
        for s in [v[0], v[1], v[3]] {
            b.extend(s.as_bytes());
        }
        records.write_all(&b).unwrap();
        position += b.len() as u64;
        ordinal += 1;
    }
    records.flush().unwrap();
    offsets.flush().unwrap();
    fst.finish().unwrap();
    println!(
        "{}",
        serde_json::json!({"actors":ordinal,"files": (["records.bin","offsets.bin","handles.fst"].map(|s| (s,fs::metadata(root.join(s)).unwrap().len())))})
    );
}
struct Dictionary {
    records: Mmap,
    offsets: Option<Mmap>,
    fst: Option<Map<Mmap>>,
    profiles: File,
    overlay: File,
    typo_builder: LevenshteinAutomatonBuilder,
}
struct Row<'a> {
    handle: &'a [u8],
    did: &'a [u8],
    pds: &'a [u8],
    score: f32,
    offset: u64,
    length: usize,
    source: u8,
}
impl Dictionary {
    fn open(root: &Path, variant: &str, snapshot: &str) -> Self {
        Self {
            records: mapfile(root.join("records.bin")),
            offsets: if variant == "sorted" {
                Some(mapfile(root.join("offsets.bin")))
            } else {
                None
            },
            fst: if variant == "fst" {
                Some(Map::from(
                    tantivy_fst::raw::Fst::new(mapfile(root.join("handles.fst"))).unwrap(),
                ))
            } else {
                assert_eq!(variant, "sorted");
                None
            },
            profiles: File::open(snapshot).unwrap(),
            overlay: File::open(root.join("overlay-profiles.bin")).unwrap(),
            typo_builder: LevenshteinAutomatonBuilder::new(1, true),
        }
    }
    fn len(&self) -> usize {
        self.offsets
            .as_ref()
            .map(|m| m.len() / 8)
            .unwrap_or(self.records.len())
    }
    fn row(&self, i: usize) -> Row<'_> {
        let p = self
            .offsets
            .as_ref()
            .map(|m| u64at(m, i * 8) as usize)
            .unwrap_or(i);
        let b = &self.records[p..];
        let h = u16at(b, 17);
        let d = u16at(b, 19);
        let host = u16at(b, 21);
        Row {
            handle: &b[23..23 + h],
            did: &b[23 + h..23 + h + d],
            pds: &b[23 + h + d..23 + h + d + host],
            score: f32::from_le_bytes(b[..4].try_into().unwrap()),
            offset: u64at(b, 4),
            length: u32::from_le_bytes(b[12..16].try_into().unwrap()) as usize,
            source: b[16],
        }
    }
    fn lower(&self, key: &[u8]) -> usize {
        let (mut lo, mut hi) = (0, self.len());
        while lo < hi {
            let m = (lo + hi) / 2;
            if self.row(m).handle < key {
                lo = m + 1
            } else {
                hi = m
            }
        }
        lo
    }
    fn query(&self, c: &Case, cap: usize, hydrate: bool) -> Answer {
        let query = c.q.trim().trim_start_matches('@').to_ascii_lowercase();
        assert!(
            !query.is_empty()
                && query.len() <= 253
                && query
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b".-".contains(&b))
        );
        let mut ids = Vec::new();
        let mut seen = HashSet::new();
        let mut examined = 0;
        let mut truncated = false;
        let dfa = if c.fuzzy {
            assert!(query.len() >= 3 && query.len() <= 64);
            Some(self.typo_builder.build_prefix_dfa(&query))
        } else {
            None
        };
        let mut add = |i: usize| -> bool {
            if !seen.insert(i) {
                return true;
            }
            if examined >= cap {
                truncated = true;
                return false;
            }
            examined += 1;
            let row = self.row(i);
            if !c.pds.is_empty() && row.pds != c.pds.as_bytes() {
                return true;
            }
            let distance = if let Some(d) = &dfa {
                match d.eval(row.handle) {
                    Distance::Exact(n) => n,
                    _ => return true,
                }
            } else {
                0
            };
            ids.push((distance, i));
            true
        };
        if let Some(fst) = &self.fst {
            if let Some(d) = &dfa {
                let mut stream = fst.search(Matcher(d)).into_stream();
                'outer: while let Some((h, i)) = stream.next() {
                    let mut n = i as usize;
                    while n < self.len() && self.row(n).handle == h {
                        if !add(n) {
                            break 'outer;
                        }
                        let row = self.row(n);
                        n += 23 + row.handle.len() + row.did.len() + row.pds.len();
                    }
                }
            } else {
                let end = prefix_end(query.as_bytes());
                let mut stream = fst.range().ge(query.as_bytes()).lt(&end).into_stream();
                'outer: while let Some((h, i)) = stream.next() {
                    let mut n = i as usize;
                    while n < self.len() && self.row(n).handle == h {
                        if !add(n) {
                            break 'outer;
                        }
                        let row = self.row(n);
                        n += 23 + row.handle.len() + row.did.len() + row.pds.len();
                    }
                }
            }
        } else {
            let variants = if c.fuzzy {
                neighbors(query.as_bytes())
            } else {
                BTreeSet::from([query.as_bytes().to_vec()])
            };
            'outer: for prefix in variants {
                let mut i = self.lower(&prefix);
                while i < self.len() && self.row(i).handle.starts_with(&prefix) {
                    if !add(i) {
                        break 'outer;
                    }
                    i += 1;
                }
            }
        }
        ids.sort_unstable_by(|(da, ia), (db, ib)| {
            let a = self.row(*ia);
            let b = self.row(*ib);
            (b.handle == query.as_bytes())
                .cmp(&(a.handle == query.as_bytes()))
                .then_with(|| da.cmp(db))
                .then_with(|| b.score.total_cmp(&a.score))
                .then_with(|| a.did.cmp(b.did))
        });
        let mut dids = HashSet::new();
        ids.retain(|(_, i)| dids.insert(self.row(*i).did));
        ids.truncate(50);
        let mut bytes = 0;
        let mut checksum = 0u64;
        if hydrate {
            for (_, i) in &ids {
                let r = self.row(*i);
                let file = if r.source == 0 {
                    &self.profiles
                } else {
                    &self.overlay
                };
                let mut blob = vec![0; r.length];
                file.read_exact_at(&mut blob, r.offset).unwrap();
                assert_eq!(blob[0], 1);
                assert_eq!(blob[1], 0);
                bytes += blob.len();
                for b in blob {
                    checksum = checksum.wrapping_mul(31).wrapping_add(b as u64);
                }
            }
        }
        let hits = ids
            .iter()
            .take(10)
            .map(|(d, i)| Hit {
                did: String::from_utf8(self.row(*i).did.to_vec()).unwrap(),
                handle: String::from_utf8(self.row(*i).handle.to_vec()).unwrap(),
                distance: *d,
                score: self.row(*i).score,
            })
            .collect();
        Answer {
            hits,
            examined,
            truncated,
            profile_bytes: bytes,
            checksum,
        }
    }
}
fn prefix_end(q: &[u8]) -> Vec<u8> {
    let mut b = q.to_vec();
    *b.last_mut().unwrap() += 1;
    b
}
fn neighbors(q: &[u8]) -> BTreeSet<Vec<u8>> {
    let alphabet = b"abcdefghijklmnopqrstuvwxyz0123456789.-";
    let mut out = BTreeSet::new();
    out.insert(q.to_vec());
    for i in 0..q.len() {
        let mut d = q.to_vec();
        d.remove(i);
        if !d.is_empty() {
            out.insert(d);
        }
        for b in alphabet {
            let mut s = q.to_vec();
            s[i] = *b;
            out.insert(s);
        }
        if i + 1 < q.len() {
            let mut t = q.to_vec();
            t.swap(i, i + 1);
            out.insert(t);
        }
    }
    for i in 0..=q.len() {
        for b in alphabet {
            let mut v = q.to_vec();
            v.insert(i, *b);
            out.insert(v);
        }
    }
    out
}
#[derive(Clone, Serialize, Deserialize)]
struct Case {
    id: String,
    q: String,
    #[serde(default)]
    fuzzy: bool,
    #[serde(default)]
    pds: String,
}
#[derive(Debug, Serialize, Deserialize, PartialEq)]
struct Hit {
    did: String,
    handle: String,
    distance: u8,
    score: f32,
}
#[derive(Serialize, Deserialize)]
struct Answer {
    hits: Vec<Hit>,
    examined: usize,
    truncated: bool,
    profile_bytes: usize,
    checksum: u64,
}
#[derive(Serialize)]
struct Attempt {
    case: String,
    elapsed_ms: f64,
    service_ms: f64,
    queue_ms: f64,
    truncated: bool,
    examined: usize,
    checksum: u64,
    hits: Vec<Hit>,
}
fn memory() -> serde_json::Value {
    let s = fs::read_to_string("/proc/self/status").unwrap_or_default();
    serde_json::json!(s
        .lines()
        .filter(|s| s.starts_with("VmRSS:")
            || s.starts_with("VmHWM:")
            || s.starts_with("RssAnon:")
            || s.starts_with("RssFile:"))
        .collect::<Vec<_>>())
}
fn main() {
    let a: Vec<_> = std::env::args().collect();
    let root = Path::new(&a[2]);
    if a[1] == "build" {
        build(root);
        return;
    }
    let dictionary = Arc::new(Dictionary::open(root, &a[3], &a[4]));
    let cases: Vec<Case> = serde_json::from_reader(File::open(&a[5]).unwrap()).unwrap();
    let cap: usize = a.get(6).map(|s| s.parse().unwrap()).unwrap_or(20000);
    if a[1] == "check" {
        for c in cases {
            let result = dictionary.query(&c, cap, true);
            println!("{}", serde_json::json!({"case":c.id,"answer":result}));
        }
        return;
    }
    assert_eq!(a[1], "bench");
    let attempts: usize = a[7].parse().unwrap();
    let rps: f64 = a[8].parse().unwrap();
    let hydrate = a.get(9).map(|s| s == "hydrate").unwrap_or(true);
    let cases = Arc::new(cases);
    let index = Arc::new(AtomicUsize::new(0));
    let start = Instant::now();
    let mut threads = Vec::new();
    for _ in 0..4 {
        let (d, c, n) = (dictionary.clone(), cases.clone(), index.clone());
        threads.push(std::thread::spawn(move || {
            let mut rows = Vec::new();
            loop {
                let i = n.fetch_add(1, Ordering::Relaxed);
                if i >= attempts {
                    break;
                }
                let scheduled = start + Duration::from_secs_f64(i as f64 / rps);
                std::thread::sleep(scheduled.saturating_duration_since(Instant::now()));
                let begun = Instant::now();
                let case = &c[i % c.len()];
                let ans = d.query(case, cap, hydrate);
                let now = Instant::now();
                rows.push(Attempt {
                    case: case.id.clone(),
                    elapsed_ms: now.duration_since(scheduled).as_secs_f64() * 1000.,
                    service_ms: now.duration_since(begun).as_secs_f64() * 1000.,
                    queue_ms: begun.duration_since(scheduled).as_secs_f64() * 1000.,
                    truncated: ans.truncated,
                    examined: ans.examined,
                    checksum: ans.checksum,
                    hits: ans.hits,
                });
            }
            rows
        }));
    }
    let rows: Vec<_> = threads
        .into_iter()
        .flat_map(|t| t.join().unwrap())
        .collect();
    let mut times: Vec<_> = rows.iter().map(|r| r.elapsed_ms).collect();
    times.sort_by(|a, b| a.total_cmp(b));
    let p = |q: f64| times[((times.len() as f64 * q).ceil() as usize).saturating_sub(1)];
    println!(
        "{}",
        serde_json::json!({"variant":a[3],"hydrate":hydrate,"attempts":rows.len(),"p50_ms":p(0.5),"p95_ms":p(0.95),"p99_ms":p(0.99),"max_ms":times.last(),"wall_s":start.elapsed().as_secs_f64(),"truncated":rows.iter().filter(|r|r.truncated).count(),"memory":memory(),"rows":rows})
    );
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn edited_prefixes_agree_with_automaton() {
        let b = LevenshteinAutomatonBuilder::new(1, true);
        for q in ["alice", "alcie", "alce", "allice", "foo.bar", "aaa"] {
            let d = b.build_prefix_dfa(q);
            let n = neighbors(q.as_bytes());
            for h in [
                "alice.test",
                "alcie.test",
                "alce.test",
                "allice.test",
                "foo.bar.test",
                "fo.bar.test",
                "aaa.test",
                "aab.test",
                "zzz.test",
            ] {
                assert_eq!(
                    n.iter().any(|p| h.as_bytes().starts_with(p)),
                    matches!(d.eval(h.as_bytes()), Distance::Exact(_)),
                    "{q} {h}"
                );
            }
        }
    }
}
