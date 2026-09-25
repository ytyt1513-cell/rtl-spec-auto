"""Run one CLI stage with local OTel capture; preserve raw data and missing usage."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone
try:
    from .usage import summarize
except ImportError:
    from usage import summarize


def capture(stage, round_number, directory, command):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    trace = directory / 'telemetry.jsonl'
    env = dict(os.environ)
    # This helper requests local telemetry only, never a remote collector.
    for key in list(env):
        if key.startswith('OTEL_EXPORTER_OTLP_'):
            del env[key]
    env.update(COPILOT_OTEL_ENABLED='true', COPILOT_OTEL_EXPORTER_TYPE='file',
               COPILOT_OTEL_FILE_EXPORTER_PATH=str(trace),
               OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT='false')
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    error = None
    with (directory / 'command-output.txt').open('w', encoding='utf-8') as output:
        try:
            completed = subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT,
                                       shell=False, check=False)
            code = completed.returncode
        except OSError as exc:
            error, code = str(exc), 127
    report = summarize([(stage, round_number, trace)])
    (directory / 'usage.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    execution = {'stage': stage, 'round': round_number, 'elapsed_seconds': time.monotonic() - started,
                 'started_at': started_at, 'finished_at': datetime.now(timezone.utc).isoformat(),
                 'command_exit_code': code, 'launch_error': error, 'usage_status': report['status'],
                 'actual_currency_cost': None, 'content_capture_requested': False,
                 'note': 'Successful process exit does not establish complete usage. See raw telemetry and usage diagnostics.'}
    (directory / 'execution.json').write_text(json.dumps(execution, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return code if code else {'ok': 0, 'incomplete': 1, 'invalid': 2}[report['status']]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True, choices=('writer', 'reviewer'))
    parser.add_argument('--round', required=True, type=int)
    parser.add_argument('--out', required=True, type=Path, help='new directory; never overwrite a prior capture')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.round < 1:
        parser.error('positive round and command after -- are required')
    try:
        raise SystemExit(capture(args.stage, args.round, args.out, command))
    except OSError as exc:
        parser.error(str(exc))
