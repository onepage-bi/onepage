//! One worksheet -> one Arrow `RecordBatch`, with the column typing rules of
//! `_explore.py` (`_coerce_time_cols` + `_coerce_mixed_cols` + whatever DuckDB
//! then inferred from the pandas frame) reproduced directly on calamine cells.
//!
//! The Python path got here in three hops (calamine -> pandas object column ->
//! coercion pass -> DuckDB type inference). We collapse that to one decision
//! per column, but the *outcome* has to be the same, because /explore's query
//! builder and the semantic layer both assume it:
//!
//! | what the column holds (ignoring blanks/empties) | type      |
//! |-------------------------------------------------|-----------|
//! | whole numbers only                              | Int64     |
//! | numbers only, at least one fractional           | Float64   |
//! | date/datetime cells only                        | Timestamp(ms) |
//! | booleans only                                   | Boolean   |
//! | anything else, including any mix                | Utf8      |
//!
//! Blank-ish cells (`Empty`, and strings that are whitespace only — 1C writes
//! `' '` where a number is missing) are NULL in every case, which is the whole
//! reason `_coerce_mixed_cols` existed.
//!
//! Time-of-day cells are text, not TIME: a partition that read as times and a
//! partition that read as datetimes must still union, and DuckDB has no
//! TIME -> TIMESTAMP cast. `"HH:MM:SS"` is stable across partitions, lexically
//! sortable, and castable back inside a query.

use std::io::Cursor;
use std::sync::Arc;

use arrow_array::builder::{
    BooleanBuilder, Float64Builder, Int64Builder, StringBuilder, TimestampMillisecondBuilder,
};
use arrow_array::{ArrayRef, RecordBatch};
use arrow_schema::{DataType, Field, Schema, TimeUnit};
use calamine::{open_workbook_auto_from_rs, Data, Range, Reader, Sheets};

use crate::norm::header_names;

pub type Res<T> = Result<T, String>;

// ── Excel serial -> epoch millis (chrono-free) ───────────────────────────
/// Howard Hinnant's days-from-civil. Valid for any proleptic Gregorian date,
/// which is more than Excel can express.
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn excel_dt_millis(dt: &calamine::ExcelDateTime) -> i64 {
    let (y, mo, d, h, mi, s, ms) = dt.to_ymd_hms_milli();
    days_from_civil(y as i64, mo as i64, d as i64) * 86_400_000
        + (h as i64) * 3_600_000
        + (mi as i64) * 60_000
        + (s as i64) * 1_000
        + ms as i64
}

/// `"HH:MM:SS"` from an Excel serial fraction (or a `[hh]:mm:ss` duration,
/// which may legitimately exceed 24 h and is not wrapped).
fn serial_to_hms(value: f64) -> String {
    let total = (value * 86_400.0).round() as i64;
    let neg = total < 0;
    let t = total.abs();
    let s = format!("{:02}:{:02}:{:02}", t / 3600, (t % 3600) / 60, t % 60);
    if neg {
        format!("-{}", s)
    } else {
        s
    }
}

/// The ISO strings only ODS produces. `Some(millis)` if it looks like a real
/// date/datetime, `None` if we should just keep the text.
fn iso_millis(s: &str) -> Option<i64> {
    let b = s.as_bytes();
    if b.len() < 10 || b[4] != b'-' || b[7] != b'-' {
        return None;
    }
    let y: i64 = s[0..4].parse().ok()?;
    let mo: i64 = s[5..7].parse().ok()?;
    let d: i64 = s[8..10].parse().ok()?;
    let mut ms = days_from_civil(y, mo, d) * 86_400_000;
    if b.len() >= 19 && (b[10] == b'T' || b[10] == b' ') {
        let h: i64 = s[11..13].parse().ok()?;
        let mi: i64 = s[14..16].parse().ok()?;
        let sec: i64 = s[17..19].parse().ok()?;
        ms += h * 3_600_000 + mi * 60_000 + sec * 1_000;
    }
    Some(ms)
}

fn millis_to_iso(ms: i64) -> String {
    // Inverse of days_from_civil, again after Hinnant.
    let days = ms.div_euclid(86_400_000);
    let rem = ms.rem_euclid(86_400_000);
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
        y,
        m,
        d,
        rem / 3_600_000,
        (rem % 3_600_000) / 60_000,
        (rem % 60_000) / 1_000
    )
}

/// `str(v)` as pandas would have printed it into a mixed (object -> text)
/// column. Integral floats print without a fraction because python-calamine
/// hands pandas a python `int` for them.
fn num_to_text(v: f64) -> String {
    if v.fract() == 0.0 && v.abs() < 9.0e15 {
        format!("{}", v as i64)
    } else {
        let mut s = format!("{}", v);
        if s.ends_with(".0") {
            s.truncate(s.len() - 2);
        }
        s
    }
}

