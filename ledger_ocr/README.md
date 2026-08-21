# Ledger photographs → verified Excel workbook

Digitisation of five photographed paper QC forms from Asian Lakto Industries Ltd
(August 2026) into a single formula-driven workbook.

**Deliverable:** `Asian_Lakto_QC_Records_Aug2026.xlsx`

## Source material

| Photo (PDF page) | Report | Date | Chemist |
|---|---|---|---|
| 3 | CSD PET Online Record (Doc CSD-PET-FRM-03) | 17/08/26 | Pammee Singh |
| 5 | CSD PET Online Record | 19/08/26 | Aafreen |
| 1 | CSD PET Online Record | 20/08/26 | Pammee Singh |
| 4 | Torque Testing Report (Hymech Line) | 17/08/26 | Pammee |
| 2 | Torque Testing Report (Hymech Line) | 20/08/26 | Pammee Singh |

Five separate documents in two report families — no photograph is a continuation
page of another — so each gets its own sheet, plus an `Index & Checks` sheet.

## Files

| File | Purpose |
|---|---|
| `ledger_data.py` | The transcription itself: every value read from the photographs, as data. |
| `build_workbook.py` | Renders the transcription into the formatted, formula-driven workbook. |
| `validate_transcription.py` | Internal-consistency checks on the transcription (runs against the data, not the workbook). |
| `verify_workbook.py` | Loads a LibreOffice-recalculated copy and asserts no formula errors and no empty formula cells. |
| `edge_case_test.py` | Mutates single input cells, recalculates, and asserts dependent formulas move — proves the workbook is live. |

## Rebuild and re-verify

```bash
pip install openpyxl                 # plus libreoffice-calc for recalculation
BOOK=Asian_Lakto_QC_Records_Aug2026.xlsx

python3 validate_transcription.py            # transcription self-checks
python3 build_workbook.py                    # write the workbook

soffice --headless --norestore --convert-to xlsx --outdir /tmp/recalc "$BOOK"
python3 verify_workbook.py /tmp/recalc/"$BOOK" "$BOOK"   # zero formula errors
python3 edge_case_test.py                    # liveness
```

## How accuracy was established

Neither form carries a derived column to recompute, so two structural properties
of the sources were used as the accuracy check instead.

**Torque sheets.** The chemist ringed every reading he rejected and wrote a
re-test underneath. A single acceptance band must therefore separate the ringed
readings from the un-ringed ones — a misread digit would break that separation.
All 58 numeric readings agree: every un-ringed reading falls in 9.6–18.5 lb-in,
every ringed one outside 7.9–20.0, and all 20 re-tests land inside the band.
The band cells on the `Index & Checks` sheet are editable and drive every check.

**CSD sheets.** Readings are logged as :00/:30 pairs, written twice. 100 of the
102 pairs are identical; the two exceptions are anomalies in the paper record
itself, reproduced as written and flagged with cell comments rather than
silently harmonised.

Cell rulings were detected computationally in each photograph and every data
cell was cut out and read individually at high magnification, so no value
depends on judging column membership by eye across the pages' perspective skew.

## Known findings in the source documents

Reproduced exactly as written, never corrected. All are listed on the
`Index & Checks` sheet and attached as comments to the cells concerned.

- **Torque 20-08, 11:00, head 6** — 7.9 lb-in is ringed as rejected but no
  re-test was written; the head's outcome is unrecorded on the form.
- **CSD 20-08, Temperature 12:00** — 6.1 °C breaks the sheet's pairing and sits
  far below every other reading that day.
- **CSD 17-08, Temperature 02:30** — 12.8 °C against 12.2 °C at 02:00.
- **CSD 17-08 and 20-08** — the "As is brix" and "% Acidity" rows are entirely
  blank although the form requires them.
- Seven further ambiguous or non-numeric cells (including two "No open" entries)
  carry explanatory comments.
