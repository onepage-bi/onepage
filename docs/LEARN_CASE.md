# The "Delta" case — brief, reference answer and typical mistakes

A reference for anyone editing the preparatory course `/learn/prep`. Everything
below is REPRODUCIBLE from `static/learn/files/delta-sales.csv` — the file is
deterministic (`tools/learn_fixture_delta.py --check` verifies its sha256), so
the numbers in the course can be checked rather than remembered. These notes
used to live in `scratchpad/learn/` and were lost from there; here they are in
the repository.

## The task

The beverage distributor "Delta": 60 stores, 3 cities, 3 channels, 12 weeks,
deliveries on Tuesdays. Sales grew by 4 %, but write-offs grew by 31 %, and
"out of stock" complaints doubled. Both problems at once means the issue is not
the purchase volume but its distribution. The course question: **where to move
the volume**.

The main trap is **censored observation**: at a store with a stockout, sales
equal what was delivered, and by sales alone it is indistinguishable from a weak
store. The naive "take volume away from where it sells worst" hits exactly the
stores with a shortage.

## Reference answer

| What we calculate | Reference |
|---|---|
| Lost sales over 12 weeks | 163,548 btl |
| Share of actual sales | 10.2 % |
| Actual sales | 1,598,836 btl |
| Stores that ran out of stock | 14 of 60 |
| Top store by shortage | RV-02 — 19,657 btl, rank 33 of 60 by sales |
| Shortage stores in the bottom twenty by sales | 11 of 14 |
| Write-offs with stock on hand | 201,151 btl = 12.6 % of sales, at 46 stores |
| Overlap of "write-offs" and "stockouts" | 0 stores |

The last two rows are the second half of the answer: **the company has the
volume, it is just in the wrong place**. There is no need to buy more; it needs
to be moved.

The method (the sample report `static/learn/files/delta-report.html` implements
the same one): days without product (`sales = 0` AND `stock = 0`) are
multiplied by that store's usual daily rate for that item; the usual rate is
calculated over days with stock, the promotion week is excluded from the rate
base, the duplicated week is dropped by the key `date + store + item`, empty
rows (the store was closed) are not counted as days without product, and packs
are converted to bottles.

The conclusion is phrased as **"we lose at least 10.2 % under assumptions 1–4"**,
not "exactly that much": there is no demand in the data at all, so this is a
lower bound.

## Assumptions: the answer is a range, not a number

All four variants are calculated on the same export; only the definition of a
store's "usual rate" changes.

| What we take as the usual rate | Answer |
|---|---|
| Mean over days with stock, promotion week excluded (our choice) | 163,548 btl · 10.2 % |
| The same, but with the promotion week included | 175,010 btl · 10.9 % |
| Median instead of mean | 163,295 btl · 10.2 % |
| Only the days before a delivery (Sat–Mon) | **0** |

The third row is a robustness check: the median is insensitive to outliers, and
agreement within 0.2 % means the conclusion does not rest on a couple of odd
days. The fourth is an **unworkable assumption**: a store with a shortage has, by
definition, no stock on the days before a delivery, so there is nothing to
calculate from. The rule: an assumption is tested on the very rows it was
introduced for.

## Nine control numbers (the person works through them on their own, lesson `p4l2`)

| # | What we calculate | Value |
|---|---|---|
| 1 | Rows excluding the header | 21,840 |
| 2 | Unique by date+store+item | 20,160 (1,680 will be removed) |
| 3 | Full grid: 60 stores × 4 items × 84 days | 20,160 — matches item 2 |
| 4 | Empty sales cells | 20 = 5 days × 4 items |
| 5 | Sum of sales as is | 584,977 (warehouse shipments for the same period — 1,817,738) |
| 6 | After unit conversion (×12 for two cities) | 1,598,836 btl |
| 7 | Days with "sales = 0 and stock = 0" | 1,826 at 14 stores |
| 8 | Write-offs with stock > 0 | 201,151 btl at 46 stores |
| 9 | Weekly profile | one week 230,400, the rest 122,204–124,869 |

## Six findings in the export

| Finding | Numbers |
|---|---|
| Stockouts | 14 stores, sales ranks 33, 39, 40, 43, 44, 46, 47, 48, 49, 52, 54, 57, 58, 60 — none in the top half |
| Write-offs with stock on hand | 201,151 btl = 12.6 % of sales, 46 stores; no overlap with the stockouts; ALL of the top 20 by sales write off |
| Units of measure | in the file, "sales per store" is 24,640 (RV) against 2,450 (LS) and 2,158 (NF); the warehouse shipped 571,634 / 664,152 / 581,952 btl — so the cause is packs of 12, not the strength of the city |
| Duplicated week | week 10, exactly 1,680 extra rows = 60 stores × 4 items × 7 days |
| Promotion week | week 7: 230,400 btl against 124,600 on average (×1.85); the other 11 weeks differ by less than 2 % |
| Store under renovation | 5 days of empty cells; counting them as zero produces a 15th "shortage store" |

## Discrepancies: what each one means

Recalculated on the same export, switching off one step of the method at a time.

| What was skipped | Lost sales | Share | Stores | What else is visible |
|---|---|---|---|---|
| nothing (reference) | 163,548 | 10.2 % | 14 | sales 1,598,836 |
| duplicated week not dropped | 177,136 | 10.3 % | 14 | **the share barely changes**, the absolute number is wrong |
| promo week used as the rate base | 175,010 | 10.9 % | 14 | the norm is inflated |
| packs not converted to bottles | 80,326 | 13.7 % | 14 | sales 584,977 — 2.7 times lower |
| empty counted as zero | 165,352 | 10.3 % | **15** | the 15th store is the one under renovation |
| all of the above | 94,448 | 15.0 % | 15 | — |

A difference from the reference within 10–15 % is a different assumption about
a "usual day" (fair, if it is stated). A difference of several times is an
analysis error, and the table row tells you which one.

## Where this is in the course

* Block `p0` "Understand the task" — the question, the tree, the hypothesis,
  "what is not in the data".
* Block `p1` — the request to the model, checking before upload, upload.
* Block `p2` — access, data updates, presenting, troubleshooting.
* Block `p3` — working with the model.
* Block `p4` "Get to the answer" — **closes the case**: take the numbers from
  your own report (`p4l1`), work through the six findings (`p4l2`), check
  against the reference (`p4l3`), state the assumptions and test robustness
  (`p4l4`), fix and publish version 2 (`p4l5`, task `new_version`), write the
  recommendation (`p4l6`); the epilogue `p2l6` sums up the course.

## How to recalculate

```
PY=.venv/bin/python
$PY tools/learn_fixture_delta.py --check    # the data has not changed
$PY tools/learn_balance.py                  # the questions do not give the answer away
$PY tools/learn_selfcontained.py app/content/learn.prep.ru.json
$PY tools/learn_screens.py                  # card pointers point at their own screen
$PY tests/smoke_learn.py
```
