"""
Arithmetic / internal-consistency checks run against the transcribed ledger data.

Neither source form carries a printed derived column (no "Total = A + B" row), so
these checks exploit the structural regularities the forms do have:

  * TORQUE  - the chemist ringed every reading he rejected and wrote a re-test
              underneath.  A single acceptance band must therefore separate the
              ringed readings from the un-ringed ones.  If a digit were misread,
              that separation would break.
  * CSD PET - readings are logged as :00 / :30 pairs and, in practice, the pair
              is written twice with the same value.  A misread digit shows up as
              a broken pair.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_data as d

fails = []
source_findings = []
def check(ok, msg):
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    if not ok:
        fails.append(msg)

def source_finding(msg):
    """A defect in the paper record itself - reported, never silently corrected."""
    print("  SOURCE  " + msg)
    source_findings.append(msg)

print("=" * 78)
print("TORQUE TESTING REPORTS - acceptance-band separation test")
print("=" * 78)

accepted, rejected, retests, no_retest = [], [], [], []
for rep in d.TORQUE_REPORTS:
    for t, heads in rep["readings"].items():
        for i, (val, circled, retest) in enumerate(heads, start=1):
            where = f"{rep['sheet']} {t} head {i}"
            if isinstance(val, (int, float)):
                (rejected if circled else accepted).append((val, where))
            if circled and retest is None:
                no_retest.append(where)
            if isinstance(retest, (int, float)):
                retests.append((retest, where))

acc_lo, acc_hi = min(a[0] for a in accepted), max(a[0] for a in accepted)
rej_lo = [r for r in rejected if r[0] < acc_lo]
rej_hi = [r for r in rejected if r[0] > acc_hi]
print(f"  numeric readings: {len(accepted) + len(rejected)} "
      f"({len(accepted)} un-ringed, {len(rejected)} ringed)")
print(f"  un-ringed readings span   : {acc_lo} .. {acc_hi}")
print(f"  ringed low  readings (max): {max(r[0] for r in rej_lo)}")
print(f"  ringed high readings (min): {min(r[0] for r in rej_hi)}")

check(max(r[0] for r in rej_lo) < acc_lo,
      "every ringed low reading is below every un-ringed reading")
check(min(r[0] for r in rej_hi) > acc_hi,
      "every ringed high reading is above every un-ringed reading")

LO, HI = d.SPEC_LOW_DEFAULT, d.SPEC_HIGH_DEFAULT
check(max(r[0] for r in rej_lo) < LO <= acc_lo,
      f"chosen lower limit {LO} separates ringed from un-ringed readings")
check(acc_hi <= HI < min(r[0] for r in rej_hi),
      f"chosen upper limit {HI} separates ringed from un-ringed readings")

mismatches = []
for rep in d.TORQUE_REPORTS:
    for t, heads in rep["readings"].items():
        for i, (val, circled, _r) in enumerate(heads, start=1):
            if isinstance(val, (int, float)):
                out = val < LO or val > HI
                if out != circled:
                    mismatches.append(f"{rep['sheet']} {t} head {i}: value={val} "
                                      f"ringed={circled} outside_band={out}")
check(not mismatches,
      f"all {len(accepted) + len(rejected)} numeric readings agree with the chemist's own ringing")
for m in mismatches:
    print("           " + m)

bad_retests = [f"{w}: {v}" for v, w in retests if v < LO or v > HI]
check(not bad_retests, f"all {len(retests)} re-test readings fall inside the band")
for b in bad_retests:
    print("           " + b)

if no_retest:
    for n in no_retest:
        source_finding(f"{n} is ringed as rejected but no re-test was written on the form")
else:
    check(True, "every ringed reading has a re-test recorded")

print()
print("=" * 78)
print("CSD PET ONLINE RECORD - :00 / :30 pair consistency and column completeness")
print("=" * 78)

NUMERIC = [p for p, _f, k in d.CSD_PARAMS if k == "num"]
total_pairs = ok_pairs = 0
for rep in d.CSD_REPORTS:
    print(f"\n  {rep['sheet']}  ({rep['date']}, {rep['chemist']})")
    diffs = []
    for pname in NUMERIC:
        row = rep["rows"][pname]
        for i in range(0, 24, 2):
            a, b = row[i], row[i + 1]
            if a is None and b is None:
                continue
            total_pairs += 1
            if a == b:
                ok_pairs += 1
            else:
                diffs.append(f"{pname} @ {d.CSD_TIMES[i]}/{d.CSD_TIMES[i+1]}: {a} vs {b}")
    print(f"    numeric :00/:30 pairs written on this sheet: "
          f"{sum(1 for p in NUMERIC for i in range(0,24,2) if not (rep['rows'][p][i] is None and rep['rows'][p][i+1] is None))}")
    for x in diffs:
        source_finding(f"{rep['sheet']}: {x} - the two halves of one :00/:30 pair differ "
                       f"(transcribed exactly as written)")

    # column completeness: which parameters are filled in each data column
    sig = {}
    for c in rep["data_cols"]:
        filled = tuple(p for p, _f, _k in d.CSD_PARAMS if rep["rows"][p][c] is not None)
        sig.setdefault(filled, []).append(d.CSD_TIMES[c])
    check(len(sig) <= 2,
          f"{rep['sheet']}: data columns share a consistent set of filled parameters "
          f"({len(sig)} distinct pattern(s))")
    for k, v in sig.items():
        print(f"      pattern of {len(k)} parameters -> columns {', '.join(v)}")

    # no stray values outside the declared data columns
    stray = [(p, d.CSD_TIMES[i]) for p, _f, _k in d.CSD_PARAMS
             for i in range(25)
             if rep["rows"][p][i] is not None and i not in rep["data_cols"]]
    check(not stray, f"{rep['sheet']}: no values recorded outside the identified active columns")

print(f"\n  pair totals across all three CSD sheets: {ok_pairs}/{total_pairs} pairs identical, "
      f"{total_pairs - ok_pairs} differ (transcribed as written, flagged in the workbook)")

print()
print("=" * 78)
print(f"RESULT: {len(fails)} failed transcription check(s); "
      f"{len(source_findings)} finding(s) in the source document itself")
for f in source_findings:
    print(f"  - {f}")
print("=" * 78)
sys.exit(1 if fails else 0)
