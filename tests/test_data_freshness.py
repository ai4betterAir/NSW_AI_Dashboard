import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import URLError

from nowcasting.data import obs_data


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ObservationFreshnessTests(unittest.TestCase):
    def _row(self, date_value, hour):
        return {
            "Site_Id": 39,
            "Date": date_value,
            "Hour": hour,
            "Parameter": {
                "ParameterCode": "PM2.5",
                "Frequency": "Hourly average",
            },
            "Value": 4.2,
        }

    def test_current_values_wrapper_is_accepted(self):
        now = datetime.now(obs_data.SYDNEY_TZ)
        payload = {"Values": [self._row(now.strftime("%Y-%m-%d"), now.hour)]}
        with patch.object(obs_data, "urlopen", return_value=_FakeResponse(payload)), patch.object(
            obs_data, "_save_observations_cache"
        ):
            result = obs_data.fetch_observations_result(timeout=1)

        self.assertEqual("live", result["source"])
        self.assertEqual(1, len(result["rows"]))
        self.assertTrue(result["recent"])

    def test_stale_live_snapshot_is_not_returned(self):
        payload = {"Values": [self._row("2026-05-16", 12)]}
        with patch.object(obs_data, "urlopen", return_value=_FakeResponse(payload)), patch.object(
            obs_data, "_fetch_official_current_report", return_value=[]
        ), patch.object(obs_data, "_load_observations_cache", return_value=[]), patch.object(
            obs_data, "_load_bundled_observations_snapshot", return_value=[]
        ):
            result = obs_data.fetch_observations_result(timeout=1)

        self.assertEqual("error", result["source"])
        self.assertEqual([], result["rows"])
        self.assertFalse(result["recent"])

    def test_history_request_uses_current_api_field_name(self):
        captured = {}
        now = datetime.now(obs_data.SYDNEY_TZ)
        payload = {"Values": [self._row(now.strftime("%Y-%m-%d"), now.hour)]}

        def fake_urlopen(request, timeout=0):
            captured.update(json.loads(request.data.decode("utf-8")))
            return _FakeResponse(payload)

        with patch.object(obs_data, "urlopen", side_effect=fake_urlopen), patch.object(
            obs_data, "_save_observations_cache"
        ):
            rows = obs_data.fetch_observation_history(
                [39],
                ["PM2.5"],
                now.strftime("%Y-%m-%d"),
                now.strftime("%Y-%m-%d"),
                timeout=1,
            )

        self.assertEqual(1, len(rows))
        self.assertEqual(["Hourly"], captured["SubCategories"])

    def test_official_report_parser_returns_api_shaped_rows(self):
        now = datetime.now(obs_data.SYDNEY_TZ).replace(minute=0, second=0, microsecond=0)
        timestamp = now.strftime("%Y%m%d%H%M%S")
        reading_cells = "".join(
            '<TD class="i1">{}</TD>'.format(value)
            for value in ("0.4", "0.3", "0.2", "1.9", "0.58", "0.4", "0.1", "20.8", "15.2")
        )
        payload = """
            <A href="getPage.php?reportid=2&date={timestamp}&load=previous">Previous</A>
            <TR>
              <TD class="region">Sydney East</TD>
              <TD class="site">Alexandria</TD>
              {reading_cells}
              <TD></TD><TD class="i1">GOOD</TD>
            </TR>
        """.format(timestamp=timestamp, reading_cells=reading_cells)

        rows = obs_data._parse_official_current_report(payload)
        pm25 = next(
            row
            for row in rows
            if row["Parameter"]["ParameterCode"] == "PM2.5"
        )
        aqc = next(
            row
            for row in rows
            if row["Parameter"]["ParameterCode"] == "AQC"
        )
        self.assertEqual(15.2, pm25["Value"])
        self.assertEqual("GOOD", aqc["AirQualityCategory"])
        self.assertEqual(str(now.hour), pm25["Hour"])

    def test_blocked_api_uses_fresh_official_report(self):
        now = datetime.now(obs_data.SYDNEY_TZ)
        report_rows = [self._row(now.strftime("%Y-%m-%d"), now.hour)]
        with patch.object(obs_data, "urlopen", side_effect=URLError("blocked")), patch.object(
            obs_data, "_fetch_official_current_report", return_value=report_rows
        ), patch.object(obs_data, "_save_observations_cache"):
            result = obs_data.fetch_observations_result(timeout=1)

        self.assertEqual("official-report", result["source"])
        self.assertEqual(1, len(result["rows"]))
        self.assertIsNone(result["error"])

    def test_dustwatch_feature_rows_are_normalised(self):
        payload = {
            "features": [
                {
                    "attributes": {
                        "site_id": "9880",
                        "parametercode": "PM10d",
                        "parameterdescription": "Particulate Matter (<10 µm) (Dustwatch)",
                        "units": "µg/m³",
                        "category": "Averages",
                        "subcategory": "Hourly",
                        "frequency": "Hourly average",
                        "date": "2026-06-28",
                        "hour": "21",
                        "hourdescription": "8 pm - 9 pm",
                        "value": None,
                        "airqualitycategory": "null",
                        "determiningpollutant": "null",
                        "region": "Western LLS",
                    }
                }
            ]
        }
        with patch.object(obs_data, "urlopen", return_value=_FakeResponse(payload)):
            rows = obs_data._fetch_official_dustwatch_rows(timeout=1)

        self.assertEqual(1, len(rows))
        self.assertEqual("PM10d", rows[0]["Parameter"]["ParameterCode"])
        self.assertIsNone(rows[0]["AirQualityCategory"])

    def test_purpleair_cache_is_marked_as_cache(self):
        now_epoch = datetime.now(timezone.utc).timestamp()
        cached = {
            "source": "live",
            "fetched_at": now_epoch,
            "error": None,
            "sensors": [{"sensor_index": 1, "last_seen": now_epoch}],
        }
        with patch.object(obs_data, "urlopen", side_effect=URLError("offline")), patch.object(
            obs_data, "_load_purpleair_snapshot_cache", return_value=cached
        ):
            result = obs_data.fetch_purpleair_snapshot(timeout=1)

        self.assertEqual("cache", result["source"])
        self.assertEqual(1, len(result["sensors"]))


if __name__ == "__main__":
    unittest.main()