// ── Per-cell classification ──────────────────────────────────────────────
#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Null,
    Num,
    Date,
    Bool,
    Text,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ColType {
    Int64,
    Float64,
    Timestamp,
    Boolean,
    Utf8,
}

fn kind_of(c: &Data) -> Kind {
    match c {
        Data::Empty => Kind::Null,
        Data::Int(_) | Data::Float(_) => Kind::Num,
        Data::Bool(_) => Kind::Bool,
        Data::String(s) => {
            if s.trim().is_empty() {
                Kind::Null
            } else {
                Kind::Text
            }
        }
        // A time-of-day / duration cell is text (see the module doc); only a
        // real date or datetime is a Date.
        Data::DateTime(dt) => {
            if dt.is_duration() || dt.as_f64() < 1.0 {
                Kind::Text
            } else {
                Kind::Date
            }
        }
        Data::DateTimeIso(s) => {
            if iso_millis(s).is_some() {
                Kind::Date
            } else {
                Kind::Text
            }
        }
        Data::DurationIso(_) => Kind::Text,
        // `#N/A` and friends: pandas keeps the marker string, so do we.
        Data::Error(_) => Kind::Text,
    }
}

fn cell_text(c: &Data) -> Option<String> {
    match c {
        Data::Empty => None,
        Data::Int(i) => Some(i.to_string()),
        Data::Float(f) => Some(num_to_text(*f)),
        Data::Bool(b) => Some(if *b { "True".into() } else { "False".into() }),
        Data::String(s) => {
            if s.trim().is_empty() {
                None
            } else {
                Some(s.clone())
            }
        }
        Data::DateTime(dt) => Some(if dt.is_duration() || dt.as_f64() < 1.0 {
            serial_to_hms(dt.as_f64())
        } else {
            millis_to_iso(excel_dt_millis(dt))
        }),
        Data::DateTimeIso(s) => Some(match iso_millis(s) {
            Some(ms) => millis_to_iso(ms),
            None => s.clone(),
        }),
        Data::DurationIso(s) => Some(s.clone()),
        Data::Error(e) => Some(format!("{:?}", e)),
    }
}

fn cell_num(c: &Data) -> Option<f64> {
    match c {
        Data::Int(i) => Some(*i as f64),
        Data::Float(f) => Some(*f),
        _ => None,
    }
}

fn cell_millis(c: &Data) -> Option<i64> {
    match c {
        Data::DateTime(dt) => Some(excel_dt_millis(dt)),
        Data::DateTimeIso(s) => iso_millis(s),
        _ => None,
    }
}

fn col_type(kinds: &[Kind]) -> ColType {
    let mut num = false;
    let mut date = false;
    let mut boolean = false;
    let mut text = false;
    for k in kinds {
        match k {
            Kind::Null => {}
            Kind::Num => num = true,
            Kind::Date => date = true,
            Kind::Bool => boolean = true,
            Kind::Text => text = true,
        }
    }
    match (num, date, boolean, text) {
        (true, false, false, false) => ColType::Float64,
        (false, true, false, false) => ColType::Timestamp,
        (false, false, true, false) => ColType::Boolean,
        // All-null columns get VARCHAR: a NULL-typed Arrow column would make
        // DuckDB's union reconciliation pick a type per partition.
        _ => ColType::Utf8,
    }
}

// ── Sheet -> RecordBatch ─────────────────────────────────────────────────
pub fn open(bytes: &[u8]) -> Res<Sheets<Cursor<Vec<u8>>>> {
    open_workbook_auto_from_rs(Cursor::new(bytes.to_vec()))
        .map_err(|e| format!("cannot open workbook: {e}"))
}

pub fn sheet_names(bytes: &[u8]) -> Res<Vec<String>> {
    Ok(open(bytes)?.sheet_names())
}

