"""
logger_data.py
Read AiM Race Studio CSV exports into a ready-to-use Session.

Usage:
    from logger_data import Session, validate, get_version

    session = Session("my_session.csv")  # parse one file
    validate(session)                    # optional sanity checks

    sessions = Session("my_folder")      # parse every CSV in a folder -> dict
    validate(sessions)                   # checks every file
    sessions["my_session.csv"].data      # one session from the folder

    session.data                         # samples of the complete laps (index: time in s)
    session.laps                         # lap table: lap, type, start, end, lap_time, lap_time_str
    session.metadata                     # track, vehicle, driver, date, sample rate, ...
    session.units                        # channel name -> unit
    session.get_lap(3)                   # samples of one lap
    session.export("out", fmt="xlsx")    # write files for people who do not use Python
    get_version()                        # version of this module, e.g. "0.2.0"

Laps are numbered like Race Studio: out-lap 0, complete laps 1..N, in-lap N+1.
The out-lap and in-lap are removed unless Session(path, keep_in_out=True).

Columns added to every sample (besides the logger channels):
    lap           lap number
    lap_elapsed   seconds since the start of the lap
    distance      metres since the start of the session (integrated GPS Speed)
    lap_distance  metres since the start/finish line; use it to overlay laps
"""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

__version__ = "0.2.0"


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------

def _split_row(line: str, sep: str) -> list[str]:
    """
    Split one CSV line into fields.
    Handles quoted values and strips surrounding spaces from every field.
    An empty line returns an empty list.
    """
    line = line.rstrip("\r\n")
    if not line.strip():
        return []
    return [f.strip() for f in next(csv.reader([line], delimiter=sep))]


def _detect_format(lines: list[str]) -> tuple[str, str]:
    """
    Confirm the first line is ("Format", "AiM CSV File") and detect export settings.
    Returns (separator, decimal), e.g. (",", ".") or (";", ",").
    Raises ValueError with a readable message if this is not an AiM file.
    """
    if not lines:
        raise ValueError("The file is empty.")

    # Remove a possible UTF-8 BOM from the very first line
    first = lines[0].lstrip("\ufeff")

    # Try each candidate separator and keep the one that reveals the AiM signature
    for sep in (",", ";", "\t"):
        fields = _split_row(first, sep)
        if len(fields) >= 2 and fields[0] == "Format" and fields[1] == "AiM CSV File":
            break
    else:
        raise ValueError(
            f"Not an AiM CSV export. First line is {first.strip()[:60]!r}, "
            'expected "Format","AiM CSV File".'
        )

    # Decimal mark: inspect the Duration value in the metadata block
    decimal = "."
    for line in lines[:50]:
        fields = _split_row(line, sep)
        if len(fields) > 1 and fields[0] == "Duration":
            if "," in fields[1] and sep != ",":
                decimal = ","
            break

    return sep, decimal


def _parse_lap_time(text: str) -> float:
    """
    Convert an AiM time string to seconds.
    "1:58.432" -> 118.432, "0:47.105" -> 47.105, "1:02:03.5" -> 3723.5
    A decimal comma ("1:58,432") is also accepted.
    """
    seconds = 0.0
    # Each ":" separated part is worth 60 times the next one (h:m:s or m:s)
    for part in text.strip().replace(",", ".").split(":"):
        seconds = seconds * 60 + float(part)
    # Round to milliseconds to avoid floating point noise like 137.50099999
    return round(seconds, 3)


