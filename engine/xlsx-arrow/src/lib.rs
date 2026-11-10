//! `xlsx-arrow` — the OnePage /explore v2 Excel front end.
//!
//! Today /explore reads a workbook through Pyodide: python-calamine -> pandas
//! -> DuckDB's pandas scan, inside a ~200 MB wasm CPython heap. This crate is
//! the same first hop with the interpreter removed — calamine (the *same*
//! Rust parser python-calamine wraps) straight to an Arrow IPC stream that
//! duckdb-wasm ingests natively.
//!
//! JS surface (see `static/js/explore2/engine.js` for the only caller):
//!
//! ```js
//! import init, { sheet_names, parse_sheet } from '/static/xlsx-arrow/xlsx_arrow.js';
//! await init('/static/xlsx-arrow/xlsx_arrow_bg.wasm');
//! const names = sheet_names(bytes);                  // string[]
//! const ipc   = parse_sheet(bytes, 'Orders', 0);     // Uint8Array, Arrow IPC stream
//! ```
//!
//! `header_row` is 0-based *within the sheet's used range*, exactly like
//! pandas' `header=` argument in `_explore.py::_read_frame`.

pub mod norm;
pub mod sheet;

use wasm_bindgen::prelude::*;

/// Worksheet names, in workbook order.
#[wasm_bindgen]
pub fn sheet_names(bytes: &[u8]) -> Result<js_sys::Array, JsValue> {
    let names = sheet::sheet_names(bytes).map_err(|e| JsValue::from_str(&e))?;
    let out = js_sys::Array::new_with_length(names.len() as u32);
    for (i, n) in names.iter().enumerate() {
        out.set(i as u32, JsValue::from_str(n));
    }
    Ok(out)
}

/// One worksheet -> one Arrow IPC *stream* (schema message + one record batch
/// + EOS), ready for `AsyncDuckDBConnection.insertArrowFromIPCStream`.
///
/// `sheet` may be empty, meaning "the first sheet".
#[wasm_bindgen]
pub fn parse_sheet(bytes: &[u8], sheet: &str, header_row: u32) -> Result<Vec<u8>, JsValue> {
    sheet::parse_sheet(bytes, sheet, header_row).map_err(|e| JsValue::from_str(&e))
}

/// Build stamp, so a page can tell which parser it actually loaded.
#[wasm_bindgen]
pub fn version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}
