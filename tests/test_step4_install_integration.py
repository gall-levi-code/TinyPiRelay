from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from spikes import step4_install_integration as spike


def passing_report() -> dict[str, object]:
    unit_common = {
        "no_new_privileges": True,
        "private_tmp": True,
        "protect_system": "strict",
        "protect_home": True,
        "restart": "on-failure",
        "restart_seconds": 5,
        "requires": [],
    }
    media_unit = {
        **unit_common,
        "user": "tinypirelay-media",
        "audio_access": True,
        "argv": ["/usr/bin/python3", "-m", "tinypirelay.media_service"],
    }
    web_unit = {
        **unit_common,
        "user": "tinypirelay-web",
        "audio_access": False,
        "argv": ["/usr/bin/python3", "-m", "tinypirelay.web_service"],
    }

    def path(
        mode: str, owner: str, group: str, kind: str
    ) -> dict[str, object]:
        return {
            "mode": mode,
            "owner": owner,
            "group": group,
            "type": kind,
            "symlink": False,
        }

    return {
        "schema_version": spike.SCHEMA_VERSION,
        "step": 4,
        "evidence_classification": spike.EVIDENCE_CLASSIFICATION,
        "development_host_mutated": False,
        "scenarios": {
            "preflight_supported": {
                "outcome": "ready",
                "plan_printed": True,
                "mutations": [],
            },
            "preflight_unsupported": {
                "outcome": "blocked",
                "error_code": "unsupported_platform",
                "mutations": [],
            },
            "headless_first_run": {
                "outcome": "installed",
                "plan_mutations": [],
                "credential_required": False,
                "setup_pending": True,
                "credential_written": False,
                "audio_configured": False,
                "repeat_mode": "noop",
                "repeat_mutations": [],
            },
            "repository_failure": {
                "outcome": "blocked",
                "error_code": "repository_unavailable",
                "mutations": [],
            },
            "package_failure": {
                "outcome": "failed",
                "failed_stage": "package_install",
                "plan_mutations": [],
                "apt_simulation_safe": True,
                "apt_download_checks": 2,
                "apt_transaction_count": 2,
                "apt_transaction_fingerprinted": True,
                "exact_version_pins": True,
                "real_install_no_remove_or_upgrade": True,
                "services_enabled": False,
                "services_started": False,
                "recoverable": True,
            },
            "missing_element_postinstall": {
                "outcome": "failed",
                "failed_stage": "postinstall_elements",
                "services_enabled": False,
                "services_started": False,
                "recoverable": True,
                "package_install_attempted": True,
                "missing_elements": ["srtsink"],
            },
            "port_conflict": {
                "outcome": "blocked",
                "error_code": "web_port_conflict",
                "mutations": [],
            },
            "existing_config": {
                "outcome": "preserved",
                "before_sha256": "a" * 64,
                "after_sha256": "a" * 64,
                "replacement_attempted": False,
                "competing_config_refused": True,
            },
            "weak_credentials": {
                "outcome": "blocked",
                "error_code": "weak_credentials",
                "credential_written": False,
                "mutations": [],
            },
            "service_start_failure": {
                "outcome": "failed",
                "failed_stage": "service_start",
                "recoverable": True,
                "journal_retained": True,
                "config_preserved": True,
            },
            "fresh_install": {
                "outcome": "installed",
                "version": "1.0.0",
                "config_preserved": True,
                "credential_mode": "0600",
                "services_enabled": True,
                "services_started": True,
                "journal_complete": True,
                "config_owner_applied": True,
                "credential_owner_applied": True,
            },
            "repeated_install": {
                "outcome": "unchanged",
                "version_before": "1.0.0",
                "version_after": "1.0.0",
                "config_before_sha256": "a" * 64,
                "config_after_sha256": "a" * 64,
                "credential_before_sha256": "b" * 64,
                "credential_after_sha256": "b" * 64,
                "mutations": [],
            },
            "interrupted_install": {
                "outcome": "interrupted",
                "journal_retained": True,
                "new_release_selected": False,
            },
            "resume_install": {
                "outcome": "installed",
                "resumed_from_checkpoint": True,
                "new_release_selected": True,
                "config_preserved": True,
            },
            "upgrade": {
                "outcome": "upgraded",
                "version_before": "0.9.0",
                "version_after": "1.0.0",
                "config_backup_created": True,
                "config_preserved": True,
                "rollback_available": True,
                "distribution_upgrade_attempted": False,
            },
            "uninstall_preserve": {
                "outcome": "uninstalled",
                "units_removed": True,
                "release_removed": True,
                "config_preserved": True,
                "credentials_preserved": True,
                "recordings_preserved": True,
                "service_users_preserved": True,
                "purge_requested": False,
            },
            "purge_with_confirmation": {
                "outcome": "purged",
                "wrong_confirmation_read_only": True,
                "configuration_removed": True,
                "recordings_removed": True,
                "service_users_preserved": True,
            },
            "boot_and_late_resources": {
                "evidence_source": "static_unit_contract_plus_runtime_unit_tests",
                "physical_boot_audio_network_execution": "pending",
                "web_order_independent": True,
                "web_unavailable_unit_test_present": True,
                "capture_failure_exit_test_present": True,
                "capture_failure_restart_is_bounded": True,
                "network_online_not_required_for_start": True,
                "network_reconnect_unit_test_present": True,
            },
        },
        "security": {
            "units": {
                "tinypirelay-media.service": media_unit,
                "tinypirelay-web.service": web_unit,
            },
            "files": {
                "/etc/tinypirelay/media": path(
                    "0700", "tinypirelay-media", "tinypirelay-media", "directory"
                ),
                "/etc/tinypirelay/media/config.json": path(
                    "0600", "tinypirelay-media", "tinypirelay-control", "file"
                ),
                "/etc/tinypirelay/web": path(
                    "0700", "tinypirelay-web", "tinypirelay-web", "directory"
                ),
                "/etc/tinypirelay/web/credential.json": path(
                    "0600", "tinypirelay-web", "tinypirelay-web", "file"
                ),
                "/run/tinypirelay": path(
                    "0750", "tinypirelay-media", "tinypirelay-control", "directory"
                ),
                "/run/tinypirelay/control.sock": path(
                    "0660", "tinypirelay-media", "tinypirelay-control", "socket"
                ),
            },
            "owner_events_present": True,
            "secret_values_absent_from_process_args": True,
            "secret_values_absent_from_logs": True,
            "credential_hash_absent_from_diagnostics": True,
        },
    }


