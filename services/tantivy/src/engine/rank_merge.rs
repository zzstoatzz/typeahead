use crate::{Error, Result};
use std::{cmp::Reverse, collections::BinaryHeap};
use tantivy::{
    DocAddress, Searcher, TERMINATED,
    columnar::StrColumn,
    query::{Scorer, Weight},
};

pub(super) fn rank_key(rank: &StrColumn, doc: u32) -> Result<String> {
    let ordinal = rank
        .term_ords(doc)
        .next()
        .ok_or_else(|| Error::Storage("rank value missing".into()))?;
    let mut value = String::new();
    rank.ord_to_str(ordinal, &mut value)?;
    Ok(value)
}

pub(super) struct RankMerge<'a> {
    ranks: &'a [Option<StrColumn>],
    streams: Vec<Box<dyn Scorer>>,
    heads: BinaryHeap<Reverse<(String, usize, u32)>>,
    advance: Option<usize>,
    pub head_reads: usize,
}
impl<'a> RankMerge<'a> {
    /// `ranks` holds each segment's rank column in `segment_readers()` order.
    pub fn new(searcher: &Searcher, ranks: &'a [Option<StrColumn>], weight: &dyn Weight) -> Result<Self> {
        let mut merge = Self {
            ranks,
            streams: Vec::new(),
            heads: BinaryHeap::new(),
            advance: None,
            head_reads: 0,
        };
        for (ordinal, segment) in searcher.segment_readers().iter().enumerate() {
            if ranks[ordinal].is_none() {
                return Err(Error::Storage("rank column missing".into()));
            }
            let scorer = weight.scorer(segment, 1.0)?;
            if scorer.doc() != TERMINATED {
                merge.heads.push(Reverse((
                    rank_key(ranks[ordinal].as_ref().unwrap(), scorer.doc())?,
                    ordinal,
                    scorer.doc(),
                )));
                merge.head_reads += 1;
            }
            merge.streams.push(scorer);
        }
        Ok(merge)
    }
    pub fn next(&mut self) -> Result<Option<(String, DocAddress)>> {
        if let Some(ordinal) = self.advance.take() {
            let scorer = &mut self.streams[ordinal];
            scorer.advance();
            if scorer.doc() != TERMINATED {
                self.heads.push(Reverse((
                    rank_key(self.ranks[ordinal].as_ref().unwrap(), scorer.doc())?,
                    ordinal,
                    scorer.doc(),
                )));
                self.head_reads += 1;
            }
        }
        let Some(Reverse((key, ordinal, doc))) = self.heads.pop() else {
            return Ok(None);
        };
        self.advance = Some(ordinal);
        Ok(Some((key, DocAddress::new(ordinal as u32, doc))))
    }
}
