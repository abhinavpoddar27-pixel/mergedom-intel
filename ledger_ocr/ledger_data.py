"""
Transcribed source data for the Asian Lakto Industries QC ledger photographs.

Source: 5 photographs (one per PDF page) of two paper QC forms:
  * CSD PET ONLINE RECORD   (Doc No. CSD-PET-FRM-03)  - 3 dated sheets
  * TORQUE TESTING REPORT (HYMECH LINE)               - 2 dated sheets

Every value below was read by eye from the photographs at cell-level zoom.
"-" means a dash was written on the form (test not due at that interval).
None means the cell was left blank on the form.
"""

# ----------------------------------------------------------------------------
# Time columns of the CSD PET ONLINE RECORD (25 columns, as printed on the form)
# ----------------------------------------------------------------------------
CSD_TIMES = ["08:00", "08:30", "09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
             "12:00", "12:30", "01:00", "01:30", "02:00", "02:30", "03:00", "03:30",
             "04:00", "04:30", "05:00", "05:30", "06:00", "06:30", "07:00", "07:30",
             "08:00"]
# 24-hour equivalents (added by us for clarity; not printed on the form)
CSD_TIMES_24 = ["08:00", "08:30", "09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
                "12:00", "12:30", "13:00", "13:30", "14:00", "14:30", "15:00", "15:30",
                "16:00", "16:30", "17:00", "17:30", "18:00", "18:30", "19:00", "19:30",
                "20:00"]

# Parameter rows exactly as printed on the form, with their printed frequency.
CSD_PARAMS = [
    ("As is brix (Bev.)",                  "Startup & Every 30 Min",                  "num"),
    ("% Acidity",                          "startup & Every hrs",                     "num"),
    ("Pressure",                           "Startup & Every 30 Min",                  "num"),
    ("Temperature",                        "Startup & Every 30 Min",                  "num"),
    ("G.V.",                               "Startup & Every 30 Min",                  "num"),
    ("TOA",                                "Startup & Every 30 Min",                  "txt"),
    ("Stress Crack Test",                  "Start Up & Every 4 Hours",                "txt"),
    ("Candling (observation)",             "Every 2 hrs.",                            "txt"),
    ("Beverage Temprature After Warmer",   "Startup & 4 hrs",                         "txt"),
    ("Condensation on Bottle",             "Startup & 2 hrs",                         "txt"),
    ("Label Alignment",                    "Startup & Every 30 Min",                  "txt"),
    ("Glue Application",                   "Startup & Every 30 Min",                  "txt"),
    ("Primary Coding",                     "Startup & Every 30 Min",                  "txt"),
    ("Shrink packing Quality",             "Startup & Every Hour",                    "txt"),
    ("Torque",                             "All Capper Heads Start Up & Every 2 Hours","txt"),
    ("Closure Lot No.",                    "",                                        "txt"),
    ("Label Lot No.",                      "",                                        "txt"),
    ("Preform Lot No.",                    "",                                        "txt"),
]

_N = None   # blank cell on the form
_D = "–"    # dash written on the form


def _row(pairs):
    """Expand {index: value} into a 25-long row of None."""
    r = [_N] * 25
    for k, v in pairs.items():
        r[k] = v
    return r


def _fill(idxs, value):
    return {i: value for i in idxs}


# ============================================================================
# REPORT A - CSD PET ONLINE RECORD - 17/08/2026  (photo 3)
# ============================================================================
A_DATA_COLS = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 22, 23]

