# app.py
# ---------------------------
# Terminal Data Quality Report (CET)
# ---------------------------
# Run: streamlit run app.py
# Requires: streamlit, pandas, numpy, xlsxwriter
# ---------------------------

import io
import re
import math
import numpy as np
import pandas as pd
import streamlit as st
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

st.set_page_config(page_title="Terminal Data Quality Report", layout="wide")
st.title("📦 Terminal Data Quality Report (CET)")
st.caption("Upload the CSV, pick a date range, and download a formatted Excel report.")

# ----------------------------
# Config / constants
# ----------------------------
NEEDED_HEADERS_HIGHLIGHT = {
    "Stop type",
    "Stop actual arrival time",
    "Stop arrival delta (minutes)",
    "Shipment ID",
    "Origin",
    "Destination initial planned arrival time",
}

# ✅ UPDATED ORDER HERE
TDQ_COL_ORDER = [
    "Origin",
    "Delivery Precision",
    "Planned Deliveries",
    "Actual Deliveries",
    "Registration Rate",
    "Delayed Deliveries",
]

DEFAULT_ORIGINS = ["Brondby", "Hasselager", "Lineage", "Odense", "Hilton", "DF HUB Brondby"]
CET_TZ = ZoneInfo("Europe/Copenhagen")

# ----------------------------
# Dynamic default dates
# ----------------------------
# End Date = yesterday
# Start Date = Monday of last week
_today = date.today()
_default_end = _today - timedelta(days=1)                          # yesterday
_default_start = _today - timedelta(days=_today.weekday() + 7)     # last week's Monday

# ----------------------------
# Helpers
# ----------------------------
def ensure_column_after(df: pd.DataFrame, after_col: str, new_col: str, values) -> pd.DataFrame:
    """
    Overwrite-or-create `new_col` and then move it to be directly after `after_col`.
    Safe to call multiple times (idempotent) to avoid 'already exists' errors.
    """
    df[new_col] = values  # overwrite or create
    cols = list(df.columns)
    if after_col in cols:
        cols.remove(new_col)
        insert_at = cols.index(after_col) + 1
        cols.insert(insert_at, new_col)
        df = df[cols]
    return df

def robust_parse_datetime_utc(series: pd.Series) -> pd.Series:
    """
    Robust UTC parsing:
    1) pandas default
    2) dayfirst=True (common EU)
    3) explicit formats
    """
    s = series.astype(str).str.strip()

    # pass 1: pandas default
    dt = pd.to_datetime(s, utc=True, errors="coerce")

    # pass 2: dayfirst
    mask = dt.isna()
    if mask.any():
        dt2 = pd.to_datetime(s[mask], utc=True, dayfirst=True, errors="coerce")
        dt.loc[mask] = dt2

    # pass 3: explicit formats for stubborn cases
    patterns = [
        "%d-%m-%Y %H:%M",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%Y-%m-%dT%H:%M:%S",        # ISO without Z
        "%Y-%m-%dT%H:%M:%S.%f",    # ISO with microseconds
    ]
    mask = dt.isna()
    if mask.any():
        for pat in patterns:
            try:
                parsed = pd.to_datetime(s[mask], format=pat, utc=True, errors="coerce")
                fill_mask = parsed.notna()
                dt.loc[mask[mask].index[fill_mask]] = parsed[fill_mask]
                mask = dt.isna()
                if not mask.any():
                    break
            except Exception:
                pass

    return dt

def normalize_origin(x: str) -> str:
    """Clean-up mojibake (e.g., 'Br√∏ndby DC' → 'Brondby') and common suffixes."""
    if not isinstance(x, str):
        return ""
    s = x.strip()
    s_lower = s.lower()
    s_lower = s_lower.replace("br√∏ndby", "brondby").replace("brøndby", "brondby")
    s_lower = re.sub(r"\s+dc\b", "", s_lower)
    if "df hub" in s_lower and "brondby" in s_lower:
        return "DF HUB Brondby"
    if "brondby" in s_lower:
        return "Brondby"
    if "hasselager" in s_lower:
        return "Hasselager"
    if "lineage" in s_lower:
        return "Lineage"
    if "odense" in s_lower:
        return "Odense"
    if "hilton" in s_lower:
        return "Hilton"
    return s  # keep original text otherwise

def is_nonblank_datetime(val) -> bool:
    """Count as 'actual' only if non-blank and parseable to a datetime."""
    if pd.isna(val):
        return False
    s = str(val).strip()
    if s == "" or s.lower() in ("nan", "nat", "none"):
        return False
    dt = pd.to_datetime(s, errors="coerce", utc=True)
    return pd.notna(dt)

def filter_by_cet_range(cet_series: pd.Series, start_d: date, end_d: date) -> pd.Series:
    """Keep rows whose CET timestamp is within [start_d 00:00, end_d 23:59:59.999] inclusive."""
    if cet_series.isna().all():
        return cet_series.notna() & False
    start_dt = datetime.combine(start_d, time.min).replace(tzinfo=CET_TZ)
    end_dt = datetime.combine(end_d, time.max).replace(tzinfo=CET_TZ)
    return (cet_series >= pd.Timestamp(start_dt)) & (cet_series <= pd.Timestamp(end_dt))

