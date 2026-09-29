import base64
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import harness_quota as quota

HOUR = 3600
DAY = 24 * HOUR
WEEK = 7 * DAY
NOW = 555555 * HOUR


def weekly_sample(captured_at, used_percent, resets_at, estimated=False):
    window = {
        "minutes": 10080,
        "usedPercent": used_percent,
        "resetsAt": resets_at,
        "estimated": estimated,
    }
    return {"capturedAt": captured_at, "windows": [window]}


def weekly_window(used_percent, resets_at, estimated=False):
    return weekly_sample(NOW, used_percent, resets_at, estimated)["windows"][0]


class QuotaTest(unittest.TestCase):
    def test_extracts_claude_usage_windows(self):
        payload = {
            "five_hour": {"utilization": 42.4, "resets_at": "2033-05-18T03:33:20Z"},
            "seven_day": {"utilization": 18, "resets_at": "2033-05-19T07:20:00+00:00"},
        }

        self.assertEqual(
            quota.extract_claude_windows(payload),
            [
                {"minutes": 300, "usedPercent": 42, "resetsAt": 2000000000, "estimated": False},
                {"minutes": 10080, "usedPercent": 18, "resetsAt": 2000100000, "estimated": False},
                {"minutes": 43200, "usedPercent": 25, "resetsAt": None, "estimated": True},
            ],
        )

    def test_extracts_codex_http_windows(self):
        payload = {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 61,
                    "limit_window_seconds": 18000,
                    "reset_at": 11,
                },
                "secondary_window": {
                    "used_percent": 33,
                    "limit_window_seconds": 604800,
                    "reset_at": 22,
                },
            }
        }

        self.assertEqual(
            quota.extract_codex_windows(payload),
            [
                {"minutes": 300, "usedPercent": 61, "resetsAt": 11, "estimated": False},
                {"minutes": 10080, "usedPercent": 33, "resetsAt": 22, "estimated": False},
                {"minutes": 43200, "usedPercent": 41, "resetsAt": None, "estimated": True},
            ],
        )

    def test_extracts_cursor_usage_windows(self):
        payload = {
            "billingCycleEnd": 2000000000000,
            "planUsage": {
                "autoPercentUsed": 12.4,
                "apiPercentUsed": "34.6",
                "totalPercentUsed": 7.4,
            },
        }

        self.assertEqual(
            quota.extract_cursor_windows(payload),
            [
                {"minutes": 300, "usedPercent": 12, "resetsAt": 2000000000, "estimated": True},
                {"minutes": 10080, "usedPercent": 35, "resetsAt": 2000000000, "estimated": True},
                {"minutes": 43200, "usedPercent": 7, "resetsAt": 2000000000, "estimated": False},
            ],
        )

    def test_computes_cursor_total_when_percentage_is_missing(self):
        payload = {"planUsage": {"limit": 2000, "includedSpend": 500}}
        self.assertEqual(
            quota.extract_cursor_windows(payload),
            [
                {"minutes": 300, "usedPercent": 25, "resetsAt": None, "estimated": True},
                {"minutes": 10080, "usedPercent": 25, "resetsAt": None, "estimated": True},
                {"minutes": 43200, "usedPercent": 25, "resetsAt": None, "estimated": True},
            ],
        )

    def test_estimates_missing_window_from_shortest_and_longest(self):
        self.assertEqual(
            quota.complete_windows(
                [
                    {"minutes": 300, "usedPercent": 20},
                    {"minutes": 43200, "usedPercent": 80},
                ]
            ),
            [
                {"minutes": 300, "usedPercent": 20, "resetsAt": None, "estimated": False},
                {"minutes": 10080, "usedPercent": 62, "resetsAt": None, "estimated": True},
                {"minutes": 43200, "usedPercent": 80, "resetsAt": None, "estimated": False},
            ],
        )

    def test_formats_remaining_percentage_and_warning(self):
        snapshot = {
            "windows": [
                {"minutes": 10080, "usedPercent": 18},
                {"minutes": 300, "usedPercent": 81},
            ]
        }

        self.assertEqual(quota.format_chip("claude", snapshot), "cl 5h19%! 1w82%")

    def test_omits_missing_windows(self):
        snapshot = {"windows": [{"minutes": 300, "usedPercent": 42}]}
        self.assertEqual(quota.format_chip("codex", snapshot), "cx 5h58%")
        self.assertEqual(quota.format_chip("cursor", {"windows": []}), "")
        self.assertEqual(
            quota.format_chip("cursor", {"windows": [{"minutes": 43200, "usedPercent": 7}]}),
            "cr 93%",
        )

    def test_cache_permissions_and_staleness(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temporary_directory}):
                quota.write_snapshot("cursor", [{"minutes": 43200, "usedPercent": 7}])
                path = quota.snapshot_path("cursor")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertIsNotNone(quota.read_snapshot("cursor", max_age=10))

                payload = json.loads(path.read_text())
                payload["capturedAt"] = int(time.time()) - 11
                path.write_text(json.dumps(payload))
                self.assertIsNone(quota.read_snapshot("cursor", max_age=10))

    def test_background_refresh_rejects_arbitrary_lock_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            victim = Path(temporary_directory) / "keep"
            victim.write_text("keep")
            errors = io.StringIO()
            with redirect_stderr(errors):
                exit_code = quota.main(["_background-refresh", "claude", str(victim)])
            victim_exists = victim.exists()

        self.assertEqual(exit_code, 2)
        self.assertIn("invalid background refresh provider", errors.getvalue())
        self.assertTrue(victim_exists)

    def test_refresh_interval_defaults_to_fifteen_minutes(self):
        report_arguments = quota.parser().parse_args([])
        chip_arguments = quota.parser().parse_args(["chip", "claude"])

        self.assertEqual(report_arguments.stale_after, 900)
        self.assertEqual(chip_arguments.refresh_after, 900)

    def test_refresh_interval_can_be_overridden(self):
        report_arguments = quota.parser().parse_args(["--stale-after", "120"])
        chip_arguments = quota.parser().parse_args(["chip", "claude", "--refresh-after", "300"])

        self.assertEqual(report_arguments.stale_after, 120)
        self.assertEqual(chip_arguments.refresh_after, 300)

    def test_default_cli_returns_all_providers_as_json(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temporary_directory}):
                quota.write_snapshot(
                    "claude",
                    [
                        {"minutes": 300, "usedPercent": 40, "estimated": False},
                        {"minutes": 10080, "usedPercent": 20, "estimated": False},
                    ],
                )
                quota.write_snapshot(
                    "codex",
                    [
                        {"minutes": 300, "usedPercent": 60, "estimated": False},
                        {"minutes": 10080, "usedPercent": 30, "estimated": False},
                    ],
                )
                quota.write_snapshot(
                    "cursor",
                    [
                        {"minutes": 300, "usedPercent": 10, "estimated": True},
                        {"minutes": 10080, "usedPercent": 20, "estimated": True},
                        {"minutes": 43200, "usedPercent": 30, "estimated": False},
                    ],
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = quota.main(["--refresh", "never"])

        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["schemaVersion"], 1)
        self.assertEqual([item["provider"] for item in report["providers"]], list(quota.PROVIDERS))
        self.assertEqual(
            report["providers"][0]["windows"],
            [
                {"minutes": 300, "usedPercent": 40, "resetsAt": None, "estimated": False},
                {"minutes": 10080, "usedPercent": 20, "resetsAt": None, "estimated": False},
                {"minutes": 43200, "usedPercent": 26, "resetsAt": None, "estimated": True},
            ],
        )
        self.assertTrue(all(item["status"] == "ok" for item in report["providers"]))
        self.assertTrue(all(item["error"] is None for item in report["providers"]))

    def test_json_report_includes_unavailable_providers(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temporary_directory}):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = quota.main(["--refresh", "never"])

        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(report["providers"]), 3)
        self.assertTrue(all(item["status"] == "unavailable" for item in report["providers"]))
        self.assertTrue(all(item["windows"] == [] for item in report["providers"]))
        self.assertTrue(
            all(item["error"]["code"] == "cache_missing" for item in report["providers"])
        )

    def test_require_all_fails_after_printing_json(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temporary_directory}):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = quota.main(["--refresh", "never", "--require-all"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(json.loads(output.getvalue())["providers"]), 3)

    def test_refresh_failure_returns_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temporary_directory}):
                quota.write_snapshot(
                    "claude",
                    [
                        {"minutes": 300, "usedPercent": 40},
                        {"minutes": 10080, "usedPercent": 20},
                    ],
                )
                with patch.object(
                    quota.report,
                    "refresh_provider",
                    side_effect=RuntimeError("quota request timed out"),
                ):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = quota.main(["--provider", "claude", "--refresh", "always"])

        provider = json.loads(output.getvalue())["providers"][0]
        self.assertEqual(exit_code, 0)
        self.assertEqual(provider["status"], "stale")
        self.assertEqual(provider["error"]["code"], "timeout")
        self.assertEqual([window["minutes"] for window in provider["windows"]], [300, 10080, 43200])

    def test_uses_installed_claude_code_version_in_user_agent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            package_root = Path(temporary_directory)
            binary = package_root / "bin" / "claude"
            binary.parent.mkdir()
            binary.write_text("")
            (package_root / "package.json").write_text(json.dumps({"version": "2.1.280"}))
            quota.claude.user_agent.cache_clear()
            with patch.object(quota.claude.shutil, "which", return_value=str(binary)):
                value = quota.claude.user_agent()
            quota.claude.user_agent.cache_clear()

        self.assertEqual(value, "claude-code/2.1.280")

    def test_refreshes_claude_from_environment_before_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = {
                "CLAUDE_CODE_OAUTH_TOKEN": "environment-token",
                "PI_CODING_AGENT_DIR": str(root / "missing-pi"),
                "CLAUDE_CONFIG_DIR": str(root / "missing-native"),
                "XDG_CACHE_HOME": str(root / "cache"),
            }
            response = {"five_hour": {"utilization": 25}, "seven_day": {"utilization": 9}}
            with (
                patch.dict(os.environ, environment, clear=True),
                patch.object(quota.claude, "fetch_json", return_value=response) as fetch,
                patch.object(quota.claude, "user_agent", return_value="claude-code/2.1.280"),
            ):
                quota.refresh_claude(timeout=2)

        self.assertEqual(fetch.call_args.args[1]["Authorization"], "Bearer environment-token")
        self.assertEqual(fetch.call_args.args[1]["User-Agent"], "claude-code/2.1.280")

    def test_refreshes_claude_from_pi_before_native_auth(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pi_config = root / "pi"
            pi_config.mkdir()
            (pi_config / "auth.json").write_text(
                json.dumps({"anthropic": {"type": "oauth", "access": "pi-claude-token"}})
            )
            environment = {
                "XDG_CACHE_HOME": str(root / "cache"),
                "PI_CODING_AGENT_DIR": str(pi_config),
                "CLAUDE_CONFIG_DIR": str(root / "missing-native"),
            }
            response = {
                "five_hour": {"utilization": 25},
                "seven_day": {"utilization": 9},
            }
            with (
                patch.dict(os.environ, environment),
                patch.object(quota.claude, "fetch_json", return_value=response) as fetch,
            ):
                quota.refresh_claude(timeout=2)
                snapshot = quota.read_snapshot("claude")

        self.assertEqual(quota.format_chip("claude", snapshot), "cl 5h75% 1w91% 30d86%")
        self.assertEqual(fetch.call_args.args[0], quota.CLAUDE_USAGE_URL)
        self.assertEqual(fetch.call_args.args[1]["Authorization"], "Bearer pi-claude-token")

    def test_falls_back_to_native_claude_auth(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pi_config = root / "pi"
            pi_config.mkdir()
            (pi_config / "auth.json").write_text("{}")
            config = root / "claude"
            config.mkdir()
            (config / ".credentials.json").write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "native-claude-token"}})
            )
            environment = {
                "XDG_CACHE_HOME": str(root / "cache"),
                "PI_CODING_AGENT_DIR": str(pi_config),
                "CLAUDE_CONFIG_DIR": str(config),
            }
            response = {"five_hour": {"utilization": 25}}
            with (
                patch.dict(os.environ, environment),
                patch.object(quota.claude, "fetch_json", return_value=response) as fetch,
            ):
                quota.refresh_claude(timeout=2)

        self.assertEqual(fetch.call_args.args[1]["Authorization"], "Bearer native-claude-token")

    def test_refreshes_codex_from_pi_before_native_auth(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pi_config = root / "pi"
            pi_config.mkdir()
            (pi_config / "auth.json").write_text(
                json.dumps(
                    {
                        "openai-codex": {
                            "type": "oauth",
                            "access": "pi-codex-token",
                            "refresh": "unused",
                            "expires": 2000000000000,
                            "accountId": "pi-account",
                        }
                    }
                )
            )
            environment = {
                "XDG_CACHE_HOME": str(root / "cache"),
                "PI_CODING_AGENT_DIR": str(pi_config),
                "CODEX_HOME": str(root / "missing-native"),
            }
            response = {
                "rate_limit": {
                    "primary_window": {"used_percent": 25, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 9, "limit_window_seconds": 604800},
                }
            }
            with (
                patch.dict(os.environ, environment),
                patch.object(quota.codex, "fetch_json", return_value=response) as fetch,
            ):
                quota.refresh_codex(timeout=2)
                snapshot = quota.read_snapshot("codex")

        self.assertEqual(quota.format_chip("codex", snapshot), "cx 5h75% 1w91% 30d86%")
        self.assertEqual(fetch.call_args.args[0], quota.CODEX_USAGE_URL)
        self.assertEqual(fetch.call_args.args[1]["Authorization"], "Bearer pi-codex-token")
        self.assertEqual(fetch.call_args.args[1]["ChatGPT-Account-Id"], "pi-account")

    def test_gets_codex_account_id_from_access_token(self):
        claim = {"https://api.openai.com/auth": {"chatgpt_account_id": "jwt-account"}}
        encoded = base64.urlsafe_b64encode(json.dumps(claim).encode()).decode().rstrip("=")
        token = f"header.{encoded}.signature"
        self.assertEqual(quota.codex_account_id(token, None), "jwt-account")

    def test_falls_back_to_native_codex_auth(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pi_config = root / "pi"
            pi_config.mkdir()
            (pi_config / "auth.json").write_text("{}")
            config = root / "codex"
            config.mkdir()
            (config / "auth.json").write_text(
                json.dumps(
                    {"tokens": {"access_token": "native-token", "account_id": "native-account"}}
                )
            )
            environment = {
                "XDG_CACHE_HOME": str(root / "cache"),
                "PI_CODING_AGENT_DIR": str(pi_config),
                "CODEX_HOME": str(config),
            }
            response = {
                "rate_limit": {
                    "primary_window": {"used_percent": 25, "limit_window_seconds": 18000}
                }
            }
            with (
                patch.dict(os.environ, environment),
                patch.object(quota.codex, "fetch_json", return_value=response) as fetch,
            ):
                quota.refresh_codex(timeout=2)

        self.assertEqual(fetch.call_args.args[1]["Authorization"], "Bearer native-token")

    def test_refreshes_cursor_from_pi_api_key_exchange(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pi_config = root / "pi"
            pi_config.mkdir()
            (pi_config / "auth.json").write_text(
                json.dumps({"cursor": {"type": "api_key", "key": "cursor-api-key"}})
            )
            environment = {
                "XDG_CACHE_HOME": str(root / "cache"),
                "PI_CODING_AGENT_DIR": str(pi_config),
                "CURSOR_CLI_CONFIG": str(root / "missing-native"),
            }
            responses = [
                {"accessToken": "cursor-session-token", "refreshToken": "unused"},
                {"planUsage": {"totalPercentUsed": 7}},
            ]
            with (
                patch.dict(os.environ, environment),
                patch.object(quota.cursor, "fetch_json", side_effect=responses) as fetch,
            ):
                quota.refresh_cursor(timeout=2)
                snapshot = quota.read_snapshot("cursor")

        self.assertEqual(quota.format_chip("cursor", snapshot), "cr 5h93% 1w93% 30d93%")
        self.assertEqual(fetch.call_args_list[0].args[0], quota.CURSOR_EXCHANGE_URL)
        self.assertEqual(fetch.call_args_list[0].args[1]["Authorization"], "Bearer cursor-api-key")
        self.assertEqual(fetch.call_args_list[1].args[0], quota.CURSOR_USAGE_URL)
        self.assertEqual(
            fetch.call_args_list[1].args[1]["Authorization"], "Bearer cursor-session-token"
        )

    def test_reads_cursor_api_key_from_native_sdk(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pi_config = root / "pi"
            pi_config.mkdir()
            (pi_config / "auth.json").write_text("{}")
            sdk_config = root / ".cursor" / "sdk"
            sdk_config.mkdir(parents=True)
            (sdk_config / "auth.json").write_text(json.dumps({"apiKey": "sdk-key"}))
            environment = {
                "HOME": str(root),
                "PI_CODING_AGENT_DIR": str(pi_config),
                "CURSOR_CLI_CONFIG": str(root / "missing-cli"),
            }
            with patch.dict(os.environ, environment):
                self.assertEqual(quota.cursor_credentials(), {"apiKey": "sdk-key"})

    def test_reads_cursor_tokens_from_desktop_database(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pi_config = root / "pi"
            pi_config.mkdir()
            (pi_config / "auth.json").write_text("{}")
            database_path = root / "state.vscdb"
            database = sqlite3.connect(database_path)
            database.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
            database.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                ("cursorAuth/accessToken", "desktop-token"),
            )
            database.commit()
            database.close()
            environment = {
                "HOME": str(root),
                "PI_CODING_AGENT_DIR": str(pi_config),
                "CURSOR_CLI_CONFIG": str(root / "missing-cli"),
                "CURSOR_STATE_DB": str(database_path),
            }
            with patch.dict(os.environ, environment):
                self.assertEqual(
                    quota.cursor_credentials(),
                    {"accessToken": "desktop-token"},
                )

    def test_refreshes_expired_cursor_access_token_without_writing_it(self):
        claim = base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip("=")
        expired = f"header.{claim}.signature"
        credentials = {"accessToken": expired, "refreshToken": "refresh-token"}
        with patch.object(
            quota.cursor, "fetch_json", return_value={"access_token": "fresh-token"}
        ) as fetch:
            token = quota.cursor_access_token(credentials, timeout=2)

        self.assertEqual(token, "fresh-token")
        self.assertEqual(fetch.call_args.args[0], quota.CURSOR_REFRESH_URL)
        body = json.loads(fetch.call_args.kwargs["body"])
        self.assertEqual(body["refresh_token"], "refresh-token")

    def test_falls_back_to_native_cursor_auth(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pi_config = root / "pi"
            pi_config.mkdir()
            (pi_config / "auth.json").write_text("{}")
            config = root / "cursor-auth.json"
            config.write_text(json.dumps({"accessToken": "cursor-token"}))
            environment = {
                "XDG_CACHE_HOME": str(root / "cache"),
                "PI_CODING_AGENT_DIR": str(pi_config),
                "CURSOR_CLI_CONFIG": str(config),
            }
            response = {"planUsage": {"totalPercentUsed": 7}}
            with (
                patch.dict(os.environ, environment),
                patch.object(quota.cursor, "fetch_json", return_value=response) as fetch,
            ):
                quota.refresh_cursor(timeout=2)

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0], quota.CURSOR_USAGE_URL)
        self.assertEqual(fetch.call_args.args[1]["Authorization"], "Bearer cursor-token")

    def test_history_estimate_needs_completed_week(self):
        estimate = quota.history.estimate_monthly_usage
        current = weekly_window(20, NOW + 5 * DAY)

        self.assertIsNone(estimate([], current, NOW))
        self.assertIsNone(
            estimate([weekly_sample(NOW - 32 * DAY, 50, NOW - 31 * DAY)], current, NOW)
        )

    def test_history_estimate_needs_reported_weekly_reset(self):
        estimate = quota.history.estimate_monthly_usage
        samples = [weekly_sample(NOW - 3 * DAY, 70, NOW - 2 * DAY)]

        self.assertIsNone(estimate(samples, weekly_window(20, None), NOW))
        self.assertIsNone(estimate(samples, weekly_window(20, NOW + 5 * DAY, True), NOW))

    def test_history_estimate_ignores_estimated_weekly_samples(self):
        samples = [weekly_sample(NOW - 3 * DAY, 70, NOW - 2 * DAY, estimated=True)]
        current = weekly_window(20, NOW + 5 * DAY)

        self.assertIsNone(quota.history.estimate_monthly_usage(samples, current, NOW))

    def test_history_estimate_ignores_current_week_split_by_reset_jitter(self):
        resets_at = NOW + 5 * DAY + HOUR // 2
        samples = [weekly_sample(NOW - HOUR, 10, resets_at - 60)]
        current = weekly_window(20, resets_at + 60)

        self.assertIsNone(quota.history.estimate_monthly_usage(samples, current, NOW))

    def test_history_estimate_combines_partial_week_with_previous_peak(self):
        samples = [
            weekly_sample(NOW - 5 * DAY, 30, NOW - 2 * DAY),
            weekly_sample(NOW - 3 * DAY, 70, NOW - 2 * DAY),
        ]
        current = weekly_window(20, NOW + 5 * DAY)

        self.assertEqual(quota.history.estimate_monthly_usage(samples, current, NOW), 70)

    def test_history_estimate_of_steady_weekly_usage_is_that_usage(self):
        samples = [
            weekly_sample(end - DAY, 50, end)
            for end in (NOW - 3 * DAY - 12 * HOUR - week * WEEK for week in range(5))
        ]
        current = weekly_window(25, NOW + 3 * DAY + 12 * HOUR)

        self.assertEqual(quota.history.estimate_monthly_usage(samples, current, NOW), 50)

    def test_history_estimate_weights_week_partly_outside_thirty_days(self):
        samples = [
            weekly_sample(NOW - 2 * DAY, 10, NOW - DAY),
            weekly_sample(NOW - 27 * DAY, 80, NOW - 26 * DAY),
        ]
        current = weekly_window(10, NOW + 6 * DAY)

        self.assertEqual(quota.history.estimate_monthly_usage(samples, current, NOW), 38)

    def test_history_estimate_does_not_count_gap_as_unused(self):
        samples = [weekly_sample(NOW - 16 * DAY, 42, NOW - 15 * DAY)]
        current = weekly_window(6, NOW + 6 * DAY)

        self.assertEqual(quota.history.estimate_monthly_usage(samples, current, NOW), 42)

    def test_history_estimate_groups_jittering_resets_into_one_week(self):
        end = NOW - 2 * DAY
        samples = [
            weekly_sample(NOW - 4 * DAY, 40, end - 7),
            weekly_sample(NOW - 3 * DAY, 60, end + 5),
        ]
        current = weekly_window(0, NOW + 5 * DAY)

        self.assertEqual(quota.history.estimate_monthly_usage(samples, current, NOW), 47)

    def test_history_estimate_counts_completed_week_split_by_reset_jitter_once(self):
        end = NOW - 2 * DAY + HOUR // 2
        samples = [
            weekly_sample(NOW - 4 * DAY, 10, end - 5),
            weekly_sample(NOW - 3 * DAY, 60, end + 5),
        ]
        current = weekly_window(0, NOW + 5 * DAY)

        self.assertEqual(quota.history.estimate_monthly_usage(samples, current, NOW), 47)

    def test_record_snapshot_appends_private_history_samples(self):
        windows = quota.complete_windows(
            [
                {"minutes": 300, "usedPercent": 40},
                {"minutes": 10080, "usedPercent": 20},
            ]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temporary_directory}):
                quota.history.record_snapshot("claude", windows)
                quota.history.record_snapshot("claude", windows)
                path = quota.history.history_path("claude")
                mode = stat.S_IMODE(path.stat().st_mode)
                samples = [json.loads(line) for line in path.read_text().splitlines()]

        self.assertEqual(mode, 0o600)
        self.assertEqual(len(samples), 2)
        self.assertTrue(all(isinstance(sample["capturedAt"], int) for sample in samples))
        self.assertTrue(all(sample["windows"] == windows for sample in samples))

    def test_record_snapshot_prunes_old_and_malformed_history(self):
        now = int(time.time())
        kept = weekly_sample(now - 36 * DAY, 50, now - 35 * DAY)
        lines = [
            json.dumps(weekly_sample(now - 38 * DAY, 50, now - 37 * DAY)),
            "not json",
            "[]",
            json.dumps({"capturedAt": "yesterday", "windows": []}),
            json.dumps(kept),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temporary_directory}):
                path = quota.history.history_path("codex")
                path.write_text("\n".join(lines) + "\n")
                quota.history.record_snapshot(
                    "codex", quota.complete_windows([{"minutes": 300, "usedPercent": 10}])
                )
                samples = [json.loads(line) for line in path.read_text().splitlines()]

        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0], kept)
        self.assertGreaterEqual(samples[1]["capturedAt"], now)

    def test_record_snapshot_ignores_symlinked_history(self):
        now = int(time.time())
        windows = quota.complete_windows(
            [
                {"minutes": 300, "usedPercent": 40},
                {"minutes": 10080, "usedPercent": 20, "resetsAt": now + 5 * DAY},
            ]
        )
        sample = weekly_sample(now - 3 * DAY, 70, now - 2 * DAY)
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "outside.jsonl"
            target.write_text(json.dumps(sample) + "\n")
            cache_home = Path(temporary_directory) / "cache"
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache_home)}):
                quota.history.history_path("claude").symlink_to(target)
                quota.history.record_snapshot("claude", windows)
                monthly_window = quota.read_snapshot("claude")["windows"][2]
            target_text = target.read_text()

        self.assertEqual(monthly_window["usedPercent"], 26)
        self.assertEqual(target_text, json.dumps(sample) + "\n")

    def test_read_snapshot_ignores_symlinked_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temporary_directory}):
                quota.write_snapshot("cursor", [{"minutes": 43200, "usedPercent": 7}])
                path = quota.snapshot_path("cursor")
                target = path.with_name("target.json")
                path.rename(target)
                path.symlink_to(target)

                self.assertIsNone(quota.read_snapshot("cursor"))

    def test_read_private_file_requires_current_owner(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "cache.json"
            path.write_text("{}")
            with patch.object(os, "getuid", return_value=os.getuid() + 1):
                foreign_text = quota.storage.read_private_file(path)
            own_text = quota.storage.read_private_file(path)

        self.assertIsNone(foreign_text)
        self.assertEqual(own_text, "{}")

    def test_snapshot_uses_history_estimate_for_thirty_days(self):
        now = int(time.time())
        windows = quota.complete_windows(
            [
                {"minutes": 300, "usedPercent": 40},
                {"minutes": 10080, "usedPercent": 20, "resetsAt": now + 5 * DAY},
            ]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temporary_directory}):
                quota.history.record_snapshot("claude", windows)
                formula_window = quota.read_snapshot("claude")["windows"][2]

                sample = weekly_sample(now - 3 * DAY, 70, now - 2 * DAY)
                quota.history.history_path("claude").write_text(json.dumps(sample) + "\n")
                quota.history.record_snapshot("claude", windows)
                history_window = quota.read_snapshot("claude")["windows"][2]

        self.assertEqual(
            formula_window,
            {"minutes": 43200, "usedPercent": 26, "resetsAt": None, "estimated": True},
        )
        self.assertEqual(
            history_window,
            {"minutes": 43200, "usedPercent": 70, "resetsAt": None, "estimated": True},
        )

    def test_record_snapshot_skips_unusable_history_values(self):
        now = int(time.time())
        windows = quota.complete_windows(
            [
                {"minutes": 300, "usedPercent": 40},
                {"minutes": 10080, "usedPercent": 20, "resetsAt": now + 5 * DAY},
            ]
        )
        resets_at = now - 2 * DAY
        valid = weekly_sample(now - 3 * DAY, 70, resets_at)
        window_line = (
            '{{"capturedAt":{captured_at},"windows":[{{"minutes":10080,'
            '"usedPercent":{used},"resetsAt":{resets_at},"estimated":false}}]}}'
        )
        lines = [
            json.dumps(valid),
            window_line.format(captured_at=now - 3 * DAY, used="NaN", resets_at=resets_at),
            window_line.format(captured_at=now - 3 * DAY, used="Infinity", resets_at=resets_at),
            window_line.format(captured_at=now - 3 * DAY, used="9" * 400, resets_at=resets_at),
            window_line.format(captured_at=now - 3 * DAY, used=90, resets_at="9" * 400),
            window_line.format(captured_at=now - 3 * DAY, used="9" * 5000, resets_at=resets_at),
            "[" * 100000 + "]" * 100000,
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temporary_directory}):
                path = quota.history.history_path("claude")
                path.write_text("\n".join(lines) + "\n")
                quota.history.record_snapshot("claude", windows)
                monthly_window = quota.read_snapshot("claude")["windows"][2]
                history_lines = path.read_text().splitlines()

        self.assertEqual(monthly_window["usedPercent"], 70)
        self.assertEqual(json.loads(history_lines[0]), valid)

    def test_cursor_snapshot_keeps_reported_thirty_days(self):
        now = int(time.time())
        windows = quota.complete_windows(
            [
                {"minutes": 300, "usedPercent": 10, "estimated": True},
                {"minutes": 10080, "usedPercent": 20, "resetsAt": now + DAY, "estimated": True},
                {"minutes": 43200, "usedPercent": 30, "resetsAt": now + DAY},
            ]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": temporary_directory}):
                sample = weekly_sample(now - 3 * DAY, 90, now - 2 * DAY)
                quota.history.history_path("cursor").write_text(json.dumps(sample) + "\n")
                quota.history.record_snapshot("cursor", windows)
                snapshot = quota.read_snapshot("cursor")
                history_lines = quota.history.history_path("cursor").read_text().splitlines()

        self.assertEqual(snapshot["windows"], windows)
        self.assertEqual(len(history_lines), 2)


if __name__ == "__main__":
    unittest.main()
