# How to translate OnePage chunks (ru / zh-HK)

The key is **the English source string**. It never changes: if a translation is
missing, the interface shows the key itself, i.e. the English text. So a missing
string means "not translated yet", not a breakage.

Russian is an ordinary translation (`app/locales/ru.json`), exactly like zh-HK
(`app/locales/zh-HK.json`). English needs no translation: `en.json` maps every
key to itself.

## What you receive and what you return

Chunks are cut from `en.json` — `en-NN.json`:

```json
{
  "_meta": {"chunk": "07", "lang": "en",
            "source_files": ["static/js/data-quality.js"], "count": 148},
  "strings": {
    "Skipped {n}": {"ref": "static/js/data-quality.js:412"},
    "{n} check|{n} checks": {"ref": "…", "plural": true}
  }
}
```

Return `ru-NN.json` / `zh-HK-NN.json` of the same shape, where the value of each
key is **the translated string**:

```json
{
  "_meta": {"chunk": "07", "lang": "zh-HK"},
  "strings": {
    "Skipped {n}": "已略過 {n} 項",
    "{n} check|{n} checks": "{n} 項檢查"
  }
}
```

Merging: `python3 tools/i18n_merge.py --merge ru` (or `--merge zh-HK`). It
refuses a chunk with an unknown key, an invented `{parameter}` or the wrong
number of plural forms.

## Plurals

A key with `"plural": true` carries **both English forms separated by `|`**
(`{n} file|{n} files`: one / everything else). The translation carries as many
forms as **its own** language needs:

| language | forms | rule |
|---|---|---|
| en | 2 | `{n} file` / `{n} files` (the key) |
| ru | 3 | 1 / 2–4 / 5–20: `{n} файл|{n} файла|{n} файлов` |
| zh-HK | 1 | `{n} 個檔案`, no `|` |

`|` is forbidden in an ordinary (non-plural) key and in its translation.

## Parameters

`{n}`, `{name}`, `{p1}` are substituted by the code. Keep the name exactly;
you may — and should — change their order in the sentence. No parameter may be
removed or added.

## Glossary (mandatory)

| en | ru | zh-HK |
|---|---|---|
| report | отчёт | 報告 |
| generator | генератор | 生成器 |
| reference table (ADR-16) | справочник | 參考表 |
| quest | квест | 任務 |
| scope (ADR-18, RLS) | область | 範圍 |
| daily surprise / chest | сюрприз дня / сундук | 每日驚喜 / 寶箱 |
| streak | стрик | 連勝 |
| quiz | викторина | 問答 |
| folder | папка | 資料夾 |
| presentation | презентация | 簡報 |
| catalog (inferred tables) | каталог | 目錄 |
| lake | озеро | 數據湖 |
| observation | наблюдение | 觀測 |
| run | прогон | 執行 |
| version | версия | 版本 |
| administrator / superuser / visitor | администратор / суперпользователь / посетитель | 管理員 / 超級用戶 / 訪客 |

## Rules

1. **Do not translate:** `OnePage`, `Data Quality` as the name of the section,
   `Greenplum`, `DuckDB`, `Parquet`, `SAML`, `person_id`, `UPN`, action and
   status codes (`explore_intent`, `check_fail`), file and field names
   (`semantics.json`, `v_employees`), SQL, `⌘K`, letter hotkeys (`G C`).
2. **zh-HK — traditional characters, Hong Kong vocabulary:** 資料夾 (not 文件夾),
   檔案 (not 文件), 設定 (not 設置), 登入 (not 登錄), 儲存 (not 保存).
   Full-width punctuation (，。：（）), but ASCII inside `{}`, codes and numbers.
   Do not add English terms in brackets.
3. **ru — ordinary Russian typography:** «guillemets» for quotes, the letter
   yo (U+0451) where it belongs, sentence case for buttons and headings (the
   English source is written in British spelling and sentence case too).
4. **Length:** a button is ≤ 1.3× the length of the English source. If there
   is no other way, add `"_long": ["key", …]` to the chunk and the layout
   person will take a look.
5. One string = one sentence. Do not move meaning between keys and do not
   "improve" the English source: changes to the English go separately and
   change the key.
6. **API errors** — neutral tone, no exclamation marks; in ru without a
   "please" word, in zh polite, but without 請 at the start of every phrase.
7. Keys with HTML inside (`{s}…{s_}`) mark where tags are inserted. The text
   between them is translated; `{s}`/`{s_}` themselves stay in place around
   the same words.
8. A value equal to its key is reported by `--merge` as "not translated yet",
   not as an error — leave a string untranslated rather than guessing.
