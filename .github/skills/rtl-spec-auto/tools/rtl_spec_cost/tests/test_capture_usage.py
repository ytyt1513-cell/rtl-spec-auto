import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capture_usage import capture


class CaptureTests(unittest.TestCase):
    def test_synthetic_child_capture_and_refusal_to_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'capture'
            # This child is a local synthetic producer, NOT Copilot.
            code = """import json, os
from pathlib import Path
assert os.environ['COPILOT_OTEL_EXPORTER_TYPE']=='file'
assert os.environ['OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT']=='false'
assert 'OTEL_EXPORTER_OTLP_ENDPOINT' not in os.environ
attrs={'gen_ai.operation.name':'invoke_agent','gen_ai.request.model':'synthetic',
'gen_ai.usage.input_tokens':100,'gen_ai.usage.output_tokens':10,
'gen_ai.usage.cache_read.input_tokens':0,'gen_ai.usage.cache_creation.input_tokens':0,
'github.copilot.nano_aiu':10}
row={'name':'invoke_agent','traceId':'a'*32,'spanId':'1'*16,'attributes':attrs}
Path(os.environ['COPILOT_OTEL_FILE_EXPORTER_PATH']).write_text(json.dumps(row)+'\\n',encoding='utf-8')
"""
            before = dict(os.environ)
            self.assertEqual(capture('writer', 1, folder, [sys.executable, '-c', code]), 0)
            self.assertEqual(dict(os.environ), before)
            report = json.loads((folder / 'usage.json').read_text(encoding='utf-8'))
            self.assertEqual(report['root_usage']['totals']['nano_aiu']['total'], 10)
            with self.assertRaises(FileExistsError):
                capture('writer', 1, folder, [sys.executable, '-c', code])

    def test_process_success_without_telemetry_is_not_measurement_success(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'empty'
            self.assertEqual(capture('reviewer', 2, folder, [sys.executable, '-c', 'pass']), 2)
            result = json.loads((folder / 'usage.json').read_text(encoding='utf-8'))
            self.assertIsNone(result['root_usage'])
            execution = json.loads((folder / 'execution.json').read_text(encoding='utf-8'))
            self.assertEqual(execution['command_exit_code'], 0)
            self.assertIsNone(execution['actual_currency_cost'])


if __name__ == '__main__':
    unittest.main()
