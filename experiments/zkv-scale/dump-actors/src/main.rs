use std::io::{self, BufWriter, Write};
use std::path::Path;
use std::process::ExitCode;

use tantivy::schema::Value;
use tantivy::{Index, ReloadPolicy, TantivyDocument};

fn dump(index_dir: &Path, out: &mut impl Write) -> Result<u64, Box<dyn std::error::Error>> {
    let index = Index::open_in_dir(index_dir)?;
    let actor = index.schema().get_field("actor")?;
    let searcher = index
        .reader_builder()
        .reload_policy(ReloadPolicy::Manual)
        .try_into()?
        .searcher();
    let mut count = 0u64;
    for segment in searcher.segment_readers() {
        let store = segment.get_store_reader(1)?;
        for document in store.iter::<TantivyDocument>(segment.alive_bitset()) {
            let document = document?;
            let json = document
                .get_first(actor)
                .and_then(|value| value.as_str())
                .ok_or("document has no stored actor")?;
            if json.contains('\n') {
                return Err("stored actor spans lines".into());
            }
            out.write_all(json.as_bytes())?;
            out.write_all(b"\n")?;
            count += 1;
        }
    }
    out.flush()?;
    Ok(count)
}

fn main() -> ExitCode {
    let mut args = std::env::args_os().skip(1);
    let (Some(index_dir), None) = (args.next(), args.next()) else {
        eprintln!("usage: dump-actors <index_dir>");
        return ExitCode::from(2);
    };
    let mut out = BufWriter::with_capacity(1 << 20, io::stdout().lock());
    match dump(Path::new(&index_dir), &mut out) {
        Ok(count) => {
            eprintln!("{count}");
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("dump-actors: {error}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::dump;
    use std::collections::BTreeSet;
    use tantivy::schema::{FAST, INDEXED, STORED, STRING, Schema};
    use tantivy::{Index, IndexSettings, IndexSortByField, Order, TantivyDocument, Term};

    fn actor(n: usize, deleted: bool) -> String {
        format!(
            r#"{{"did":"did:plc:fixture{n:04}","handle":"actor{n}.test","display_name":"Actor \"{n}\" 名前","avatar_url":"","labels":[],"associated":{{}},"created_at":"","pds":"https://pds.test","pds_host":"pds.test","quality_score":{n}.5,"version":{},"deleted":{deleted},"hidden":false,"followers_count":{n},"follows_count":0,"posts_count":0,"last_activity_at":0}}"#,
            1_700_000_000 + n
        )
    }

    #[test]
    fn round_trips_every_alive_document_across_segments() {
        let directory = tempfile::tempdir().unwrap();
        let mut builder = Schema::builder();
        let key = builder.add_text_field("key", STRING);
        let did = builder.add_text_field("did", STRING);
        let rank = builder.add_text_field("rank", FAST);
        let pds = builder.add_text_field("pds", STRING);
        let visible = builder.add_u64_field("visible", INDEXED);
        let stored = builder.add_text_field("actor", STORED);
        let mut settings = IndexSettings::default();
        settings.sort_by_field = Some(IndexSortByField {
            field: "rank".into(),
            order: Order::Asc,
        });
        let index = Index::builder()
            .schema(builder.build())
            .settings(settings)
            .create_in_dir(directory.path())
            .unwrap();
        let mut writer = index.writer_with_num_threads(1, 20_000_000).unwrap();
        writer.set_merge_policy(Box::new(tantivy::merge_policy::NoMergePolicy));
        let mut expected = BTreeSet::new();
        for n in 0..40 {
            let deleted = n % 13 == 0;
            let json = actor(n, deleted);
            let mut document = TantivyDocument::default();
            document.add_text(did, format!("did:plc:fixture{n:04}"));
            document.add_text(rank, format!("{n:08}"));
            document.add_text(pds, "pds.test");
            document.add_u64(visible, u64::from(!deleted));
            if !deleted {
                document.add_text(key, format!("actor{n}"));
            }
            document.add_text(stored, &json);
            writer.add_document(document).unwrap();
            expected.insert(json);
            if n % 10 == 9 {
                writer.commit().unwrap();
            }
        }
        writer.delete_term(Term::from_field_text(did, "did:plc:fixture0007"));
        writer.commit().unwrap();
        expected.remove(&actor(7, false));
        assert!(index.searchable_segments().unwrap().len() > 1);

        let mut out = Vec::new();
        let count = dump(directory.path(), &mut out).unwrap();
        let text = String::from_utf8(out).unwrap();
        let lines: BTreeSet<String> = text.lines().map(str::to_owned).collect();
        assert_eq!(count, 39);
        assert_eq!(text.lines().count(), 39);
        assert_eq!(lines, expected);
        assert!(lines.contains(&actor(13, true)));
    }
}