REPORT_A = {
    "sheet":   "CSD 17-08-2026",
    "title":   "CSD PET ONLINE RECORD",
    "docno":   "CSD-PET-FRM-03",
    "date":    "17/08/26",
    "product": "MC2 Original 250 ml",
    "chemist": "Pammee Singh",
    "shift":   "Day",
    "photo":   "Photo 3 (PDF page 3)",
    "data_cols": A_DATA_COLS,
    "annotations": [
        ("Time column, spanning the sheet",
         'Vertical note: "Filler Start 8:38 AM", with arrows spanning the full column height.'),
    ],
    "remark": "",
    "qc_chemist": "Pammee Singh",
    "qc_manager": "(initialled)",
    "note_below_table": "",
    "rows": {
        "As is brix (Bev.)": _row({}),
        "% Acidity":         _row({}),
        "Pressure": _row({2: 36, 3: 36, 4: 40, 5: 40, 6: 40, 7: 40, 8: 44, 9: 44,
                          10: 42, 11: 42, 12: 46, 13: 46, 14: 44, 15: 44, 16: 44, 17: 44,
                          18: 46, 19: 46, 22: 46, 23: 46}),
        "Temperature": _row({2: 10.4, 3: 10.4, 4: 13.4, 5: 13.4, 6: 11.7, 7: 11.7,
                             8: 14, 9: 14, 10: 10.6, 11: 10.6, 12: 12.2, 13: 12.8,
                             14: 14.6, 15: 14.6, 16: 13.7, 17: 13.7, 18: 15.1, 19: 15.1,
                             22: 15.2, 23: 15.2}),
        "G.V.": _row({2: 3.9, 3: 3.9, 4: 3.9, 5: 3.9, 6: 4.1, 7: 4.1, 8: 4.1, 9: 4.1,
                      10: 4.4, 11: 4.4, 12: 4.4, 13: 4.4, 14: 4.1, 15: 4.1, 16: 4.2, 17: 4.2,
                      18: 4.1, 19: 4.1, 22: 4.1, 23: 4.1}),
        "TOA":                              _row(_fill(A_DATA_COLS, "OK")),
        "Stress Crack Test":                _row(_fill(A_DATA_COLS, "OK")),
        "Candling (observation)":           _row(_fill(A_DATA_COLS, _D)),
        "Beverage Temprature After Warmer": _row(_fill(A_DATA_COLS, _D)),
        "Condensation on Bottle":           _row(_fill(A_DATA_COLS, _D)),
        "Label Alignment":                  _row(_fill(A_DATA_COLS, "OK")),
        "Glue Application":                 _row(_fill(A_DATA_COLS, "OK")),
        "Primary Coding":                   _row(_fill(A_DATA_COLS, "OK")),
        "Shrink packing Quality":           _row(_fill(A_DATA_COLS, "OK")),
        "Torque":                           _row({}),
        "Closure Lot No.":                  _row({}),
        "Label Lot No.":                    _row({}),
        "Preform Lot No.":                  _row({}),
    },
}

# ============================================================================
# REPORT B - CSD PET ONLINE RECORD - 19/08/2026  (photo 5)
# ============================================================================
B_DATA_COLS = [2, 3, 4, 5, 8, 9, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]

REPORT_B = {
    "sheet":   "CSD 19-08-2026",
    "title":   "CSD PET ONLINE RECORD",
    "docno":   "CSD-PET-FRM-03",
    "date":    "19/08/26",
    "product": "MC2 Original 250 ml",
    "chemist": "Aafreen",
    "shift":   "Day",
    "photo":   "Photo 5 (PDF page 5)",
    "data_cols": B_DATA_COLS,
    "annotations": [
        ("08:30 column",           'Vertical note: "Filler Start 8:30 am" with an arrow.'),
        ("11:00 - 11:30 columns",  'Vertical note: "Filler stop due to outfeed knife setting '
                                   '& welding work" - no readings logged for these two intervals.'),
        ("01:00 - 01:30 columns",  'Vertical note: "Line stop" with arrows - no readings logged.'),
        ("08:00 PM column",        'Vertical note: "shift over".'),
    ],
    "remark": "",
    "qc_chemist": "Aafreen",
    "qc_manager": "(initialled)",
    "note_below_table": "",
    "rows": {
        "As is brix (Bev.)": _row({2: 10.96, 3: 10.96, 4: 11.18, 5: 11.18, 8: 11.07, 9: 11.07,
                                   12: 11.12, 13: 11.12, 14: 11.02, 15: 11.02, 16: 11.17, 17: 11.17,
                                   18: 11.03, 19: 11.03, 20: 10.92, 21: 10.92, 22: 10.98, 23: 10.98}),
        "% Acidity": _row({2: 0.70, 3: 0.70, 4: 0.70, 5: 0.70, 8: 0.70, 9: 0.70,
                           12: 0.68, 13: 0.68, 14: 0.70, 15: 0.70, 16: 0.70, 17: 0.70,
                           18: 0.70, 19: 0.70, 20: 0.70, 21: 0.70, 22: 0.70, 23: 0.70}),
        "Pressure": _row({2: 42, 3: 42, 4: 43, 5: 43, 8: 48, 9: 48,
                          12: 38, 13: 38, 14: 40, 15: 40, 16: 44, 17: 44,
                          18: 46, 19: 46, 20: 42, 21: 42, 22: 40, 23: 40}),
        "Temperature": _row({2: 10.2, 3: 10.2, 4: 9.5, 5: 9.5, 8: 14.2, 9: 14.2,
                             12: 10.3, 13: 10.3, 14: 13.0, 15: 13.0, 16: 13.8, 17: 13.8,
                             18: 13.8, 19: 13.8, 20: 9.5, 21: 9.5, 22: 10.3, 23: 10.3}),
        "G.V.": _row({2: 4.4, 3: 4.4, 4: 4.6, 5: 4.6, 8: 4.4, 9: 4.4,
                      12: 4.2, 13: 4.2, 14: 4.0, 15: 4.0, 16: 4.2, 17: 4.2,
                      18: 4.3, 19: 4.3, 20: 4.6, 21: 4.6, 22: 4.3, 23: 4.3}),
        "TOA":                              _row(_fill(B_DATA_COLS, "OK")),
        "Stress Crack Test":                _row({**_fill(B_DATA_COLS, _D), 2: "OK"}),
        "Candling (observation)":           _row(_fill(B_DATA_COLS, _D)),
        "Beverage Temprature After Warmer": _row(_fill(B_DATA_COLS, _D)),
        "Condensation on Bottle":           _row(_fill(B_DATA_COLS, _D)),
        "Label Alignment":                  _row(_fill(B_DATA_COLS, "OK")),
        "Glue Application":                 _row(_fill(B_DATA_COLS, "OK")),
        "Primary Coding":                   _row(_fill(B_DATA_COLS, "OK")),
        "Shrink packing Quality":           _row(_fill(B_DATA_COLS, "OK")),
        "Torque":                           _row({**_fill(B_DATA_COLS, _D), 2: "OK"}),
        "Closure Lot No.":                  _row({}),
        "Label Lot No.":                    _row({}),
        "Preform Lot No.":                  _row({}),
    },
}