class Step4InstallAssessmentTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX atomic symlink lifecycle")
    def test_actual_installer_runs_the_complete_redirected_fixture_lifecycle(self) -> None:
        report = spike.run_isolated_scenarios()
        result = spike.assess_report(
            report, forbidden_values=(spike.FIXTURE_PASSWORD,)
        )

        self.assertEqual(
            "pass",
            result["status"],
            json.dumps(
                {
                    "reasons": result["reasons"],
                    "setup_errors": report["setup_errors"],
                },
                indent=2,
            ),
        )

    def test_complete_isolated_lifecycle_report_passes(self) -> None:
        result = spike.assess_report(
            passing_report(), forbidden_values=("fixture-secret-never-render",)
        )

        self.assertEqual("pass", result["status"])
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual([], result["reasons"])

    def test_every_preflight_and_failure_gate_fails_closed(self) -> None:
        mutations = {
            "supported_preflight_is_read_only": lambda report: report["scenarios"][
                "preflight_supported"
            ]["mutations"].append("write:/etc/tinypirelay"),
            "unsupported_preflight_is_read_only": lambda report: report["scenarios"][
                "preflight_unsupported"
            ]["mutations"].append("apt-install"),
            "headless_first_run_supports_browser_setup": lambda report: report["scenarios"][
                "headless_first_run"
            ].update({"credential_required": True}),
            "repository_failure_blocks_before_mutation": lambda report: report[
                "scenarios"
            ]["repository_failure"]["mutations"].append("useradd"),
            "package_failure_is_recoverable": lambda report: report["scenarios"][
                "package_failure"
            ].update({"services_enabled": True}),
            "missing_element_fails_postinstall_before_services": lambda report: report[
                "scenarios"
            ]["missing_element_postinstall"].update({"services_started": True}),
            "port_conflict_blocks_before_mutation": lambda report: report["scenarios"][
                "port_conflict"
            ]["mutations"].append("write-config"),
            "weak_credentials_are_rejected_without_write": lambda report: report[
                "scenarios"
            ]["weak_credentials"].update({"credential_written": True}),
            "service_failure_is_recoverable": lambda report: report["scenarios"][
                "service_start_failure"
            ].update({"journal_retained": False}),
        }
        for check, mutate in mutations.items():
            with self.subTest(check=check):
                report = passing_report()
                mutate(report)
                result = spike.assess_report(report)
                self.assertEqual("fail", result["status"])
                self.assertFalse(result["checks"][check])

    def test_onboarding_contract_requires_new_evidence_and_explicit_config_refusal(self) -> None:
        report = passing_report()
        report["schema_version"] = 1
        self.assertFalse(spike.assess_report(report)["checks"]["isolated_fixture_only"])
        report = passing_report()
        report["scenarios"]["existing_config"]["competing_config_refused"] = False
        self.assertFalse(spike.assess_report(report)["checks"]["existing_config_is_never_overwritten"])
        report = passing_report()
        report["scenarios"]["headless_first_run"]["setup_pending"] = False
        self.assertFalse(spike.assess_report(report)["checks"]["headless_first_run_supports_browser_setup"])

    def test_lifecycle_rejects_overwrite_nonatomic_upgrade_and_data_loss(self) -> None:
        mutations = {
            "existing_config_is_never_overwritten": lambda report: report["scenarios"][
                "existing_config"
            ].update({"after_sha256": "x" * 64}),
            "fresh_install_completes_staged_lifecycle": lambda report: report[
                "scenarios"
            ]["fresh_install"].update({"credential_mode": "0644"}),
            "repeated_install_is_idempotent": lambda report: report["scenarios"][
                "repeated_install"
            ]["mutations"].append("credential-replaced"),
            "interruption_then_resume_is_atomic": lambda report: report["scenarios"][
                "interrupted_install"
            ].update({"new_release_selected": True}),
            "upgrade_is_versioned_and_preserving": lambda report: report["scenarios"][
                "upgrade"
            ].update({"distribution_upgrade_attempted": True}),
            "default_uninstall_preserves_state": lambda report: report["scenarios"][
                "uninstall_preserve"
            ].update({"recordings_preserved": False}),
            "apt_transaction_preflight_is_read_only_and_pinned": lambda report: report[
                "scenarios"
            ]["package_failure"].update({"apt_simulation_safe": False}),
        }
        for check, mutate in mutations.items():
            with self.subTest(check=check):
                report = passing_report()
                mutate(report)
                result = spike.assess_report(report)
                self.assertEqual("fail", result["status"])
                self.assertFalse(result["checks"][check])

    def test_permissions_units_secret_hygiene_and_boot_recovery_fail_closed(self) -> None:
        mutations = {
            "units_are_least_privilege_and_independent": lambda report: report[
                "security"
            ]["units"]["tinypirelay-web.service"].update(
                {"user": "root", "audio_access": True}
            ),
            "filesystem_permissions_are_restrictive": lambda report: report[
                "security"
            ]["files"]["/etc/tinypirelay/web/credential.json"].update(
                {"mode": "0644"}
            ),
            "secrets_absent_from_args_logs_and_report": lambda report: report[
                "security"
            ].update({"secret_values_absent_from_logs": False}),
            "boot_and_late_resources_static_contract_only": lambda report: report["scenarios"][
                "boot_and_late_resources"
            ].update({"network_online_not_required_for_start": False}),
        }
        for check, mutate in mutations.items():
            with self.subTest(check=check):
                report = passing_report()
                mutate(report)
                result = spike.assess_report(report)
                self.assertEqual("fail", result["status"])
                self.assertFalse(result["checks"][check])

        report = passing_report()
        report["security"]["logs"] = "fixture-secret-never-render"
        result = spike.assess_report(
            report, forbidden_values=("fixture-secret-never-render",)
        )
        self.assertFalse(result["checks"]["secrets_absent_from_args_logs_and_report"])

    def test_cli_assesses_json_without_performing_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            report_path.write_text(json.dumps(passing_report()), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, spike.main((str(report_path),)))

            report_path.write_text("[]", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(2, spike.main((str(report_path),)))

    def test_packaged_units_and_paths_match_the_security_and_boot_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        media = (root / "packaging/systemd/tinypirelay-media.service").read_text(
            encoding="utf-8"
        )
        web = (root / "packaging/systemd/tinypirelay-web.service").read_text(
            encoding="utf-8"
        )
        sysusers = (root / "packaging/sysusers.d/tinypirelay.conf").read_text(
            encoding="utf-8"
        )
        tmpfiles = (root / "packaging/tmpfiles.d/tinypirelay.conf").read_text(
            encoding="utf-8"
        )

        for text, user in (
            (media, "tinypirelay-media"),
            (web, "tinypirelay-web"),
        ):
            self.assertIn(f"User={user}", text)
            self.assertNotIn("User=root", text)
            self.assertIn("NoNewPrivileges=true", text)
            self.assertIn("ProtectHome=true", text)
            self.assertIn("Restart=on-failure", text)
            self.assertIn("RestartSec=5s", text)
            self.assertNotRegex(text.casefold(), r"--(?:password|passphrase)(?:=|\s)")

        self.assertNotIn("network-online.target", media)
        self.assertIn("After=network.target sound.target", media)
        self.assertIn("SupplementaryGroups=audio", media)
        self.assertIn("DevicePolicy=closed", media)
        self.assertNotIn("tinypirelay-media.service", web)
        self.assertIn("PrivateDevices=true", web)
        self.assertNotIn("SupplementaryGroups=audio", web)

        for identity in ("tinypirelay-media", "tinypirelay-web"):
            self.assertIn(identity, sysusers)
            self.assertIn("/usr/sbin/nologin", sysusers)
        for contract in (
            "d /etc/tinypirelay/media                    0700 tinypirelay-media",
            "d /etc/tinypirelay/web                      0700 tinypirelay-web",
            "d /run/tinypirelay                          0750 tinypirelay-media",
            "d /var/lib/tinypirelay/recordings           0750 tinypirelay-media",
        ):
            self.assertIn(contract, tmpfiles)


if __name__ == "__main__":
    unittest.main()
