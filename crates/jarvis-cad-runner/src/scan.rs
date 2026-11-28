//! Reads of the engine's `inspect_drawing` and `find_text` texts that build nothing but what the
//! answer keeps.
//!
//! Parsing up to 4 MiB of untrusted JSON into a `serde_json::Value` can cost 30 times its size
//! (`[0,0,...]` is 32 bytes a number): measured 120 MB of peak memory against a 256 MB
//! container. These walk the text once with serde visitors instead and keep only the counts,
//! the extents and the texts.
//!
//! The rules are those of the slice 0 test (`summarise`, `find_extents` in
//! `spikes/cad-engine/draw_test.py`), as the first version of this runner applied them to a
//! `Value` (whose objects iterate in sorted key order): the `counts` object at the top (its 64
//! smallest keys); the extents from the first key naming "extent" whose value is a min and a max
//! point, looked for at each level of objects (not lists) before going deeper, keys in sorted
//! order, at most 32 levels down.

use std::collections::BTreeMap;
use std::fmt;

use serde::de::{DeserializeSeed, Deserializer, IgnoredAny, MapAccess, SeqAccess, Visitor};

use crate::replay::one_line;

/// Objects deeper than this are not searched for extents.
const MAX_EXTENT_DEPTH: usize = 32;
/// The `counts` entries read.
const MAX_COUNT_KINDS: usize = 64;

/// The parts of an `inspect_drawing` reply the answer carries.
#[derive(Debug, PartialEq)]
pub struct Summary {
    pub counts: BTreeMap<String, u64>,
    pub items: u64,
    pub extents: Option<[f64; 2]>,
}

/// The counts and extents of an `inspect_drawing` text (nothing for text that is not JSON).
pub fn summarise(inspect_text: &str) -> Summary {
    let scan = scan_top(inspect_text).unwrap_or_default();
    let mut counts = BTreeMap::new();
    for (kind, n) in scan.counts.unwrap_or_default() {
        let (Some(n), kind) = (n, one_line(&kind, 40)) else {
            continue;
        };
        if !kind.is_empty() {
            counts.insert(kind, n);
        }
    }
    let items = counts.values().fold(0u64, |sum, n| sum.saturating_add(*n));
    // Two finite numbers after rounding, or none (`1e308 * 10` is infinite, and serde_json
    // would write it as null).
    let extents = scan
        .found
        .map(|(w, h)| [(w * 10.0).round() / 10.0, (h * 10.0).round() / 10.0])
        .filter(|e| e.iter().all(|n| n.is_finite()));
    Summary {
        counts,
        items,
        extents,
    }
}

/// The extents `(width, height)` in a JSON text, by the rules above.
pub fn find_extents(text: &str) -> Option<(f64, f64)> {
    scan_top(text).ok()?.found
}

fn scan_top(text: &str) -> Result<Scan, serde_json::Error> {
    let mut de = serde_json::Deserializer::from_str(text);
    let scan = Node {
        depth: 0,
        counts: false,
    }
    .deserialize(&mut de)?;
    de.end()?;
    Ok(scan)
}

/// What one JSON value is, as far as the rules need.
#[derive(Debug, Default)]
struct Scan {
    number: Option<f64>,
    /// `{x, y}` or `[x, y, ...]`, finite.
    point: Option<(f64, f64)>,
    /// `{min, max}` or `[point, point]`: (width, height), finite.
    pair: Option<(f64, f64)>,
    /// The extents found inside this object.
    found: Option<(f64, f64)>,
    /// This object's entries, for the top-level `counts`.
    counts: Option<BTreeMap<String, Option<u64>>>,
    whole: Option<u64>,
}

fn finite_point(x: Option<f64>, y: Option<f64>) -> Option<(f64, f64)> {
    let (x, y) = (x?, y?);
    (x.is_finite() && y.is_finite()).then_some((x, y))
}

fn span(low: Option<(f64, f64)>, high: Option<(f64, f64)>) -> Option<(f64, f64)> {
    let (low, high) = (low?, high?);
    let (w, h) = ((high.0 - low.0).abs(), (high.1 - low.1).abs());
    (w.is_finite() && h.is_finite()).then_some((w, h))
}

/// Keeps the entry with the smallest key (the first a sorted object would give).
fn keep_first(slot: &mut Option<(String, (f64, f64))>, key: &str, value: Option<(f64, f64)>) {
    if let Some(value) = value {
        if slot.as_ref().is_none_or(|(k, _)| key < k.as_str()) {
            *slot = Some((key.to_string(), value));
        }
    }
}

