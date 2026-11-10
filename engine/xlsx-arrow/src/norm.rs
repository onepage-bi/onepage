//! Column-name normalisation — a port of `_explore.py::_norm_col`, which is
//! itself a port of `app.services.catalog.norm_col`.
//!
//! The three implementations MUST agree: the /explore query builder writes SQL
//! from catalogue column names, so a drift here is not an exception, it is a
//! silently wrong answer. Everything below is spelled out rather than
//! approximated, and `tests/` pins the cases that actually occur in 1C exports.

use unicode_normalization::UnicodeNormalization;

/// Reserved result columns. Named with a double-underscore prefix that
/// `norm_col` can never *produce* — but a sheet can still literally spell one,
/// which is why `header_names` renames such a header out of the way.
pub const PARTITION_COL: &str = "__partition";
pub const FILE_COL: &str = "__file";

/// Python's `str.casefold()` differs from Rust's `to_lowercase()` in a handful
/// of places. Only the ones that can plausibly reach a spreadsheet header are
/// reproduced; the rest are documented as a known divergence in docs/ENGINE2.md.
fn casefold(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        match ch {
            'ß' | 'ẞ' => out.push_str("ss"),
            'ς' => out.push('σ'),
            'ﬁ' => out.push_str("fi"),
            'ﬂ' => out.push_str("fl"),
            _ => {
                for lc in ch.to_lowercase() {
                    out.push(lc);
                }
            }
        }
    }
    out
}

/// `re.sub(r"\s+", " ", s)` over unicode whitespace.
fn collapse_ws(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut in_ws = false;
    for ch in s.chars() {
        if ch.is_whitespace() {
            in_ws = true;
        } else {
            if in_ws {
                out.push(' ');
                in_ws = false;
            }
            out.push(ch);
        }
    }
    if in_ws {
        out.push(' ');
    }
    out
}

/// `^unnamed:\s*\d+(\.\d+)?$` — pandas' placeholder for a blank header cell.
fn is_unnamed(key: &str) -> bool {
    let rest = match key.strip_prefix("unnamed:") {
        Some(r) => r.trim_start(),
        None => return false,
    };
    let (head, tail) = match rest.split_once('.') {
        Some((h, t)) => (h, Some(t)),
        None => (rest, None),
    };
    if head.is_empty() || !head.chars().all(|c| c.is_ascii_digit()) {
        return false;
    }
    match tail {
        None => true,
        Some(t) => !t.is_empty() && t.chars().all(|c| c.is_ascii_digit()),
    }
}

/// `re.sub(r"\.\d+$", "", key)` — undoes pandas' `.1` duplicate-header tail.
fn strip_dedupe_suffix(key: &str) -> &str {
    match key.rfind('.') {
        Some(i) => {
            let tail = &key[i + 1..];
            if !tail.is_empty() && tail.chars().all(|c| c.is_ascii_digit()) {
                &key[..i]
            } else {
                key
            }
        }
        None => key,
    }
}

/// One Excel header -> the catalogue's canonical key, or `None` if the header
/// is an artefact (blank, or pandas' `Unnamed: 7`).
pub fn norm_col(raw: &str) -> Option<String> {
    let nfc: String = raw.nfc().collect();
    let key = collapse_ws(&casefold(nfc.trim()));
    if key.is_empty() {
        return None;
    }
    if is_unnamed(&key) {
        return None;
    }
    let key = strip_dedupe_suffix(&key).trim().to_string();
    if key.is_empty() {
        None
    } else {
        Some(key)
    }
}

/// Rename a sheet's raw headers to catalogue keys — collision-safe, in order.
///
/// Three failure modes, all silent if unhandled: a header the catalogue drops
/// as an artefact, two headers that normalise to the same key, and a header
/// that literally spells a reserved column. Each gets a distinct, stable, still
/// queryable name (`col_3`, `key__2`, `__partition__2`) instead of being
/// dropped — data a user can see in Excel must be reachable from SQL.
pub fn header_names(raw: &[String]) -> Vec<String> {
    let mut used: Vec<String> = vec![PARTITION_COL.to_string(), FILE_COL.to_string()];
    let mut out = Vec::with_capacity(raw.len());
    for (i, r) in raw.iter().enumerate() {
        let mut key = norm_col(r).unwrap_or_else(|| format!("col_{}", i + 1));
        if used.iter().any(|u| u == &key) {
            let mut n = 2usize;
            loop {
                let cand = format!("{}__{}", key, n);
                if !used.iter().any(|u| u == &cand) {
                    key = cand;
                    break;
                }
                n += 1;
            }
        }
        used.push(key.clone());
        out.push(key);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nfc_strip_casefold_collapse() {
        assert_eq!(norm_col("  \u{41d}\u{43e}\u{43c}\u{435}\u{440}   \u{417}\u{430}\u{43a}\u{430}\u{437}\u{430}  ").unwrap(), "\u{43d}\u{43e}\u{43c}\u{435}\u{440} \u{437}\u{430}\u{43a}\u{430}\u{437}\u{430}");
        assert_eq!(norm_col("SUM\tTotal").unwrap(), "sum total");
    }

    #[test]
    fn artefacts_drop() {
        assert!(norm_col("").is_none());
        assert!(norm_col("   ").is_none());
        assert!(norm_col("Unnamed: 3").is_none());
        assert!(norm_col("unnamed: 12.1").is_none());
        assert!(norm_col("unnamed: x").is_some());
    }

    #[test]
    fn dedupe_tail_undone() {
        assert_eq!(norm_col("\u{414}\u{430}\u{442}\u{430}.1").unwrap(), "\u{434}\u{430}\u{442}\u{430}");
        assert_eq!(norm_col("v1.2.3").unwrap(), "v1.2");
        assert_eq!(norm_col("ver.x").unwrap(), "ver.x");
    }

    #[test]
    fn duplicates_get_suffixes() {
        let got = header_names(&[
            "\u{414}\u{430}\u{442}\u{430}".into(),
            "\u{434}\u{430}\u{442}\u{430}".into(),
            "\u{414}\u{410}\u{422}\u{410}".into(),
            "".into(),
            "__partition".into(),
        ]);
        assert_eq!(got, vec!["\u{434}\u{430}\u{442}\u{430}", "\u{434}\u{430}\u{442}\u{430}__2", "\u{434}\u{430}\u{442}\u{430}__3", "col_4", "__partition__2"]);
    }
}