def _parse_metadata(lines: list[str], sep: str) -> dict:
    """
    Parse the key-value block at the top of the file, up to the first blank line.
      - "Sample Rate" -> int, "Duration" -> float
      - "Beacon Markers" -> list of floats (cumulative seconds)
      - "Segment Times" -> list of floats (seconds)
      - "Date" + "Time" -> extra key "Datetime" (None if the format is unknown)
      - Any other key is kept as text, so new fields never break parsing
    """
    def to_float(value: str) -> float:
        # Fields are already split, so a comma here can only be a decimal comma
        return float(value.replace(",", "."))

    meta = {}
    for line in lines:
        fields = _split_row(line, sep)
        # The metadata block ends at the first blank line
        if not fields:
            break

        key, values = fields[0], [v for v in fields[1:] if v != ""]

        if key == "Beacon Markers":
            meta[key] = [to_float(v) for v in values]
        elif key == "Segment Times":
            meta[key] = [_parse_lap_time(v) for v in values]
        elif key == "Sample Rate" and values:
            meta[key] = int(to_float(values[0]))
        elif key == "Duration" and values:
            meta[key] = to_float(values[0])
        else:
            # Unknown or text keys: one value -> string, several -> list, none -> ""
            meta[key] = values[0] if len(values) == 1 else (values or "")

    # Combine Date and Time into one datetime when the format is recognised
    try:
        meta["Datetime"] = datetime.strptime(
            f'{meta.get("Date", "")} {meta.get("Time", "")}',
            "%A, %B %d, %Y %I:%M %p",
        )
    except ValueError:
        meta["Datetime"] = None

    return meta


def _find_header_row(lines: list[str], sep: str) -> int:
    """
    Return the index of the channel header row (first field == "Time").
    The search starts after the metadata block, because the metadata
    also contains a "Time" line (the session start time, e.g. "2:30 PM").
    Raises ValueError if no header row is found.
    """
    in_metadata = True
    for i, line in enumerate(lines):
        fields = _split_row(line, sep)
        # The first blank line marks the end of the metadata block
        if in_metadata:
            if not fields:
                in_metadata = False
            continue
        if fields and fields[0] == "Time":
            return i
    raise ValueError('Channel header row not found (no line starting with "Time" after the metadata).')


def _parse_channels(lines: list[str], header_idx: int, sep: str) -> tuple[list[str], dict]:
    """
    Read channel names from the header row and units from the row below it.
    Returns (names, units) where units maps each channel name to its unit.
      - Blank units (e.g. GPS Nsat) become ""
      - If the units row is missing or has a different length, all units are ""
      - Duplicate channel names get a suffix ("Speed", "Speed_2") so none is lost
    """
    raw_names = _split_row(lines[header_idx], sep)

    # Make every name unique without dropping any channel
    names, seen = [], {}
    for name in raw_names:
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f"{name}_{seen[name]}")

    # The units row sits directly below the header
    unit_fields = _split_row(lines[header_idx + 1], sep) if header_idx + 1 < len(lines) else []
    if len(unit_fields) != len(names):
        unit_fields = [""] * len(names)

    units = dict(zip(names, unit_fields))
    return names, units


def _clean_name(name: str) -> str:
    """
    Convert an AiM channel name to a Python-friendly one.
    "GPS Speed" -> "gps_speed", "YawRate" -> "yaw_rate",
    "GPS PosAccuracy" -> "gps_pos_accuracy", "maxinline" -> "maxinline"
    """
    # Split camelCase: "YawRate" -> "Yaw_Rate", "GPSSpeed" -> "GPS_Speed"
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    # Anything that is not a letter or digit becomes "_"
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    # Python names cannot start with a digit
    if name and name[0].isdigit():
        name = "ch_" + name
    return name


def _read_data(path: str | Path, header_idx: int, names: list[str],
               sep: str, decimal: str) -> pd.DataFrame:
    """
    Read the sample rows into a DataFrame.
      - Skips metadata, header, units row and blank lines
      - Uses the names from _parse_channels as column names
      - Forces every column to numeric (all values are quoted in the file)
      - Uses the first channel ("Time") as the index
    Only rows without a numeric time value (e.g. the units row) are skipped.
    """
    df = pd.read_csv(
        path,
        sep=sep,
        decimal=decimal,
        skiprows=header_idx + 1,   # everything up to and including the header row
        header=None,
        names=names,
        skip_blank_lines=True,
        low_memory=False,
    )

    # The units row makes pandas read every column as text; convert to numbers.
    # A decimal comma must become a dot first, otherwise the value would be lost
    for col in df.columns:
        if not pd.api.types.is_numeric_dtype(df[col]):
            text = df[col].astype(str)
            if decimal != ".":
                text = text.str.replace(decimal, ".", regex=False)
            df[col] = pd.to_numeric(text, errors="coerce")

    # The units row has no numeric time value, so it is skipped here
    df = df[df[names[0]].notna()]

    return df.set_index(names[0])