def make_tz_naive_for_excel(df_in: pd.DataFrame) -> pd.DataFrame:
    """Excel can't store tz-aware datetimes; strip tz info."""
    df_out = df_in.copy()
    for c in df_out.columns:
        if pd.api.types.is_datetime64tz_dtype(df_out[c]):
            df_out[c] = df_out[c].dt.tz_localize(None)
    return df_out

def safe_column_width(series: pd.Series, min_w: int = 12, max_w: int = 40) -> int:
    """
    Robust width estimator from string lengths; never throws.
    Falls back to min_w when data is empty or quantile is NaN/invalid.
    """
    try:
        lengths = series.astype(str).str.len()
        q = lengths.quantile(0.95)
        if pd.isna(q) or not np.isfinite(q):
            return min_w
        return int(max(min_w, min(max_w, math.ceil(q))))
    except Exception:
        return min_w

# ----------------------------
# UI inputs
# ----------------------------
uploaded_csv = st.file_uploader("Upload the main CSV", type=["csv"])

origins = st.multiselect(
    "Origins to include",
    options=DEFAULT_ORIGINS,
    default=DEFAULT_ORIGINS,
    help="Edit as needed. Mojibake like 'Br√∏ndby DC' is normalized to 'Brondby'."
)

col1, col2 = st.columns(2)
with col1:
    start_date = st.date_input("Start Date (CET)", value=_default_start)
with col2:
    end_date = st.date_input("End Date (CET)", value=_default_end)

if end_date < start_date:
    st.error("End Date must be on or after Start Date.")
    st.stop()

run = st.button("Generate Excel Report", type="primary", use_container_width=True)

