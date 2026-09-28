import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard_server
import database_queries
import soc_estimate
from soc_estimate import SocConfig, SocEstimator

BASE_TS = 1_790_000_000
BMS_ID = "gw-test"


def full_sample(ts):
    """A CV tail-current sample: 14.45 V at 0.8 A."""
    return (ts, 14.45, 0.8, 3.60)


def rest_sample(ts, min_cell=3.30, current=0.0):
    return (ts, 13.2, current, min_cell)


class SocEstimatorTest(unittest.TestCase):
    def run_samples(self, samples, **config):
        estimator = SocEstimator(SocConfig(**config))
        for sample in samples:
            estimator.update(*sample)
        return estimator

    def test_no_estimate_before_first_true_full(self):
        estimator = self.run_samples([rest_sample(BASE_TS + i * 10) for i in range(10)])
        self.assertIsNone(estimator.soc_pct)
        self.assertIsNone(estimator.snapshot()['hours_since_full'])

    def test_bulk_charge_at_high_voltage_is_not_a_true_full(self):
        estimator = SocEstimator(SocConfig())
        self.assertFalse(estimator.is_true_full(14.5, 30.0))
        self.assertFalse(estimator.is_true_full(14.5, 0.0))
        self.assertFalse(estimator.is_true_full(14.3, 0.8))
        self.assertTrue(estimator.is_true_full(14.4, 0.8))

    def test_unmeasured_drain_is_subtracted_at_rest(self):
        # Ten hours at a BMS-reported 0.0 A with a 0.2 A hidden load is
        # 2 Ah, or 2% of 100 Ah.
        samples = [full_sample(BASE_TS)]
        samples += [rest_sample(BASE_TS + i * 10) for i in range(1, 3601)]
        estimator = self.run_samples(samples)
        # The first interval holds the full sample's +0.8 A, which the 100%
        # ceiling absorbs, so 35,990 s of drain remain.
        self.assertAlmostEqual(estimator.soc_pct, 100.0 - 0.2 * 35990 / 3600, places=6)
        snapshot = estimator.snapshot()
        self.assertEqual(snapshot['hours_since_full'], 10.0)
        self.assertTrue(snapshot['is_estimate'])

    def test_zero_drain_holds_at_full(self):
        samples = [full_sample(BASE_TS)]
        samples += [rest_sample(BASE_TS + i * 10) for i in range(1, 361)]
        estimator = self.run_samples(samples, unmeasured_drain_a=0.0)
        self.assertEqual(estimator.soc_pct, 100.0)

    def test_measured_discharge_is_integrated(self):
        # 1 h at -10 A measured plus the 0.2 A drain: 10.2 Ah.
        samples = [full_sample(BASE_TS)]
        samples += [(BASE_TS + i * 10, 13.1, -10.0, 3.28) for i in range(1, 361)]
        estimator = self.run_samples(samples)
        # The first interval holds the full sample's +0.8 A, absorbed by the
        # 100% ceiling; the remaining 3,590 s discharge.
        expected = 100.0 - 10.2 * 3590 / 3600.0
        self.assertAlmostEqual(estimator.soc_pct, expected, places=6)

    def test_gap_integrates_at_most_the_cap(self):
        # A 30 A reading followed by two hours of silence must not be held
        # across the gap: that would fabricate 60 Ah of charge.
        samples = [full_sample(BASE_TS)]
        samples += [(BASE_TS + i * 10, 13.1, -5.0, 3.28) for i in range(1, 361)]
        before = self.run_samples(samples).soc_pct
        last_ts = samples[-1][0]
        samples.append((last_ts + 10, 13.8, 30.0, 3.40))
        samples.append((last_ts + 10 + 7200, 13.8, 30.0, 3.40))
        estimator = self.run_samples(samples)
        # At most 10 s at -5 A and 60 s at 30 A, less the drain.
        cap_gain = (30.0 * 60 - 5.0 * 10 - 0.2 * 70) / 3600.0
        self.assertAlmostEqual(estimator.soc_pct, before + cap_gain, places=3)
        self.assertAlmostEqual(
            estimator.snapshot()['unmeasured_gap_hours'], round(7140 / 3600, 2)
        )

    def test_saturates_at_zero_and_one_hundred(self):
        samples = [full_sample(BASE_TS)]
        samples += [(BASE_TS + i * 10, 14.0, 30.0, 3.45) for i in range(1, 100)]
        self.assertEqual(self.run_samples(samples).soc_pct, 100.0)
        samples = [full_sample(BASE_TS)]
        samples += [(BASE_TS + i * 10, 12.9, -100.0, 3.22) for i in range(1, 1000)]
        self.assertEqual(self.run_samples(samples).soc_pct, 0.0)

    def test_rest_floor_caps_after_rest_interval(self):
        samples = [full_sample(BASE_TS)]
        # 14 minutes at rest at 3.19 V: not yet long enough.
        samples += [rest_sample(BASE_TS + i * 10, 3.19) for i in range(1, 85)]
        self.assertGreater(self.run_samples(samples).soc_pct, 90.0)
        samples += [rest_sample(BASE_TS + i * 10, 3.19) for i in range(85, 100)]
        estimator = self.run_samples(samples)
        # Capped at 20%, then drained for the samples after the cap.
        self.assertAlmostEqual(estimator.soc_pct, 20.0, delta=0.01)
        self.assertEqual(estimator.snapshot()['voltage_floor_applied_pct'], 20.0)

    def test_rest_floor_uses_lower_band(self):
        samples = [full_sample(BASE_TS)]
        samples += [rest_sample(BASE_TS + i * 10, 3.08) for i in range(1, 100)]
        self.assertAlmostEqual(self.run_samples(samples).soc_pct, 10.0, delta=0.01)

    def test_load_breaks_rest_interval(self):
        samples = [full_sample(BASE_TS)]
        for i in range(1, 100):
            current = -3.8 if i % 30 == 0 else 0.0
            samples.append(rest_sample(BASE_TS + i * 10, 3.19, current))
        self.assertGreater(self.run_samples(samples).soc_pct, 90.0)

    def test_knee_empties_under_light_load(self):
        samples = [full_sample(BASE_TS)]
        samples += [(BASE_TS + i * 10, 11.9, -3.8, 2.98) for i in range(1, 12)]
        self.assertGreater(self.run_samples(samples).soc_pct, 90.0)
        samples += [(BASE_TS + i * 10, 11.9, -3.8, 2.98) for i in range(12, 15)]
        self.assertEqual(self.run_samples(samples).soc_pct, 0.0)

    def test_knee_ignores_heavy_load_sag(self):
        samples = [full_sample(BASE_TS)]
        samples += [(BASE_TS + i * 10, 11.9, -40.0, 2.98) for i in range(1, 30)]
        self.assertGreater(self.run_samples(samples).soc_pct, 60.0)

    def test_true_full_resets_after_discharge(self):
        samples = [full_sample(BASE_TS)]
        samples += [(BASE_TS + i * 10, 13.0, -20.0, 3.25) for i in range(1, 360)]
        samples.append(full_sample(BASE_TS + 3600))
        estimator = self.run_samples(samples)
        self.assertEqual(estimator.soc_pct, 100.0)
        self.assertEqual(estimator.last_full_ts, BASE_TS + 3600)

    def test_config_from_env(self):
        with mock.patch.dict(os.environ, {
            'SOC_UNMEASURED_DRAIN_A': '0',
            'SOC_CAPACITY_AH': '120',
        }):
            cfg = SocConfig.from_env()
        self.assertEqual(cfg.unmeasured_drain_a, 0.0)
        self.assertEqual(cfg.capacity_ah, 120.0)
        with mock.patch.dict(os.environ, {'SOC_UNMEASURED_DRAIN_A': 'abc'}):
            self.assertEqual(SocConfig.from_env().unmeasured_drain_a, 0.20)


class SocDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "soc.db")
        self.original_path = database_queries.DATABASE_PATH
        database_queries.DATABASE_PATH = self.db_path
        database_queries.ensure_database_schema()
        soc_estimate.latest_soc_cache.clear()
        self.env = mock.patch.dict(os.environ, {'SOC_UNMEASURED_DRAIN_A': '0.2'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        soc_estimate.latest_soc_cache.clear()
        database_queries.DATABASE_PATH = self.original_path
        self.temp_dir.cleanup()

    def insert(self, samples, bms_id=BMS_ID, copies=1):
        conn = database_queries.create_db_connection()
        try:
            for ts, voltage, current, min_cell in samples:
                for _ in range(copies):
                    conn.execute(
                        """
                        INSERT INTO bms_telemetry (
                            bms_id, timestamp, timestamp_valid, pack_voltage_v,
                            pack_current_a, min_cell_voltage_v, state_of_charge_pct,
                            full_capacity_ah
                        ) VALUES (?, ?, 1, ?, ?, ?, 100, 100)
                        """,
                        (bms_id, ts, voltage, current, min_cell),
                    )
            conn.commit()
        finally:
            conn.close()

    def discharge(self, start_ts, count, current=-2.0):
        return [(start_ts + i * 10, 13.1, current, 3.28) for i in range(count)]

    def test_duplicate_rows_do_not_change_the_estimate(self):
        samples = [full_sample(BASE_TS)] + self.discharge(BASE_TS + 10, 720)
        self.insert(samples)
        clean = soc_estimate.get_latest_corrected_soc(BMS_ID)

        other = "gw-dupes"
        self.insert(samples, bms_id=other, copies=3)
        duplicated = soc_estimate.get_latest_corrected_soc(other)
        self.assertEqual(clean['corrected_soc_pct'], duplicated['corrected_soc_pct'])

        series, _ = soc_estimate.estimate_series(other, BASE_TS, BASE_TS + 7200)
        self.assertEqual(len(series), len(samples))

    def test_anchor_is_the_last_full_before_the_window(self):
        self.insert([full_sample(BASE_TS)] + self.discharge(BASE_TS + 10, 720))
        series, _ = soc_estimate.estimate_series(
            BMS_ID, BASE_TS + 3600, BASE_TS + 7200
        )
        self.assertTrue(series[0][1] < 100.0)
        self.assertEqual(series[0][0], BASE_TS + 3600)

    def test_cache_matches_full_replay_after_incremental_and_late_rows(self):
        first = [full_sample(BASE_TS)] + self.discharge(BASE_TS + 10, 300)
        self.insert(first)
        soc_estimate.get_latest_corrected_soc(BMS_ID)

        # New rows extending the series, delivered twice.
        self.insert(self.discharge(BASE_TS + 3100, 100, current=-5.0), copies=2)
        incremental = soc_estimate.get_latest_corrected_soc(BMS_ID)
        _, rebuilt = soc_estimate.estimate_series(BMS_ID, BASE_TS, BASE_TS + 10**6)
        self.assertEqual(incremental['corrected_soc_pct'], rebuilt.snapshot()['corrected_soc_pct'])

        # A spool replay fills a hole behind the watermark.
        self.insert([(BASE_TS + 3005, 13.1, -50.0, 3.28)])
        late = soc_estimate.get_latest_corrected_soc(BMS_ID)
        _, rebuilt = soc_estimate.estimate_series(BMS_ID, BASE_TS, BASE_TS + 10**6)
        self.assertEqual(late['corrected_soc_pct'], rebuilt.snapshot()['corrected_soc_pct'])
        self.assertNotEqual(late['corrected_soc_pct'], incremental['corrected_soc_pct'])

    def test_cache_picks_up_a_new_true_full(self):
        self.insert([full_sample(BASE_TS)] + self.discharge(BASE_TS + 10, 360, -20.0))
        self.assertLess(soc_estimate.get_latest_corrected_soc(BMS_ID)['corrected_soc_pct'], 90)
        self.insert([full_sample(BASE_TS + 4000)])
        latest = soc_estimate.get_latest_corrected_soc(BMS_ID)
        self.assertEqual(latest['corrected_soc_pct'], 100.0)
        self.assertEqual(latest['last_full_at'], BASE_TS + 4000)

    def test_annotate_bucketed_records_uses_bucket_mean(self):
        self.insert([full_sample(BASE_TS)] + self.discharge(BASE_TS + 10, 359))
        records = [{'timestamp': BASE_TS - BASE_TS % 600 + k * 600} for k in range(7)]
        soc_estimate.annotate_records(records, BMS_ID, 600)
        values = [r['corrected_soc_pct'] for r in records if r['corrected_soc_pct'] is not None]
        self.assertTrue(values)
        self.assertEqual(values, sorted(values, reverse=True))

    def test_annotate_without_device_is_none(self):
        records = [{'timestamp': BASE_TS}]
        soc_estimate.annotate_records(records, None, None)
        self.assertIsNone(records[0]['corrected_soc_pct'])

    def test_api_exposes_the_estimate(self):
        now = int(dashboard_server.time.time())
        self.insert([full_sample(now - 3600)] + self.discharge(now - 3590, 359))
        client = dashboard_server.app.test_client()

        latest = client.get(f"/api/latest?bms_id={BMS_ID}").get_json()
        self.assertEqual(latest['state_of_charge_pct'], 100)
        self.assertLess(latest['corrected_soc_pct'], 100)
        self.assertTrue(latest['corrected_soc']['is_estimate'])
        self.assertAlmostEqual(latest['corrected_soc']['hours_since_full'], 1.0, places=1)

        for resolution in ('auto', '1m'):
            payload = client.get(
                f"/api/data?hours=2&bms_id={BMS_ID}&resolution={resolution}"
            ).get_json()
            self.assertTrue(all('corrected_soc_pct' in r for r in payload['records']))
            self.assertIsNotNone(payload['records'][-1]['corrected_soc_pct'])
            self.assertEqual(
                payload['latest_reading']['corrected_soc_pct'],
                latest['corrected_soc_pct'],
            )


if __name__ == '__main__':
    unittest.main()