/// One value at `depth`; `counts` when it is the top-level `counts` object.
#[derive(Clone, Copy)]
struct Node {
    depth: usize,
    counts: bool,
}

impl<'de> DeserializeSeed<'de> for Node {
    type Value = Scan;
    fn deserialize<D: Deserializer<'de>>(self, d: D) -> Result<Scan, D::Error> {
        d.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for Node {
    type Value = Scan;

    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("any JSON value")
    }

    fn visit_u64<E>(self, n: u64) -> Result<Scan, E> {
        Ok(Scan {
            number: Some(n as f64),
            whole: Some(n),
            ..Scan::default()
        })
    }

    fn visit_i64<E>(self, n: i64) -> Result<Scan, E> {
        Ok(Scan {
            number: Some(n as f64),
            whole: u64::try_from(n).ok(),
            ..Scan::default()
        })
    }

    fn visit_f64<E>(self, n: f64) -> Result<Scan, E> {
        Ok(Scan {
            number: Some(n),
            ..Scan::default()
        })
    }

    fn visit_str<E>(self, _: &str) -> Result<Scan, E> {
        Ok(Scan::default())
    }

    fn visit_bool<E>(self, _: bool) -> Result<Scan, E> {
        Ok(Scan::default())
    }

    fn visit_unit<E>(self) -> Result<Scan, E> {
        Ok(Scan::default())
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Scan, A::Error> {
        // Lists are not searched for extents, but may be a point or a pair of points.
        let child = Node {
            depth: self.depth + 1,
            counts: false,
        };
        let mut len = 0usize;
        let mut first = Scan::default();
        let mut second = Scan::default();
        while let Some(item) = seq.next_element_seed(child)? {
            match len {
                0 => first = item,
                1 => second = item,
                _ => {}
            }
            len += 1;
        }
        let point = if len >= 2 {
            finite_point(first.number, second.number)
        } else {
            None
        };
        let pair = if len == 2 {
            span(first.point, second.point)
        } else {
            None
        };
        Ok(Scan {
            point,
            pair,
            ..Scan::default()
        })
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Scan, A::Error> {
        let searched = self.depth <= MAX_EXTENT_DEPTH;
        let (mut x, mut y, mut min, mut max) = (None, None, None, None);
        let mut here: Option<(String, (f64, f64))> = None;
        let mut deeper: Option<(String, (f64, f64))> = None;
        // This object's entries, when it is the top-level `counts`; or, at the top, those of
        // the `counts` entry.
        let mut own: Option<BTreeMap<String, Option<u64>>> = self.counts.then(BTreeMap::new);
        let mut from_counts = None;
        while let Some(key) = map.next_key::<String>()? {
            let child = Node {
                depth: self.depth + 1,
                counts: self.depth == 0 && key == "counts",
            };
            let value = map.next_value_seed(child)?;
            match key.as_str() {
                "x" => x = value.number,
                "y" => y = value.number,
                "min" => min = value.point,
                "max" => max = value.point,
                _ => {}
            }
            if searched {
                if key.to_lowercase().contains("extent") {
                    keep_first(&mut here, &key, value.pair);
                }
                keep_first(&mut deeper, &key, value.found);
            }
            if child.counts {
                from_counts = value.counts;
            }
            if let Some(own) = own.as_mut() {
                own.insert(key, value.whole);
                if own.len() > MAX_COUNT_KINDS {
                    own.pop_last();
                }
            }
        }
        Ok(Scan {
            point: finite_point(x, y),
            pair: span(min, max),
            found: here.or(deeper).map(|(_, v)| v),
            counts: own.or(from_counts),
            ..Scan::default()
        })
    }
}

/// The drawing's texts from a `find_text` reply: a list of rows, or an object with a `texts`
/// list; each row's `text`, on one line of at most `chars` characters; the first `max`.
pub fn texts_of(find_text: &str, max: usize, chars: usize) -> Vec<String> {
    let mut de = serde_json::Deserializer::from_str(find_text);
    let top = TextsTop { max, chars };
    top.deserialize(&mut de)
        .and_then(|t| de.end().map(|()| t))
        .unwrap_or_default()
}

#[derive(Clone, Copy)]
struct TextsTop {
    max: usize,
    chars: usize,
}

impl<'de> DeserializeSeed<'de> for TextsTop {
    type Value = Vec<String>;
    fn deserialize<D: Deserializer<'de>>(self, d: D) -> Result<Vec<String>, D::Error> {
        d.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for TextsTop {
    type Value = Vec<String>;
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("a list of texts")
    }
    fn visit_seq<A: SeqAccess<'de>>(self, seq: A) -> Result<Vec<String>, A::Error> {
        Rows(self).visit_seq(seq)
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Vec<String>, A::Error> {
        let mut texts = Vec::new();
        while let Some(key) = map.next_key::<String>()? {
            if key == "texts" {
                texts = map.next_value_seed(Rows(self))?;
            } else {
                map.next_value::<IgnoredAny>()?;
            }
        }
        Ok(texts)
    }
    fn visit_str<E>(self, _: &str) -> Result<Vec<String>, E> {
        Ok(Vec::new())
    }
    fn visit_bool<E>(self, _: bool) -> Result<Vec<String>, E> {
        Ok(Vec::new())
    }
    fn visit_u64<E>(self, _: u64) -> Result<Vec<String>, E> {
        Ok(Vec::new())
    }
    fn visit_i64<E>(self, _: i64) -> Result<Vec<String>, E> {
        Ok(Vec::new())
    }
    fn visit_f64<E>(self, _: f64) -> Result<Vec<String>, E> {
        Ok(Vec::new())
    }
    fn visit_unit<E>(self) -> Result<Vec<String>, E> {
        Ok(Vec::new())
    }
}

/// A list of rows (anything else: no texts).
struct Rows(TextsTop);

impl<'de> DeserializeSeed<'de> for Rows {
    type Value = Vec<String>;
    fn deserialize<D: Deserializer<'de>>(self, d: D) -> Result<Vec<String>, D::Error> {
        d.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for Rows {
    type Value = Vec<String>;
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("a list of rows")
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Vec<String>, A::Error> {
        let mut texts = Vec::new();
        while let Some(row) = seq.next_element::<Row>()? {
            if let (Some(text), true) = (row.0, texts.len() < self.0.max) {
                texts.push(one_line(&text, self.0.chars));
            }
        }
        Ok(texts)
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Vec<String>, A::Error> {
        while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
        Ok(Vec::new())
    }
    fn visit_str<E>(self, _: &str) -> Result<Vec<String>, E> {
        Ok(Vec::new())
    }
    fn visit_bool<E>(self, _: bool) -> Result<Vec<String>, E> {
        Ok(Vec::new())
    }
    fn visit_u64<E>(self, _: u64) -> Result<Vec<String>, E> {
        Ok(Vec::new())
    }
    fn visit_i64<E>(self, _: i64) -> Result<Vec<String>, E> {
        Ok(Vec::new())
    }
    fn visit_f64<E>(self, _: f64) -> Result<Vec<String>, E> {
        Ok(Vec::new())
    }
    fn visit_unit<E>(self) -> Result<Vec<String>, E> {
        Ok(Vec::new())
    }
}

/// One row: its `text` if that is a string (a row of another shape: none).
struct Row(Option<String>);

impl<'de> serde::Deserialize<'de> for Row {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Row, D::Error> {
        d.deserialize_any(RowVisitor)
    }
}

struct RowVisitor;

impl<'de> Visitor<'de> for RowVisitor {
    type Value = Row;
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("a row")
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Row, A::Error> {
        let mut text = None;
        while let Some(key) = map.next_key::<String>()? {
            if key == "text" {
                text = map.next_value::<crate::engine::Lenient>()?.0;
            } else {
                map.next_value::<IgnoredAny>()?;
            }
        }
        Ok(Row(text))
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Row, A::Error> {
        while seq.next_element::<IgnoredAny>()?.is_some() {}
        Ok(Row(None))
    }
    fn visit_str<E>(self, _: &str) -> Result<Row, E> {
        Ok(Row(None))
    }
    fn visit_bool<E>(self, _: bool) -> Result<Row, E> {
        Ok(Row(None))
    }
    fn visit_u64<E>(self, _: u64) -> Result<Row, E> {
        Ok(Row(None))
    }
    fn visit_i64<E>(self, _: i64) -> Result<Row, E> {
        Ok(Row(None))
    }
    fn visit_f64<E>(self, _: f64) -> Result<Row, E> {
        Ok(Row(None))
    }
    fn visit_unit<E>(self) -> Result<Row, E> {
        Ok(Row(None))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn extents(v: serde_json::Value) -> Option<(f64, f64)> {
        find_extents(&v.to_string())
    }

    #[test]
    fn extents_like_the_slice_0_search() {
        let data = json!({"counts": {}, "drawing": {"extents": {"min": {"x": -1, "y": 2}, "max": [9, 7]}}});
        assert_eq!(extents(data), Some((10.0, 5.0)));
        let data = json!({"extmin": [0, 0], "entities": [{"extents": [[0, 0], [1, 1]]}]});
        assert_eq!(extents(data), None, "lists are not searched");
        assert_eq!(extents(json!({"extents": null})), None);
        assert_eq!(extents(json!({"extents": [[0, 0], [1, 1], [2, 2]]})), None);
        assert_eq!(
            extents(json!({"extents": [[0, 0, 9], [1, 1]]})),
            Some((1.0, 1.0))
        );
        let mut deep = json!({"extents": [[0, 0], [1, 1]]});
        for _ in 0..40 {
            deep = json!({ "d": deep });
        }
        assert_eq!(extents(deep), None);
        let mut ok = json!({"extents": [[0, 0], [1, 1]]});
        for _ in 0..32 {
            ok = json!({ "d": ok });
        }
        assert_eq!(extents(ok), Some((1.0, 1.0)));
    }

    #[test]
    fn this_level_first_then_deeper_in_key_order() {
        // `b` here beats anything deeper; of two at one level the smaller key wins.
        let data = json!({"a": {"extents": [[0, 0], [1, 1]]}, "b_extents": [[0, 0], [2, 2]]});
        assert_eq!(extents(data), Some((2.0, 2.0)));
        let data = json!({"z_extent": [[0, 0], [3, 3]], "a_extent": [[0, 0], [4, 4]]});
        assert_eq!(extents(data), Some((4.0, 4.0)));
        let data = json!({"z": {"extents": [[0, 0], [5, 5]]}, "m": {"extents": [[0, 0], [6, 6]]}});
        assert_eq!(extents(data), Some((6.0, 6.0)));
        // A key naming extent without a pair is passed over.
        let data = json!({"extents": 3, "inner": {"Extent": {"min": [1, 1], "max": [4, 5]}}});
        assert_eq!(extents(data), Some((3.0, 4.0)));
    }

    #[test]
    fn summary_keeps_only_whole_counts() {
        let s = summarise(
            r#"{"counts": {"Line": 5, "Text": 2, "Odd": -1, "Bad": "x", "Dimension": 2}}"#,
        );
        assert_eq!(s.items, 9);
        assert_eq!(s.counts.len(), 3);
        assert_eq!(s.extents, None);
        assert_eq!(summarise("not json").items, 0);
        assert_eq!(summarise(r#"{"counts": [1]}"#).items, 0);
        assert_eq!(summarise(r#"{"inner": {"counts": {"Line": 1}}}"#).items, 0);
        let many: serde_json::Map<String, serde_json::Value> =
            (0..100).map(|i| (format!("k{i:03}"), json!(1))).collect();
        let s = summarise(&json!({ "counts": many }).to_string());
        assert_eq!(s.counts.len(), 64);
        assert!(s.counts.contains_key("k000") && !s.counts.contains_key("k064"));
    }

    #[test]
    fn extents_are_finite_after_rounding_or_none() {
        let s = summarise(r#"{"extents": {"min": [0, 0], "max": [1e308, 5]}}"#);
        assert_eq!(s.extents, None);
        let s = summarise(r#"{"extents": {"min": [0, 0], "max": [12.34, 5]}}"#);
        assert_eq!(s.extents, Some([12.3, 5.0]));
    }

    #[test]
    fn texts_in_both_shapes() {
        let t = |s: &str| texts_of(s, 80, 80);
        assert_eq!(t(r#"{"count":1,"texts":[{"text":"A\nB"}]}"#), vec!["A B"]);
        assert_eq!(t(r#"[{"text":"C"},{"x":1},5,[1],{"text":7}]"#), vec!["C"]);
        assert!(t("3").is_empty());
        assert!(t(r#"{"texts": 3}"#).is_empty());
        assert!(t("[").is_empty());
        let rows: Vec<_> = (0..100).map(|i| json!({"text": format!("t{i}")})).collect();
        let got = texts_of(&json!(rows).to_string(), 80, 80);
        assert_eq!(got.len(), 80);
        assert_eq!(got[79], "t79");
    }
}
