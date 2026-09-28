#!/usr/bin/env python3
"""
Drift-corrected state-of-charge estimate.

The JBD BMS on gw-e3aba4 reads 0.0 A at rest, so a continuous load wired
upstream of it (the ~0.2 A cellular gateway) never reaches its coulomb counter.
Its SOC therefore reads high and drifts by roughly 5 points a day until the
next full charge resynchronizes it; on 2026-08-13 it showed ~75% while the pack
was empty.

This module recomputes SOC independently of the BMS's own counter:

  * 100% at a "true full" sample: pack voltage at or above 14.4 V with a small
    positive tail current (the end of a CV charge, not the ~30 A bulk phase).
  * From there, integrate the measured pack current and subtract a configured
    unmeasured drain, saturating at 0 and 100.
  * Cap the result from the minimum cell voltage where it means something.
    LiFePO4 is flat between ~20% and ~90%, so only the low end is usable:
    <= 3.20 V / 3.10 V after 15 min at rest caps at 20% / 10%, and <= 3.00 V
    under light discharge means empty.

The estimate is computed on read. Nothing is persisted: spool replays deliver
old timestamps late, and a stored value would be wrong after every one.

Samples are integrated over DISTINCT timestamps -- the telemetry table holds
duplicate rows (see TELEMETRY-DEDUPLICATION-PLAN.md), and counting a copy twice
would double its charge. Each sample's dt is capped, so a telemetry gap is
reported as unmeasured time instead of being filled with a guess.
"""

import os
import threading
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import database_queries


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == '':
        return default
    try:
        return float(value)
    except ValueError:
        print(f"Ignoring invalid {name}={value!r}; using {default}")
        return default


@dataclass(frozen=True)
class SocConfig:
    capacity_ah: float = 100.0
    # Load drawn from the pack that the BMS never measures. Set to 0 once the
    # gateway is rewired behind the BMS.
    unmeasured_drain_a: float = 0.20
    # Telemetry arrives every ~10 s. A longer interval integrates at most this
    # many seconds; the rest is counted as unmeasured gap time.
    max_sample_dt_s: float = 60.0
    full_voltage_v: float = 14.4
    full_tail_max_a: float = 1.5
    rest_current_a: float = 0.5
    rest_min_s: float = 900.0
    # (min cell voltage at rest, SOC ceiling), lowest voltage first.
    rest_floors: Tuple[Tuple[float, float], ...] = ((3.10, 10.0), (3.20, 20.0))
    # Below the LiFePO4 knee the pack is empty whatever the counter says. This
    # holds under light load too, so it does not wait for rest -- only for the
    # reading to persist, so one sagging sample cannot zero the estimate.
    empty_cell_v: float = 3.00
    empty_max_discharge_a: float = 10.0
    empty_min_s: float = 120.0

    @classmethod
    def from_env(cls) -> 'SocConfig':
        defaults = cls()
        return cls(
            capacity_ah=_env_float('SOC_CAPACITY_AH', defaults.capacity_ah),
            unmeasured_drain_a=_env_float(
                'SOC_UNMEASURED_DRAIN_A', defaults.unmeasured_drain_a
            ),
            max_sample_dt_s=_env_float(
                'SOC_MAX_SAMPLE_DT_S', defaults.max_sample_dt_s
            ),
        )