# ----------------------------
# Main
# ----------------------------
if run:
    if uploaded_csv is None:
        st.error("Please upload the CSV first.")
        st.stop()

    df = pd.read_csv(uploaded_csv)

    # Ensure required headers exist (avoid hard failures if missing)
    for col in NEEDED_HEADERS_HIGHLIGHT:
        if col not in df.columns:
            df[col] = pd.Series([np.nan] * len(df))

    # Validate mandatory field presence
    if "Destination initial planned arrival time" not in df.columns:
        st.error("The CSV must contain the column 'Destination initial planned arrival time'.")
        st.stop()

    # Build CET (always recompute from UTC source)
    utc_series = robust_parse_datetime_utc(df["Destination initial planned arrival time"])
    cet_series = utc_series.dt.tz_convert(CET_TZ)
    df = ensure_column_after(df, "Destination initial planned arrival time", "CET", cet_series)

    # Diagnostics BEFORE filtering
    total_rows = len(df)
    parsed_ok = int(cet_series.notna().sum())
    parsed_ratio = 0 if total_rows == 0 else parsed_ok / total_rows
    cet_min = cet_series.min() if parsed_ok > 0 else None
    cet_max = cet_series.max() if parsed_ok > 0 else None

    with st.expander("🔎 Data diagnostics", expanded=True):
        st.write(f"Total rows: **{total_rows}**")
        st.write(f"Parsed CET timestamps: **{parsed_ok}** ({parsed_ratio:.0%})")
        if cet_min is not None:
            st.write(f"Dataset CET range: **{cet_min}** → **{cet_max}**")
        else:
            st.write("Dataset CET range: _no parsable timestamps_")
        st.write("Sample of original planned-arrival values (first 5 non-null):")
        sample_dates = df.loc[df["Destination initial planned arrival time"].notna(),
                              "Destination initial planned arrival time"].head(5)
        st.write(sample_dates)

    # Filter by CET range
    mask = filter_by_cet_range(df["CET"], start_date, end_date)
    matched_rows = int(mask.sum())
    if matched_rows == 0:
        st.warning(
            "No rows matched the selected CET date range. "
            "Check the **Data diagnostics** above and align Start/End dates with the dataset CET range."
        )

    df_filtered = df.loc[mask].copy()

    # Normalize origins
    df_filtered["_OriginNorm"] = df_filtered["Origin"].apply(normalize_origin)

    # Compute Terminal Data Quality metrics
    results = []
    for origin in origins:
        sub = df_filtered[df_filtered["_OriginNorm"] == origin]

        planned = int(len(sub))
        actual_series = sub["Stop actual arrival time"]
        actual = int(actual_series.apply(is_nonblank_datetime).sum())

        delta = pd.to_numeric(sub["Stop arrival delta (minutes)"], errors="coerce")
        delayed = int((delta > 0).sum())

        reg_rate = (actual / planned) if planned > 0 else np.nan
        del_prec = (1 - (delayed / actual)) if actual > 0 else np.nan

        results.append({
            "Origin": origin,
            "Delivery Precision": del_prec,
            "Planned Deliveries": planned,
            "Actual Deliveries": actual,
            "Registration Rate": reg_rate,
            "Delayed Deliveries": delayed,
        })

    tdq_df = pd.DataFrame(results, columns=TDQ_COL_ORDER)

    # Single "Terminal Quality" header table
    header_table = pd.DataFrame({
        "Terminal Quality": ["Start Date", "End Date", "Carrier"],
        "Value": [start_date.strftime("%d-%b"), end_date.strftime("%d-%b"), "All"]
    })

    # Prepare Excel (strip tz for Excel compatibility)
    df_out = make_tz_naive_for_excel(df_filtered)

    output = io.BytesIO()
    main_sheet_name = "Main (Filtered to CET Range)"
    tdq_sheet = "Terminal Data Quality"

    with pd.ExcelWriter(
        output, engine="xlsxwriter",
        datetime_format="yyyy-mm-dd hh:mm:ss", date_format="yyyy-mm-dd"
    ) as writer:
        # Main data
        df_out.to_excel(writer, index=False, sheet_name=main_sheet_name)
        wb = writer.book
        ws_main = writer.sheets[main_sheet_name]

        # Header formatting
        header_format_default = wb.add_format({
            "bold": True, "text_wrap": True, "valign": "top", "border": 1
        })
        header_format_yellow = wb.add_format({
            "bold": True, "text_wrap": True, "valign": "top", "border": 1, "bg_color": "#FFF59D"
        })

        # Re-write header row with highlight
        for col_idx, col_name in enumerate(df_out.columns):
            fmt = header_format_yellow if col_name in NEEDED_HEADERS_HIGHLIGHT else header_format_default
            ws_main.write(0, col_idx, col_name, fmt)

        # Column widths (robust)
        for idx, col in enumerate(df_out.columns):
            width = safe_column_width(df_out[col])
            ws_main.set_column(idx, idx, width)

        # ── Terminal Data Quality sheet ──

        # Write header_table WITHOUT DataFrame column headers
        header_table.to_excel(
            writer, index=False, header=False,
            sheet_name=tdq_sheet, startrow=0,
        )

        # TDQ data table starts 2 blank rows after the header block
        tdq_start_row = len(header_table) + 2  # rows 0-2 = header block, 3-4 = gap, 5 = TDQ header
        tdq_df.to_excel(
            writer, index=False,
            sheet_name=tdq_sheet, startrow=tdq_start_row,
        )
        ws_tdq = writer.sheets[tdq_sheet]

        # Style header summary table (rows 0-2)
        bold_fmt = wb.add_format({"bold": True, "bg_color": "#E0E0E0", "border": 1})
        val_fmt = wb.add_format({"border": 1, "align": "center"})
        for i in range(len(header_table)):
            ws_tdq.write(i, 0, header_table.iloc[i, 0], bold_fmt)
            ws_tdq.write(i, 1, header_table.iloc[i, 1], val_fmt)

        # TDQ header row: bold + border + centered
        header_fmt_centered = wb.add_format({
            "bold": True, "text_wrap": True, "valign": "top",
            "border": 1, "align": "center",
        })
        for col_idx, col_name in enumerate(tdq_df.columns):
            ws_tdq.write(tdq_start_row, col_idx, col_name, header_fmt_centered)

        # Data cell formats: border + centered, with percentage variant
        border_center_fmt = wb.add_format({"border": 1, "align": "center"})
        pct_center_fmt = wb.add_format({"num_format": "0%", "border": 1, "align": "center"})

        pct_cols = {"Delivery Precision", "Registration Rate"}

        for row_i in range(len(tdq_df)):
            excel_row = tdq_start_row + 1 + row_i
            for col_i, col_name in enumerate(tdq_df.columns):
                val = tdq_df.iloc[row_i, col_i]
                if col_name in pct_cols:
                    ws_tdq.write(excel_row, col_i, val, pct_center_fmt)
                else:
                    ws_tdq.write(excel_row, col_i, val, border_center_fmt)

        # Column widths for TDQ sheet
        for i, name in enumerate(tdq_df.columns):
            ws_tdq.set_column(i, i, 20)

    output.seek(0)

    st.subheader("📄 Terminal Data Quality (Preview)")
    st.dataframe(tdq_df, use_container_width=True)

    st.download_button(
        label="⬇️ Download Excel Report",
        data=output,
        file_name=f"Terminal_Data_Quality_{start_date:%d%b}-{end_date:%d%b}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.partial",
        use_container_width=True,
    )

st.markdown("""
**Notes**
- Times are converted from UTC to CET/CEST (`Europe/Copenhagen`).
- The CET filter uses the selected **Start Date** and **End Date** (inclusive).
- **Actual Deliveries** count only non-blank, parseable datetimes in **Stop actual arrival time**.
- Table order updated to: Origin → Delivery Precision → Planned → Actual → Registration Rate → Delayed.
""")