# ---------------------------------------------------------------------------
# Laps and derived channels
# ---------------------------------------------------------------------------

def _build_laps(beacons: list[float], segment_times: list[float],
                duration: float | None = None) -> pd.DataFrame:
    """
    Build one row per lap from the beacon markers, numbered like Race Studio.
    Columns:
      - lap: 0 for the out-lap, 1..N for complete laps, N+1 for the in-lap
      - type: "out", "lap" or "in"
      - start, end: session time (s) where the lap begins and ends
      - lap_time: AiM's own lap time (s) from "Segment Times"
                  (falls back to end - start if segment times are missing)
      - lap_time_str: the same time as text, e.g. "1:58.432"
    The first and last segments are always partial (recording start to the first
    line crossing, last crossing to recording end), so they are the out-lap and
    in-lap. With fewer than 3 segments there is no complete lap between them,
    so every segment is a "lap" numbered from 1.
    If the file has no beacons, the whole session becomes lap 1.
    """
    ends = list(beacons)
    if not ends:
        ends = [duration if duration is not None else float("inf")]
    starts = [0.0] + ends[:-1]
    n = len(ends)

    # Use AiM's lap times when they match the beacons one to one
    if len(segment_times) == n:
        lap_times = list(segment_times)
    else:
        lap_times = [round(e - s, 3) for s, e in zip(starts, ends)]

    # Race Studio numbering: out-lap 0, complete laps from 1, in-lap last
    if n >= 3:
        numbers = list(range(n))
        types = ["out"] + ["lap"] * (n - 2) + ["in"]
    else:
        numbers = list(range(1, n + 1))
        types = ["lap"] * n

    laps = pd.DataFrame({
        "lap": numbers,
        "type": types,
        "start": starts,
        "end": ends,
        "lap_time": lap_times,
    })
    laps["lap_time_str"] = [f"{int(t // 60)}:{t % 60:06.3f}" for t in laps["lap_time"]]
    return laps


def _assign_laps(df: pd.DataFrame, laps: pd.DataFrame) -> pd.DataFrame:
    """
    Tag every sample with its lap. No sample is dropped.
    Adds two columns at the front of the DataFrame:
      - lap: the lap whose [start, end) contains the sample time
      - lap_elapsed: seconds since the start of that lap
    Samples after the last beacon (if any) are kept in the last lap.
    """
    df = df.copy()
    t = df.index.to_numpy()

    # For each sample find the first lap whose end is after the sample time
    pos = np.searchsorted(laps["end"].to_numpy(), t, side="right")
    pos = np.clip(pos, 0, len(laps) - 1)

    df.insert(0, "lap", laps["lap"].to_numpy()[pos])
    df.insert(1, "lap_elapsed", np.round(t - laps["start"].to_numpy()[pos], 3))
    return df


def _add_distance(df: pd.DataFrame, speed_col: str = "GPS Speed") -> pd.DataFrame:
    """
    Add distance channels by integrating speed (km/h -> m/s) over time.
    Must run after _assign_laps (it needs "lap_elapsed").
      - distance: metres from the start of the session
      - lap_distance: metres from the start/finish line of the current lap
    lap_distance is the x-axis to use when overlaying laps.
    The speed channel itself is never modified. If it is missing,
    the DataFrame is returned unchanged.
    """
    if speed_col not in df.columns:
        return df

    df = df.copy()
    t = df.index.to_numpy(dtype=float)

    # Fill any gaps in speed for the calculation only (the channel is untouched)
    v = df[speed_col].interpolate(limit_direction="both").to_numpy() / 3.6

    # Trapezoidal integration: average speed of two samples * time between them
    step = (v[1:] + v[:-1]) / 2 * np.diff(t)
    distance = np.concatenate([[0.0], np.cumsum(step)])

    # Distance at the exact moment the lap started (line crossing between samples)
    lap_start_time = t - df["lap_elapsed"].to_numpy()
    lap_start_distance = np.interp(lap_start_time, t, distance)

    df["distance"] = np.round(distance, 2)
    df["lap_distance"] = np.round(distance - lap_start_distance, 2)
    return df