@dataclass
class SocEstimator:
    """Streaming estimator. Feed samples in ascending, distinct timestamp order."""

    config: SocConfig
    soc_pct: Optional[float] = None
    last_ts: Optional[int] = None
    last_current_a: Optional[float] = None
    last_full_ts: Optional[int] = None
    gap_seconds: float = 0.0
    rest_since_ts: Optional[int] = None
    empty_since_ts: Optional[int] = None
    floor_pct: Optional[float] = None

    def is_true_full(self, voltage_v: Optional[float], current_a: Optional[float]) -> bool:
        if voltage_v is None or current_a is None:
            return False
        return (
            voltage_v >= self.config.full_voltage_v
            and 0.0 < current_a < self.config.full_tail_max_a
        )

    def update(
        self,
        ts: int,
        voltage_v: Optional[float],
        current_a: Optional[float],
        min_cell_v: Optional[float],
    ) -> Optional[float]:
        cfg = self.config
        dt = 0.0
        gapped = False
        if self.last_ts is not None:
            dt = float(ts - self.last_ts)
            if dt <= 0:
                # Out of order or a repeated timestamp: not ours to integrate.
                return self.soc_pct
            gapped = dt > cfg.max_sample_dt_s

        if self.soc_pct is not None and dt > 0:
            used = min(dt, cfg.max_sample_dt_s)
            self.gap_seconds += dt - used
            # Zero-order hold: the previous reading's current is in effect
            # until this sample.
            held_current = self.last_current_a or 0.0
            delta_ah = (held_current - cfg.unmeasured_drain_a) * used / 3600.0
            self.soc_pct = min(
                100.0, max(0.0, self.soc_pct + delta_ah / cfg.capacity_ah * 100.0)
            )

        at_rest = current_a is not None and abs(current_a) <= cfg.rest_current_a
        if not at_rest or gapped:
            # A gap breaks the rest interval too: we cannot see what happened.
            self.rest_since_ts = ts if at_rest else None
        elif self.rest_since_ts is None:
            self.rest_since_ts = ts

        at_knee = (
            min_cell_v is not None
            and current_a is not None
            and min_cell_v <= cfg.empty_cell_v
            and -cfg.empty_max_discharge_a <= current_a <= 0.0
        )
        if not at_knee or gapped:
            self.empty_since_ts = ts if at_knee else None
        elif self.empty_since_ts is None:
            self.empty_since_ts = ts

        if self.is_true_full(voltage_v, current_a):
            self.soc_pct = 100.0
            self.last_full_ts = ts
            self.gap_seconds = 0.0
            self.floor_pct = None
        elif (
            self.soc_pct is not None
            and self.empty_since_ts is not None
            and ts - self.empty_since_ts >= cfg.empty_min_s
        ):
            if self.soc_pct > 0.0:
                self.soc_pct = 0.0
                self.floor_pct = 0.0
        elif (
            self.soc_pct is not None
            and min_cell_v is not None
            and self.rest_since_ts is not None
            and ts - self.rest_since_ts >= cfg.rest_min_s
        ):
            for threshold_v, ceiling_pct in cfg.rest_floors:
                if min_cell_v <= threshold_v:
                    if self.soc_pct > ceiling_pct:
                        self.soc_pct = ceiling_pct
                        self.floor_pct = ceiling_pct
                    break

        self.last_ts = ts
        self.last_current_a = current_a
        return self.soc_pct

    def snapshot(self) -> Dict:
        cfg = self.config
        hours_since_full = None
        if self.last_full_ts is not None and self.last_ts is not None:
            hours_since_full = round((self.last_ts - self.last_full_ts) / 3600.0, 2)
        return {
            'corrected_soc_pct': (
                round(self.soc_pct, 1) if self.soc_pct is not None else None
            ),
            'is_estimate': True,
            'as_of': self.last_ts,
            'last_full_at': self.last_full_ts,
            'hours_since_full': hours_since_full,
            'unmeasured_gap_hours': round(self.gap_seconds / 3600.0, 2),
            # The lowest voltage ceiling applied since the last full, if any.
            'voltage_floor_applied_pct': self.floor_pct,
            'unmeasured_drain_a': cfg.unmeasured_drain_a,
            'capacity_ah': cfg.capacity_ah,
        }


# --- Database access ---------------------------------------------------------

# One sample per distinct timestamp. Duplicate rows carry identical values, so
# averaging them is the same as picking one, and it is also well defined if a
# device ever sends two genuinely different samples within one second.
_SAMPLE_SQL = """
    SELECT timestamp,
           AVG(pack_voltage_v) AS pack_voltage_v,
           AVG(pack_current_a) AS pack_current_a,
           MIN(min_cell_voltage_v) AS min_cell_voltage_v
    FROM bms_telemetry
    WHERE bms_id = ? AND timestamp_valid = 1
      AND timestamp >= ? AND timestamp <= ?
    GROUP BY timestamp
    ORDER BY timestamp ASC
"""


