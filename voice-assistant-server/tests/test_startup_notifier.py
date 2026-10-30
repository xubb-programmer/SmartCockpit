import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy/startup-notifier.sh"
BASH = os.environ.get("SMARTCOCKPIT_TEST_BASH") or (shutil.which("bash") if os.name == "posix" else None)


@unittest.skipUnless(BASH, "requires Bash")
class StartupNotifierTests(unittest.TestCase):
    def run_flow(self, collector, mode="hybrid", entry="do_autostart"):
        harness = r'''
source "$1"
WAIT_SECONDS=0
RECOVERY_SECONDS=0
POLL_SECONDS=0
VOICE_ACCESS_MODE="$2"
configure_preferred_output() { :; }
start_audio_route_monitor() { :; }
notify_desktop() { :; }
log_info() { :; }
log_warn() { printf 'LOG %s\n' "$*"; }
write_status() { printf 'STATE %s\n' "$1"; }
play_audio_file() { printf 'AUDIO %s\n' "$1"; }
open_browser() { printf 'BROWSER\n'; }
systemctl() { return 1; }
sleep() { :; }
calls=0
''' + collector + "\n" + entry + "\n"
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, XDG_RUNTIME_DIR=Path(directory).as_posix())
            result = subprocess.run([BASH, "-c", harness, "test", SCRIPT.as_posix(), mode],
                                    capture_output=True, text=True, timeout=10, encoding="utf-8", env=env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result.stdout

    def test_normal_speaks_once(self):
        output = self.run_flow("collect_core_missing() { CORE_MISSING=(); }")
        self.assertEqual([line for line in output.splitlines() if line.startswith("AUDIO")], ["AUDIO startup-ok.wav"])

    def test_multiple_failures_speak_one_anomaly_then_one_recovery(self):
        collector = r'''
collect_core_missing() {
    calls=$((calls + 1))
    if (( calls <= 2 )); then
        CORE_MISSING=(rkllm voice web)
        FAILURE_REASONS[rkllm]=model
        FAILURE_REASONS[voice]=voice
        FAILURE_REASONS[web]=web
    else CORE_MISSING=(); fi
}
'''
        output = self.run_flow(collector)
        self.assertEqual([line for line in output.splitlines() if line.startswith("AUDIO")],
                         ["AUDIO startup-error.wav", "AUDIO startup-recovered.wav"])
        self.assertEqual(output.count("BROWSER\n"), 1)
        self.assertIn("model", output)

    def test_vehicle_only_failure_has_no_voice_anomaly(self):
        collector = r'''
vehicle_check_enabled() { return 0; }
python3() { printf 'vehicle_control\t9000 unavailable\n'; }
start_vehicle_recovery_monitor() { printf 'VEHICLE_MONITOR\n'; }
'''
        output = self.run_flow(collector)
        self.assertEqual([line for line in output.splitlines() if line.startswith("AUDIO")], ["AUDIO startup-ok.wav"])
        self.assertIn("VEHICLE_MONITOR", output)

    def test_vehicle_monitor_has_separate_anomaly_and_recovery(self):
        collector = r'''
vehicle_check_enabled() { return 0; }
flock() { return 0; }
checks=0
vehicle_port_ready() { checks=$((checks + 1)); (( checks >= 3 )); }
'''
        output = self.run_flow(collector, entry='start_vehicle_recovery_monitor; wait; cat "$RUNTIME_DIR/voice-assistant-vehicle-status"')
        self.assertEqual([line for line in output.splitlines() if line.startswith("AUDIO")],
                         ["AUDIO vehicle-control-unavailable.wav", "AUDIO vehicle-control-recovered.wav"])
        self.assertIn("state=recovered", output)

    def test_vehicle_failure_does_not_block_voice_recovery(self):
        collector = r'''
collect_core_missing() {
    calls=$((calls + 1))
    VEHICLE_DIAGNOSTIC="9000 unavailable"
    if (( calls <= 2 )); then
        CORE_MISSING=(rkllm)
        FAILURE_REASONS[rkllm]=offline
    else CORE_MISSING=(); fi
    printf 'CHECK %s\n' "$calls"
}
'''
        output = self.run_flow(collector)
        self.assertIn("CHECK 4\n", output)
        self.assertEqual(output.count("AUDIO startup-error.wav"), 1)
        self.assertEqual(output.count("AUDIO startup-recovered.wav"), 1)
        self.assertEqual(output.count("BROWSER\n"), 1)

    def test_wakeup_mode_never_opens_browser(self):
        output = self.run_flow("collect_core_missing() { CORE_MISSING=(); }", "wakeup")
        self.assertNotIn("BROWSER", output)

    def test_only_three_voice_and_two_vehicle_prompts_can_be_announced(self):
        source = SCRIPT.read_text(encoding="utf-8")
        import re
        self.assertEqual(set(re.findall(r'play_audio_file "([^"]+\.wav)"', source)),
                         {"startup-ok.wav", "startup-error.wav", "startup-recovered.wav",
                          "vehicle-control-unavailable.wav", "vehicle-control-recovered.wav"})