def _drop_in_out_laps(df: pd.DataFrame, laps: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Remove the out-lap and the in-lap from the data and the lap table,
    keeping only complete laps (type "lap").
      - Lap numbers do not change: complete laps stay 1..N as in Race Studio
      - Must run after _add_distance, so distance is computed on the full data
    Returns (df, laps).
    """
    keep = laps.loc[laps["type"] == "lap", "lap"]
    laps = laps[laps["lap"].isin(keep)].reset_index(drop=True)
    df = df[df["lap"].isin(keep)]
    return df, laps


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _validate(meta: dict, df: pd.DataFrame, laps: pd.DataFrame) -> list[str]:
    """
    Sanity checks on the full recording, before any lap is removed.
    Returns human-readable warnings; never raises and never changes the data.
    df must be the raw data from _read_data (no derived columns).
      1. Last beacon matches Duration
      2. Beacon differences match Segment Times
      3. Recording covers the whole session (within 1 s)
      4. Time step is constant (no gaps or duplicates)
      5. Empty or non-numeric values per channel
      6. Channels with one constant value (e.g. unused math channels)
      7. Identical consecutive rows lasting 1 s or more (frozen logger)
    """
    warnings = []
    beacons = meta.get("Beacon Markers", [])
    segments = meta.get("Segment Times", [])
    duration = meta.get("Duration")
    rate = meta.get("Sample Rate")
    t = df.index.to_numpy(dtype=float)

    # 1. The last beacon marks the end of the session
    if beacons and duration is not None and abs(beacons[-1] - duration) > 0.01:
        warnings.append(f"Last beacon ({beacons[-1]} s) does not match Duration ({duration} s).")

    # 2. Time between beacons should equal AiM's own lap times
    if len(beacons) != len(segments):
        warnings.append(f"{len(beacons)} beacon markers but {len(segments)} segment times.")
    else:
        diffs = np.diff([0.0] + list(beacons))
        for lap, (d, s) in enumerate(zip(diffs, segments), start=1):
            if abs(d - s) > 0.01:
                warnings.append(f"Lap {lap}: beacons give {d:.3f} s, Segment Times say {s:.3f} s.")

    # 3. The samples should span the whole session
    if duration is not None and len(t):
        if t[0] > 1 or abs(t[-1] - duration) > 1:
            warnings.append(f"Samples cover {t[0]:.2f}-{t[-1]:.2f} s but Duration is {duration} s.")

    # 4. Every step should be exactly 1 / Sample Rate
    if rate and len(t) > 1:
        step = np.round(np.diff(t), 3)
        bad = np.flatnonzero(step != round(1 / rate, 3))
        if len(bad):
            warnings.append(f"{len(bad)} irregular time step(s) (gaps or duplicates), "
                            f"first at {t[bad[0]]:.2f} s.")

    # 5. Values that could not be read as numbers became empty cells
    for col, count in df.isna().sum().items():
        if count:
            warnings.append(f"{col}: {count} empty or non-numeric values.")

    # 6. A channel with a single value carries no information
    for col in df.columns:
        values = df[col].dropna().unique()
        if len(values) == 1:
            warnings.append(f"{col} has the same value ({values[0]}) in every sample.")

    # 7. Runs of identical consecutive rows: the logger repeated its last values
    same = df.eq(df.shift()).all(axis=1).to_numpy()
    min_rows = int(rate) if rate else 20
    i = 0
    while i < len(same):
        if not same[i]:
            i += 1
            continue
        j = i
        while j < len(same) and same[j]:
            j += 1
        # Rows i-1 .. j-1 are all copies of row i-1
        if j - i >= min_rows:
            pos = min(np.searchsorted(laps["end"].to_numpy(), t[i - 1], side="right"), len(laps) - 1)
            row = laps.iloc[pos]
            where = f"lap {row['lap']}" + ("" if row["type"] == "lap" else f", {row['type']}-lap")
            warnings.append(
                f"Samples {t[i - 1]:.2f}-{t[j - 1]:.2f} s ({j - i + 1} rows, {where}) "
                f"are identical: the logger repeated its last values."
            )
        i = j

    return warnings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _natural_key(path: Path) -> list:
    """
    Sort key that compares the numbers inside file names as numbers,
    so "2.csv" comes before "10.csv" and "10.csv" before "46.csv".
    """
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", path.name)]


def _load_folder(folder: str | Path, keep_in_out: bool = False) -> dict:
    """
    Load every AiM CSV file in a folder (subfolders are not searched).
    Called by Session(folder). Returns a dict: file name -> Session.
      - Files are loaded in natural order: 2.csv, 10.csv, 46.csv
      - A file that cannot be loaded (not an AiM export, incomplete upload, ...)
        is skipped and reported; the other files still load
      - keep_in_out is passed to every Session
    """
    folder = Path(folder)
    paths = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".csv"),
                   key=_natural_key)
    print(f"Loading {len(paths)} CSV file(s) from {folder}")

    sessions = {}
    for path in paths:
        try:
            sessions[path.name] = Session(path, keep_in_out=keep_in_out)
            print(f"  ok    {path.name}: {sessions[path.name]}")
        except Exception as e:
            # The first line of the error is enough to know why the file was skipped
            reason = str(e).splitlines()[0] if str(e) else ""
            print(f"  skip  {path.name}: {type(e).__name__}: {reason}")

    print(f"Loaded {len(sessions)} of {len(paths)} file(s).")
    return sessions


class Session:
    """
    One AiM CSV file, parsed and ready to use.
    Given a folder instead of a file, returns a dict: file name -> Session.

    Usage:
        session = Session("my_session.csv")
        session.data        # samples of the complete laps, indexed by time (s)
        session.laps        # lap table (lap, type, start, end, lap_time, lap_time_str)
        session.metadata    # track, vehicle, driver, date, sample rate, ...
        session.units       # channel name -> unit
        session.get_lap(3)  # samples of one lap
        session.export("out", fmt="xlsx")  # files for Excel users
        validate(session)   # optional sanity checks

        sessions = Session("my_folder")   # dict: file name -> Session
        validate(sessions)                # checks every file

    The out-lap and in-lap are removed from data and laps.
    Use Session(path, keep_in_out=True) to keep them.
    """

    def __new__(cls, path: str | Path | None = None, *args, **kwargs):
        # A folder gives one Session per CSV file, returned as a dict
        if path is not None and Path(path).is_dir():
            return _load_folder(path, *args, **kwargs)
        return super().__new__(cls)

    def __init__(self, path: str | Path, keep_in_out: bool = False):
        self.path = str(path)

        # 1. Read the file as text lines and detect the export format
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
        sep, decimal = _detect_format(lines)

        # 2. Session info, lap markers, channel names and units
        self.metadata = _parse_metadata(lines, sep)
        header_idx = _find_header_row(lines, sep)
        names, self.units = _parse_channels(lines, header_idx, sep)
        self.name_map = {_clean_name(n): n for n in names}

        # 3. Samples exactly as recorded (validate checks these)
        self.raw = _read_data(path, header_idx, names, sep, decimal)

        # 4. All laps, then lap number and distance on every sample
        self.all_laps = _build_laps(
            self.metadata.get("Beacon Markers", []),
            self.metadata.get("Segment Times", []),
            self.metadata.get("Duration"),
        )
        data = _add_distance(_assign_laps(self.raw, self.all_laps))

        # 5. Remove the out-lap and in-lap unless asked to keep them
        if keep_in_out:
            self.data, self.laps = data, self.all_laps
        else:
            self.data, self.laps = _drop_in_out_laps(data, self.all_laps)

        # Filled in by validate(session)
        self.warnings = None

    def __repr__(self) -> str:
        # Short summary instead of printing whole DataFrames
        m = self.metadata
        when = m.get("Datetime") or m.get("Date", "")
        checked = "not validated" if self.warnings is None else f"{len(self.warnings)} warnings"
        return (f"Session({m.get('Session', '?')} | {m.get('Vehicle', '?')} | "
                f"{m.get('Racer', '?')} | {when} | {len(self.laps)} laps | "
                f"{len(self.data)} samples | {checked})")

    def get_lap(self, lap: int) -> pd.DataFrame:
        """
        Return the samples of one lap as a new DataFrame.
        The index stays session time (s); use the "lap_elapsed" or
        "lap_distance" columns as the x-axis when comparing laps.
        Raises ValueError with a clear message if the lap is not available.
        """
        if lap in self.laps["lap"].values:
            return self.data[self.data["lap"] == lap].copy()

        # The lap exists in the file but was removed (out-lap or in-lap)
        removed = self.all_laps[self.all_laps["lap"] == lap]
        if len(removed):
            kind = removed["type"].iloc[0]
            raise ValueError(
                f"Lap {lap} is the {kind}-lap and was removed. "
                f"Use Session(path, keep_in_out=True) to keep it."
            )

        available = self.laps["lap"].tolist()
        raise ValueError(f"Lap {lap} does not exist. Available laps: {available}")

    def export(self, folder: str | Path = ".", fmt: str = "csv", per_lap: bool = False) -> list[Path]:
        """
        Write the session to files for people who do not use Python.
          - fmt="csv":  <name>_laps.csv (lap table) and <name>_data.csv (all samples)
          - fmt="xlsx": <name>.xlsx with a "laps" sheet and a "data" sheet
          - per_lap=True: one CSV file / one sheet per lap (lap01, lap02, ...)
            instead of a single "data" table
        <name> is the original file name without extension, e.g. "my_session".
        CSV files use "," between values and "." for decimals. Excel with Greek
        or other European settings may not split them into columns, so for
        Excel use fmt="xlsx", which opens correctly everywhere.
        Returns the list of files written.
        """
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        name = Path(self.path).stem

        # One table per lap, or one table with every sample
        if per_lap:
            tables = {f"lap{lap:02d}": self.data[self.data["lap"] == lap]
                      for lap in self.laps["lap"]}
        else:
            tables = {"data": self.data}

        written = []
        if fmt == "csv":
            laps_path = folder / f"{name}_laps.csv"
            self.laps.to_csv(laps_path, index=False)
            written.append(laps_path)
            for table_name, table in tables.items():
                path = folder / f"{name}_{table_name}.csv"
                table.to_csv(path)
                written.append(path)
        elif fmt == "xlsx":
            path = folder / f"{name}.xlsx"
            with pd.ExcelWriter(path) as writer:
                self.laps.to_excel(writer, sheet_name="laps", index=False)
                for table_name, table in tables.items():
                    table.to_excel(writer, sheet_name=table_name)
            written.append(path)
        else:
            raise ValueError(f'Unknown format {fmt!r}: use "csv" or "xlsx".')

        return written


def validate(session: Session | dict) -> bool:
    """
    Run sanity checks on a Session and print what was found.
    Also accepts the dict returned by Session(folder) and checks every file.
    The checks look at the full recording as read from the file,
    including the out-lap and in-lap even if they were removed.
    The data is never changed. Warnings are stored in session.warnings.
    Returns True if no issues were found (in every file, for a dict).
    """
    if isinstance(session, dict):
        results = [validate(s) for s in session.values()]
        return all(results)

    session.warnings = _validate(session.metadata, session.raw, session.all_laps)

    name = Path(session.path).name
    if not session.warnings:
        print(f"{name}: no issues found.")
    else:
        print(f"{name}: {len(session.warnings)} warning(s)")
        for w in session.warnings:
            print(" -", w)
    return not session.warnings


def get_version() -> str:
    """
    Return the version of logger_data, e.g. "0.2.0".
    Useful when comparing results between teammates: same version, same parsing.
    """
    return __version__