def _find_anchor_ts(conn, bms_id: str, at_or_before: int, cfg: SocConfig) -> Optional[int]:
    row = conn.execute(
        """
        SELECT MAX(timestamp) AS anchor_ts
        FROM bms_telemetry
        WHERE bms_id = ? AND timestamp_valid = 1 AND timestamp <= ?
          AND pack_voltage_v >= ?
          AND pack_current_a > 0 AND pack_current_a < ?
        """,
        (bms_id, at_or_before, cfg.full_voltage_v, cfg.full_tail_max_a),
    ).fetchone()
    return row[0] if row else None


def _iter_samples(conn, bms_id: str, start_ts: int, end_ts: int) -> Iterable[Tuple]:
    cursor = conn.execute(_SAMPLE_SQL, (bms_id, start_ts, end_ts))
    while True:
        rows = cursor.fetchmany(5000)
        if not rows:
            return
        for row in rows:
            yield row[0], row[1], row[2], row[3]


def estimate_series(
    bms_id: str,
    start_ts: int,
    end_ts: int,
    config: Optional[SocConfig] = None,
) -> Tuple[List[Tuple[int, Optional[float]]], SocEstimator]:
    """Replay from the last true full at or before start_ts through end_ts.

    Returns (timestamp, corrected SOC) for every distinct sample inside
    [start_ts, end_ts], plus the estimator in its final state. Samples before
    the first true full on record have no estimate (None).
    """
    cfg = config or SocConfig.from_env()
    estimator = SocEstimator(cfg)
    series: List[Tuple[int, Optional[float]]] = []
    conn = database_queries.create_db_connection(use_row_factory=False)
    try:
        anchor = _find_anchor_ts(conn, bms_id, start_ts, cfg)
        replay_from = anchor if anchor is not None else start_ts
        for ts, voltage, current, min_cell in _iter_samples(
            conn, bms_id, replay_from, end_ts
        ):
            soc = estimator.update(ts, voltage, current, min_cell)
            if ts >= start_ts:
                series.append((ts, soc))
    finally:
        conn.close()
    return series, estimator