# ============================================================================
# REPORT C - CSD PET ONLINE RECORD - 20/08/2026  (photo 1)
# ============================================================================
C_DATA_COLS = [0, 1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 22, 23]

REPORT_C = {
    "sheet":   "CSD 20-08-2026",
    "title":   "CSD PET ONLINE RECORD",
    "docno":   "CSD-PET-FRM-03",
    "date":    "20/08/26",
    "product": "MC2 Original 250 ml",
    "chemist": "Pammee Singh",
    "shift":   "Day",
    "photo":   "Photo 1 (PDF page 1)",
    "data_cols": C_DATA_COLS,
    "annotations": [
        ("05:00 - 06:30 columns", 'Vertical notes: "Filler Stop" and "15 min", bracketed across '
                                  'these columns - no readings logged for 05:00-06:30. '
                                  'Small markers "1" and "2" are written at the head of the '
                                  '05:00 and 06:30 columns.'),
        ("10:00 - 10:30 columns", 'No readings logged; no explanatory note written on the form.'),
    ],
    "remark": "",
    "qc_chemist": "Pammee Singh",
    "qc_manager": "(initialled)",
    "note_below_table": ('Handwritten below the table: "MC2 Original 250 ml Pyramid / '
                         '3500 previously washed bottle / were run on the production line / '
                         'time 7:00 Pm start ..." (final line runs off the bottom edge of the photograph)'),
    "rows": {
        "As is brix (Bev.)": _row({}),
        "% Acidity":         _row({}),
        "Pressure": _row({0: 40, 1: 40, 2: 36, 3: 36, 6: 42, 7: 42, 8: 40, 9: 40,
                          10: 40, 11: 40, 12: 38, 13: 38, 14: 40, 15: 40, 16: 38, 17: 38,
                          22: 42, 23: 42}),
        "Temperature": _row({0: 8.5, 1: 8.5, 2: 8.5, 3: 8.5, 6: 8.7, 7: 8.7, 8: 6.1, 9: 10.1,
                             10: 7.4, 11: 7.4, 12: 10.2, 13: 10.2, 14: 9.3, 15: 9.3,
                             16: 10, 17: 10, 22: 13.1, 23: 13.1}),
        "G.V.": _row({0: 4.5, 1: 4.5, 2: 4.2, 3: 4.2, 6: 4.7, 7: 4.7, 8: 4.3, 9: 4.3,
                      10: 4.7, 11: 4.7, 12: 4.1, 13: 4.1, 14: 4.4, 15: 4.4, 16: 4.2, 17: 4.2,
                      22: 4.1, 23: 4.1}),
        "TOA":                              _row(_fill(C_DATA_COLS, "OK")),
        "Stress Crack Test":                _row(_fill(C_DATA_COLS, "OK")),
        "Candling (observation)":           _row(_fill(C_DATA_COLS, _D)),
        "Beverage Temprature After Warmer": _row(_fill(C_DATA_COLS, _D)),
        "Condensation on Bottle":           _row(_fill(C_DATA_COLS, _D)),
        "Label Alignment":                  _row(_fill(C_DATA_COLS, "OK")),
        "Glue Application":                 _row(_fill(C_DATA_COLS, "OK")),
        "Primary Coding":                   _row(_fill(C_DATA_COLS, "OK")),
        "Shrink packing Quality":           _row(_fill(C_DATA_COLS, "OK")),
        "Torque":                           _row({}),
        "Closure Lot No.":                  _row({}),
        "Label Lot No.":                    _row({}),
        "Preform Lot No.":                  _row({}),
    },
}