pub fn range_to_batch(range: &Range<Data>, header_row: u32) -> Res<RecordBatch> {
    let rows: Vec<&[Data]> = range.rows().collect();
    let hdr = header_row as usize;
    if rows.is_empty() {
        return Err("sheet is empty".into());
    }
    if hdr >= rows.len() {
        return Err(format!(
            "header row {} is outside the sheet ({} rows)",
            hdr + 1,
            rows.len()
        ));
    }
    let raw_headers: Vec<String> = rows[hdr]
        .iter()
        .map(|c| cell_text(c).unwrap_or_default())
        .collect();
    let names = header_names(&raw_headers);
    let ncols = names.len();
    let body = &rows[hdr + 1..];

    let mut fields = Vec::with_capacity(ncols);
    let mut arrays: Vec<ArrayRef> = Vec::with_capacity(ncols);

    for (j, name) in names.iter().enumerate() {
        let kinds: Vec<Kind> = body
            .iter()
            .map(|r| r.get(j).map(kind_of).unwrap_or(Kind::Null))
            .collect();
        const EMPTY: &Data = &Data::Empty;
        let cells = || body.iter().map(move |r| r.get(j).unwrap_or(EMPTY));
        let mut t = col_type(&kinds);
        if t == ColType::Float64 && cells().all(|c| match cell_num(c) {
            // `pd.to_numeric` gives int64 when every value is a whole number,
            // and the shipped engine's DuckDB then sees BIGINT. Matching that
            // keeps `sum()` an integer for the columns where it was one.
            Some(v) => v.fract() == 0.0 && v.abs() < 9.0e15,
            None => true,
        }) {
            t = ColType::Int64;
        }

        let (dt, arr): (DataType, ArrayRef) = match t {
            ColType::Int64 => {
                let mut b = Int64Builder::with_capacity(body.len());
                for c in cells() {
                    b.append_option(cell_num(c).map(|v| v as i64));
                }
                (DataType::Int64, Arc::new(b.finish()))
            }
            ColType::Float64 => {
                let mut b = Float64Builder::with_capacity(body.len());
                for c in cells() {
                    b.append_option(cell_num(c));
                }
                (DataType::Float64, Arc::new(b.finish()))
            }
            ColType::Timestamp => {
                let mut b = TimestampMillisecondBuilder::with_capacity(body.len());
                for c in cells() {
                    b.append_option(cell_millis(c));
                }
                (
                    DataType::Timestamp(TimeUnit::Millisecond, None),
                    Arc::new(b.finish()),
                )
            }
            ColType::Boolean => {
                let mut b = BooleanBuilder::with_capacity(body.len());
                for c in cells() {
                    b.append_option(match c {
                        Data::Bool(v) => Some(*v),
                        _ => None,
                    });
                }
                (DataType::Boolean, Arc::new(b.finish()))
            }
            ColType::Utf8 => {
                let mut b = StringBuilder::with_capacity(body.len(), body.len() * 8);
                for c in cells() {
                    b.append_option(cell_text(c));
                }
                (DataType::Utf8, Arc::new(b.finish()))
            }
        };
        fields.push(Field::new(name, dt, true));
        arrays.push(arr);
    }

    let schema = Arc::new(Schema::new(fields));
    RecordBatch::try_new(schema, arrays).map_err(|e| format!("arrow: {e}"))
}

pub fn parse_sheet(bytes: &[u8], sheet: &str, header_row: u32) -> Res<Vec<u8>> {
    let mut wb = open(bytes)?;
    let name = if sheet.is_empty() {
        wb.sheet_names()
            .first()
            .cloned()
            .ok_or_else(|| "workbook has no sheets".to_string())?
    } else {
        sheet.to_string()
    };
    let range = wb
        .worksheet_range(&name)
        .map_err(|e| format!("sheet \"{name}\": {e}"))?;
    let batch = range_to_batch(&range, header_row)?;
    batch_to_ipc(&batch)
}

pub fn batch_to_ipc(batch: &RecordBatch) -> Res<Vec<u8>> {
    let mut out: Vec<u8> = Vec::with_capacity(1 << 20);
    {
        let mut w = arrow_ipc::writer::StreamWriter::try_new(&mut out, &batch.schema())
            .map_err(|e| format!("arrow ipc: {e}"))?;
        w.write(batch).map_err(|e| format!("arrow ipc: {e}"))?;
        w.finish().map_err(|e| format!("arrow ipc: {e}"))?;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_roundtrip() {
        let ms = days_from_civil(2026, 8, 22) * 86_400_000 + 13 * 3_600_000 + 45 * 60_000;
        assert_eq!(millis_to_iso(ms), "2026-08-22T13:45:00");
        assert_eq!(days_from_civil(1970, 1, 1), 0);
    }

    #[test]
    fn hms_from_fraction() {
        assert_eq!(serial_to_hms(0.5), "12:00:00");
        assert_eq!(serial_to_hms(1.25), "30:00:00");
    }

    #[test]
    fn numbers_print_like_python_calamine() {
        assert_eq!(num_to_text(5.0), "5");
        assert_eq!(num_to_text(5.25), "5.25");
    }

    #[test]
    fn types_follow_the_table() {
        use Kind::*;
        assert_eq!(col_type(&[Num, Null, Num]), ColType::Float64);
        assert_eq!(col_type(&[Date, Null]), ColType::Timestamp);
        assert_eq!(col_type(&[Date, Text]), ColType::Utf8);
        assert_eq!(col_type(&[Num, Text]), ColType::Utf8);
        assert_eq!(col_type(&[Null, Null]), ColType::Utf8);
    }

    #[test]
    fn iso_parsing() {
        assert_eq!(iso_millis("1970-01-02"), Some(86_400_000));
        assert!(iso_millis("not a date").is_none());
    }
}