def annotate_records(
    records: List[Dict],
    bms_id: Optional[str],
    bucket_seconds: Optional[int],
    config: Optional[SocConfig] = None,
) -> None:
    """Add corrected_soc_pct to dashboard view records in place.

    Raw records get the value at their own timestamp. Bucketed records get the
    mean over the bucket, matching how state_of_charge_pct is aggregated.
    Mixed-device views (no bms_id) get None: the estimate is per pack.
    """
    if not records:
        return
    if not bms_id:
        for record in records:
            record['corrected_soc_pct'] = None
        return

    start_ts = int(records[0]['timestamp'])
    end_ts = int(records[-1]['timestamp'])
    if bucket_seconds:
        end_ts += int(bucket_seconds) - 1
    series, _ = estimate_series(bms_id, start_ts, end_ts, config)

    if bucket_seconds:
        sums: Dict[int, List[float]] = {}
        for ts, soc in series:
            if soc is None:
                continue
            bucket = (ts // bucket_seconds) * bucket_seconds
            acc = sums.setdefault(bucket, [0.0, 0])
            acc[0] += soc
            acc[1] += 1
        for record in records:
            acc = sums.get(int(record['timestamp']))
            record['corrected_soc_pct'] = (
                round(acc[0] / acc[1], 2) if acc else None
            )
    else:
        by_ts = dict(series)
        for record in records:
            soc = by_ts.get(int(record['timestamp']))
            record['corrected_soc_pct'] = round(soc, 2) if soc is not None else None


# --- Latest-value cache ------------------------------------------------------

class LatestSocCache:
    """Per-device estimator advanced incrementally by row id.

    The dashboard asks for the latest estimate on every telemetry push, so
    replaying from the last full each time would be wasteful. New rows are
    folded in when they extend the series; a row that lands inside the
    already-integrated range (a spool replay, not a duplicate) forces a full
    replay from the last true full.
    """

    def __init__(self, config_factory=SocConfig.from_env):
        self._config_factory = config_factory
        self._lock = threading.Lock()
        self._entries: Dict[str, Tuple[SocEstimator, int]] = {}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _rebuild(self, conn, bms_id: str, cfg: SocConfig) -> Tuple[SocEstimator, int]:
        max_id_row = conn.execute(
            "SELECT MAX(id) FROM bms_telemetry WHERE bms_id = ?", (bms_id,)
        ).fetchone()
        max_id = max_id_row[0] or 0
        latest_row = conn.execute(
            """
            SELECT MAX(timestamp) FROM bms_telemetry
            WHERE bms_id = ? AND timestamp_valid = 1 AND id <= ?
            """,
            (bms_id, max_id),
        ).fetchone()
        estimator = SocEstimator(cfg)
        latest_ts = latest_row[0] if latest_row else None
        if latest_ts is None:
            return estimator, max_id
        anchor = _find_anchor_ts(conn, bms_id, latest_ts, cfg)
        if anchor is None:
            # Never seen a true full: there is no reference to count from.
            estimator.last_ts = latest_ts
            return estimator, max_id
        cursor = conn.execute(
            """
            SELECT timestamp,
                   AVG(pack_voltage_v), AVG(pack_current_a), MIN(min_cell_voltage_v)
            FROM bms_telemetry
            WHERE bms_id = ? AND timestamp_valid = 1
              AND timestamp >= ? AND id <= ?
            GROUP BY timestamp
            ORDER BY timestamp ASC
            """,
            (bms_id, anchor, max_id),
        )
        for ts, voltage, current, min_cell in cursor:
            estimator.update(ts, voltage, current, min_cell)
        return estimator, max_id

    def _advance(
        self, conn, bms_id: str, estimator: SocEstimator, seen_id: int
    ) -> Optional[Tuple[SocEstimator, int]]:
        """Fold rows newer than seen_id in; None means a replay is required."""
        rows = conn.execute(
            """
            SELECT new.id, new.timestamp, new.pack_voltage_v,
                   new.pack_current_a, new.min_cell_voltage_v
            FROM bms_telemetry AS new
            WHERE new.bms_id = ? AND new.id > ? AND new.timestamp_valid = 1
              AND NOT EXISTS (
                  SELECT 1 FROM bms_telemetry AS seen
                  WHERE seen.bms_id = new.bms_id
                    AND seen.timestamp = new.timestamp
                    AND seen.timestamp_valid = 1
                    AND seen.id < new.id
              )
            ORDER BY new.timestamp ASC, new.id ASC
            """,
            (bms_id, seen_id),
        ).fetchall()
        max_id_row = conn.execute(
            "SELECT MAX(id) FROM bms_telemetry WHERE bms_id = ?", (bms_id,)
        ).fetchone()
        max_id = max(seen_id, max_id_row[0] or 0)
        if not rows:
            return estimator, max_id
        if estimator.last_ts is not None and rows[0][1] <= estimator.last_ts:
            return None
        # Rows are already one per timestamp (NOT EXISTS keeps the first copy).
        for _, ts, voltage, current, min_cell in rows:
            estimator.update(ts, voltage, current, min_cell)
        return estimator, max_id

    def get(self, bms_id: Optional[str]) -> Optional[Dict]:
        if not bms_id:
            return None
        cfg = self._config_factory()
        with self._lock:
            conn = database_queries.create_db_connection(use_row_factory=False)
            try:
                entry = self._entries.get(bms_id)
                result = None
                if entry is not None and entry[0].config == cfg:
                    result = self._advance(conn, bms_id, entry[0], entry[1])
                if result is None:
                    result = self._rebuild(conn, bms_id, cfg)
                self._entries[bms_id] = result
                return result[0].snapshot()
            finally:
                conn.close()


latest_soc_cache = LatestSocCache()


def get_latest_corrected_soc(bms_id: Optional[str]) -> Optional[Dict]:
    return latest_soc_cache.get(bms_id)