CSD_REPORTS = [REPORT_A, REPORT_B, REPORT_C]

# ============================================================================
# TORQUE TESTING REPORT (HYMECH LINE)
# Rows printed on the form: 09:00, 11:00, 01:00, 03:00, 05:00, 07:00
# Each entry: (reading, circled_on_source, retest_or_result)
#   reading  - number, or a string for a non-numeric entry, or None if blank
#   circled  - True if the chemist ringed the value on the form (= rejected)
#   retest   - re-test reading (number) or "OK" when the first reading was accepted
# ============================================================================
TORQUE_TIMES = ["09:00", "11:00", "01:00", "03:00", "05:00", "07:00"]

REPORT_D = {
    "sheet":   "Torque 17-08-2026",
    "company": "ASIAN LAKTO INDUSTRIES LTD",
    "title":   "TORQUE TESTING REPORT (HYMECH LINE)",
    "date":    "17/08/26",
    "product": "MC2 Original 250 ml",
    "capmake": "MD",
    "shift":   "Day",
    "chemist": "Pammee",
    "photo":   "Photo 4 (PDF page 4)",
    "readings": {
        "11:00": [(25.8, True, 12.3), (10.1, False, "OK"), (27.7, True, 17.5),
                  (15.9, False, "OK"), (10.1, False, "OK"), (10.2, False, "OK"),
                  (16.7, False, "OK"), (10.0, False, "OK"), (12.7, False, "OK"),
                  (6.0, True, 10.8),  (15.9, False, "OK"), (10.3, False, "OK"),
                  (25.4, True, 11.7), ("No open", True, 16.0), (10.0, False, "OK")],
        "01:00": [(10.0, False, "OK"), (11.8, False, "OK"), (14.0, False, "OK"),
                  (20.5, True, 15.5), (13.8, False, "OK"), (9.9, False, "OK"),
                  (10.7, False, "OK"), (25.4, True, 16.0), (13.8, False, "OK"),
                  (7.6, True, 10.5),  (14.3, False, "OK"), (14.2, False, "OK"),
                  (25.1, True, 15.0), (26.3, True, 16.3), (13.0, False, "OK")],
    },
    "remarks": {},
}

REPORT_E = {
    "sheet":   "Torque 20-08-2026",
    "company": "ASIAN LAKTO INDUSTRIES LTD",
    "title":   "TORQUE TESTING REPORT (HYMECH LINE)",
    "date":    "20/08/26",
    "product": "MC2 Original 250 ml",
    "capmake": "MD",
    "shift":   "Day",
    "chemist": "Pammee Singh",
    "photo":   "Photo 2 (PDF page 2)",
    "readings": {
        "11:00": [(15.0, False, "OK"), (13.7, False, "OK"), (15.8, False, "OK"),
                  (24.1, True, 15.0), (11.3, False, "OK"), (7.9, True, None),
                  ("No open", True, 16.6), (9.6, False, "OK"), (12.7, False, "OK"),
                  (35.3, True, 17.5), (17.5, False, "OK"), (14.4, False, "OK"),
                  (26.4, True, 16.3), (21.2, True, 15.0), (26.3, True, 17.6)],
        "01:00": [(17.6, False, "OK"), (14.3, False, "OK"), (14.1, False, "OK"),
                  (16.7, False, "OK"), (9.8, False, "OK"), (13.2, False, "OK"),
                  (17.5, False, "OK"), (14.3, False, "OK"), (18.5, False, "OK"),
                  (22.5, True, 15.1), (20.0, True, 11.5), (16.2, False, "OK"),
                  (28.8, True, 13.0), (20.3, True, 14.3), (13.0, False, "OK")],
    },
    "remarks": {},
}

TORQUE_REPORTS = [REPORT_D, REPORT_E]

# Acceptance band for cap-removal torque.  NOT printed on the form; see notes.
SPEC_LOW_DEFAULT = 8.0
SPEC_HIGH_DEFAULT = 19.0
