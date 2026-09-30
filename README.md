# logger_data

Read AiM Race Studio CSV exports into pandas, with laps and distance ready to use.

```python
from logger_data import Session, validate

session = Session("my_session.csv")
validate(session)
```

That is all you need. `session.data` is a DataFrame with every sample of every complete lap, and `session.laps` is the lap table.

## Requirements

- Python 3.9 or newer
- pandas and numpy (tested with pandas 2.2 and 3.0)
- openpyxl, only for `export(..., fmt="xlsx")`

Google Colab already has all of them.

## Setup

**Local:** put `logger_data.py` in the same folder as your notebook or script.

**Colab, one-off:** upload `logger_data.py` to `/content` with the Files panel.

**Colab, shared through Google Drive:** keep one copy of `logger_data.py` in a shared Drive folder so everyone uses the same version.

```python
from google.colab import drive
drive.mount("/content/drive")

import sys
sys.path.append("/content/drive/MyDrive/<shared folder>")   # folder that contains logger_data.py

from logger_data import Session, validate
```

## What a Session contains

| Attribute | What it is |
|---|---|
| `session.data` | Samples of the complete laps. Index: session time in seconds. Columns: every logger channel plus the four added columns below. |
| `session.laps` | One row per lap: `lap`, `type`, `start`, `end`, `lap_time` (s), `lap_time_str` (e.g. `1:58.432`). |
| `session.metadata` | Everything from the top of the file, usually: `Format`, `Session`, `Vehicle`, `Racer`, `Championship`, `Comment`, `Date`, `Time`, `Sample Rate`, `Duration`, `Segment`, `Beacon Markers`, `Segment Times`. The parser adds `Datetime` (date and time combined). Other files may have more keys; they are kept as text. |
| `session.units` | Channel name to unit, e.g. `session.units["GPS Speed"]` gives `"km/h"`. |
| `session.raw` | The samples exactly as recorded, all laps, no added columns. |
| `session.all_laps` | The lap table including the out-lap and in-lap. |
| `session.warnings` | Filled in by `validate(session)`; `None` until then. |
| `session.name_map` | Python-friendly name to original channel name, e.g. `"gps_speed"` to `"GPS Speed"`. |
| `session.path` | The file the session was loaded from. |

Channel names are kept exactly as in Race Studio (`"GPS Speed"`, `"LateralAcc"`, ...).

### Columns added to every sample

| Column | Meaning |
|---|---|
| `lap` | Lap number (see below). |
| `lap_elapsed` | Seconds since the start of the lap. |
| `distance` | Metres since the start of the session, from integrated `GPS Speed`. |
| `lap_distance` | Metres since the start/finish line. **Use this as the x-axis to compare laps.** |

## Lap numbering

Laps are numbered like Race Studio:

| `lap` | `type` | What it is |
|---|---|---|
| 0 | `out` | From the start of the recording to the first line crossing |
| 1 … N | `lap` | Complete laps |
| N+1 | `in` | From the last line crossing to the end of the recording |

The out-lap and in-lap are **removed by default**, so `session.data` and `session.laps` only contain complete laps. To keep them:

```python
full = Session("my_session.csv", keep_in_out=True)
```

## Common tasks

**Lap times and fastest lap**

```python
print(session.laps[["lap", "lap_time_str"]])

best = session.laps.loc[session.laps["lap_time"].idxmin()]
print("Fastest: lap", best["lap"], best["lap_time_str"])
```

**One lap**

```python
lap = session.get_lap(3)
print(lap["GPS Speed"].max(), session.units["GPS Speed"])
```

`get_lap` returns a copy, so changing it never changes the session. Asking for a lap that is not there gives a clear error that lists the available laps.

**Compare two laps on distance**

```python
import matplotlib.pyplot as plt

for n in (2, 3):
    lap = session.get_lap(n)
    plt.plot(lap["lap_distance"], lap["GPS Speed"], label=f"Lap {n}")

plt.xlabel("Distance from start/finish (m)")
plt.ylabel("GPS Speed (km/h)")
plt.legend()
plt.show()
```

**Session info**

```python
meta = session.metadata
print(meta["Session"], meta["Vehicle"], meta["Racer"], meta["Datetime"])
```

**Short summary**

```python
print(session)
# Session(<Session> | <Vehicle> | <Racer> | <date and time> | <N> laps | <samples> samples | not validated)
```

## Exporting for people who do not use Python

```python
session.export("export")                            # export/my_session_laps.csv and export/my_session_data.csv
session.export("export", fmt="xlsx")                # export/my_session.xlsx with sheets "laps" and "data"
session.export("export", fmt="xlsx", per_lap=True)  # one sheet per lap: lap01, lap02, ...
```

`export` returns the list of files it wrote. File names start with the original file name (`my_session` for `my_session.csv`).

**For Excel, use `fmt="xlsx"`.** CSV files use `,` between values and `.` for decimals; Excel with Greek or other European settings may put everything in one column. Writing `.xlsx` takes around 10 seconds, CSV under one second.

## Validation

```python
ok = validate(session)
```

`validate` prints what it found, stores it in `session.warnings`, and returns `True` if there were no issues. It checks the full recording (including out-lap and in-lap) and **never changes the data**.

| Check | Example of what it catches |
|---|---|
| Last beacon matches `Duration` | Truncated file |
| Beacon times match `Segment Times` | Inconsistent lap markers |
| Samples cover the whole session | Missing start or end |
| Constant time step | Gaps or duplicated samples |
| Empty or non-numeric values | Corrupted cells |
| Channels with one constant value | Unused math channels |
| Identical rows for 1 s or more | Logger repeating its last values |

Example of the output:

```
my_session.csv: 2 warning(s)
 - <channel> has the same value (0.0) in every sample.
 - Samples <start>-<end> s (<rows> rows, lap <n>, in-lap) are identical: the logger repeated its last values.
```

## Good to know

- **File format.** Only AiM Race Studio CSV exports are accepted (first line `"Format","AiM CSV File"`). Any other file gives a clear error. Exports that use `;` and a decimal comma are detected automatically.
- **Distance.** It comes from integrating `GPS Speed` over time, measured from the exact moment the car crosses the start/finish line.
- **Frozen logger.** If the logger repeats its last row, speed stays above zero while the car is not moving, so `distance` keeps growing there. `validate` reports these stretches.
- **Datetime.** `metadata["Datetime"]` is `None` when the date in the file is not in English. `Date` and `Time` are always kept as text.
- **Version.** `get_version()` returns the version of `logger_data`. When results differ between two people, check this first.

## Changing the code

Bump `__version__` at the top of `logger_data.py` whenever the parsing changes, and add a line below.

## Versions

- **0.1.0** — First version: `Session`, `validate`, `get_lap`, `export`, `get_version`.
