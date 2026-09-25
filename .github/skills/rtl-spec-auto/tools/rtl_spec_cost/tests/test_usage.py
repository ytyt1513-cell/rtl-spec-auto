"""Synthetic OTel fixtures only; these are not measured Copilot CLI exports."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools.rtl_spec_cost import usage


def span(sid, operation="invoke_agent", parent=None, model="test-model", **measures):
    attrs = {
        "gen_ai.operation.name": operation, "gen_ai.request.model": model,
        "gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 20,
        "gen_ai.usage.cache_read.input_tokens": 30,
        "gen_ai.usage.cache_creation.input_tokens": 10,
        "github.copilot.nano_aiu": 500, "github.copilot.cost": 999,
    }
    attrs.update(measures)
    return dict(name=operation, traceId="a" * 32, spanId=f"{sid:016x}",
                parentSpanId=f"{parent:016x}" if parent else "", attributes=attrs)


class UsageTests(unittest.TestCase):
    def test_known_non_trace_envelopes_do_not_double_count_or_become_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'mixed.jsonl'
            records = [span(1), {'resourceMetrics': []}, {'resourceLogs': []}]
            path.write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')
            result = usage.summarize([('writer', 1, path)])
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['root_usage']['totals']['nano_aiu']['total'], 500)
            path.write_text(json.dumps({'resourceMetrics': []}), encoding='utf-8')
            result = usage.summarize([('writer', 1, path)])
            self.assertEqual(result['status'], 'invalid')
            self.assertIsNone(result['root_usage'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def write(self, name, records):
        path = self.base / name
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return path

    def test_root_only_units_and_separate_chat_model_tokens(self):
        records = [span(1), span(2, "chat", 1, **{"gen_ai.response.model": "actual"}),
                   span(3, parent=1), span(4, "chat", 3, model="other")]
        report = usage.summarize([("writer", 1, self.write("w.jsonl", records))])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["root_usage"]["totals"]["nano_aiu"]["total"], 500)
        self.assertEqual(report["chat_usage"]["totals"]["input_tokens"]["total"], 200)
        self.assertEqual(report["chat_usage"]["totals"]["cache_read_tokens"]["total"], 60)
        self.assertEqual({g["model"] for g in report["chat_usage"]["groups"]}, {"actual", "other"})
        self.assertNotIn("cost", report["root_usage"]["totals"])

    def test_otlp_and_python_context_formats_and_stage_round(self):
        root = span(1)
        root["attributes"] = [{"key": k, "value": {"intValue" if isinstance(v, int) else "stringValue": str(v)}}
                              for k, v in root["attributes"].items()]
        otlp = {"resourceSpans": [{"scopeSpans": [{"spans": [root]}]}]}
        other = span(2)
        other["context"] = dict(trace_id="0x" + other.pop("traceId"), span_id="0x" + other.pop("spanId"))
        other["parent_id"] = other.pop("parentSpanId")
        report = usage.summarize([("writer", 1, self.write("a", [otlp])),
                                  ("reviewer", 2, self.write("b", [other]))])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["root_usage"]["totals"]["nano_aiu"]["total"], 1000)
        self.assertEqual({(g["stage"], g["round"]) for g in report["root_usage"]["groups"]},
                         {("writer", 1), ("reviewer", 2)})

    def test_missing_measures_null_not_zero_or_partial_total(self):
        missing = span(2)
        del missing["attributes"]["github.copilot.nano_aiu"]
        del missing["attributes"]["gen_ai.usage.cache_creation.input_tokens"]
        report = usage.summarize([("writer", 1, self.write("w", [span(1), missing]))])
        self.assertEqual(report["status"], "incomplete")
        units = report["root_usage"]["totals"]["nano_aiu"]
        self.assertIsNone(units["total"])
        self.assertEqual(units["known_sum"], 500)
        self.assertEqual(units["missing_spans"], 1)

    def test_identical_duplicate_dedup_but_conflict_and_response_reuse_invalid(self):
        for records, expected in [([span(1), span(1)], "ok"),
                                  ([span(1), span(1, **{"github.copilot.nano_aiu": 600})], "invalid"),
                                  ([span(1), span(2, "chat", 1, **{"gen_ai.response.id": "r"}),
                                    span(3, "chat", 1, **{"gen_ai.response.id": "r"})], "invalid")]:
            with self.subTest(expected=expected, records=len(records)):
                report = usage.summarize([("writer", 1, self.write("w", records))])
                self.assertEqual(report["status"], expected)
                if expected == "ok":
                    self.assertEqual(report["root_usage"]["totals"]["span_count"], 1)
                else:
                    self.assertIsNone(report["root_usage"])

    def test_invalid_empty_malformed_missing_id_and_negative_measure(self):
        no_id = span(1)
        del no_id["spanId"]
        for records in ([], [{"unknown": []}], [no_id],
                        [span(1, **{"github.copilot.nano_aiu": -1})],
                        [span(1, **{"github.copilot.nano_aiu": float("nan")})],
                        [span(1, parent=2), span(2, parent=1)],
                        [{"resourceSpans": [{"scopeSpans": "bad"}]}]):
            with self.subTest(records=records):
                report = usage.summarize([("writer", 1, self.write("w", records))])
                self.assertEqual(report["status"], "invalid")
                self.assertIsNone(report["root_usage"])
        bad = self.base / "bad"
        bad.write_text("{\n", encoding="utf-8")
        self.assertEqual(usage.summarize([("writer", 1, bad)])["status"], "invalid")
        bad.write_text('{"name":"invoke_agent","name":"chat"}\n', encoding="utf-8")
        self.assertEqual(usage.summarize([("writer", 1, bad)])["status"], "invalid")

    def test_orphans_and_absent_roots_are_diagnosed(self):
        report = usage.summarize([("writer", 1, self.write("w", [span(1, parent=99)]))])
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("missing_parent", [d["code"] for d in report["diagnostics"]])
        report = usage.summarize([("writer", 1, self.write("w", [span(2, "chat", 1)]))])
        self.assertEqual(report["status"], "incomplete")
        self.assertIsNone(report["root_usage"]["totals"]["nano_aiu"]["total"])
        orphan = span(3, "chat")
        orphan["traceId"] = "b" * 32
        report = usage.summarize([("writer", 1, self.write("w", [span(1), orphan]))])
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("no_root_invoke_agent", [d["code"] for d in report["diagnostics"]])

    def test_cli_explicit_output_and_collision_protection(self):
        source = self.write("w", [span(1)])
        output = self.base / "result.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(usage.main(["--input", "writer", "1", str(source), "--output", str(output)]), 0)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "ok")
        original = source.read_bytes()
        for argv in (["--input", "writer", "1", str(source), "--output", str(source)],
                     ["--input", "writer", "1", str(source)]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                usage.main(argv)
            self.assertEqual(raised.exception.code, 2)
        self.assertEqual(source.read_bytes(), original)

    def test_overlapping_input_exports_are_not_counted_twice(self):
        report = usage.summarize([("writer", 1, self.write("w", [span(1)])),
                                  ("reviewer", 1, self.write("r", [span(1)]))])
        self.assertEqual(report["status"], "invalid")
        self.assertIsNone(report["root_usage"])

    def test_response_id_collision_across_traces_and_files_is_invalid(self):
        for split_files in (False, True):
            for namespace in ({}, {"gen_ai.provider.name": "github", "server.address": "api.example"}):
                with self.subTest(split_files=split_files, namespace=namespace):
                    first = [span(1), span(2, "chat", 1, **{
                        "gen_ai.response.id": "same-response", **namespace})]
                    second = [span(3), span(4, "chat", 3, model="another-model", **{
                        "gen_ai.response.id": "same-response", **namespace})]
                    for record in second:
                        record["traceId"] = "b" * 32
                    inputs = [("writer", 1, self.write("first", first + ([] if split_files else second)))]
                    if split_files:
                        inputs.append(("reviewer", 2, self.write("second", second)))
                    report = usage.summarize(inputs)
                    self.assertEqual(report["status"], "invalid")
                    self.assertIsNone(report["chat_usage"])
                    self.assertIsNone(report["root_usage"])
                    self.assertIn("repeated_chat_response", [d["code"] for d in report["diagnostics"]])

    def test_response_id_namespaces_must_be_provably_distinct(self):
        base = {"gen_ai.provider.name": "github", "server.address": "api.example", "server.port": 443}
        cases = [
            ({**base, "server.address": "API.EXAMPLE.", "server.port": "443"}, "invalid"),
            ({**base, "gen_ai.provider.name": "other"}, "ok"),
            ({**base, "server.address": "other.example"}, "ok"),
            ({**base, "server.port": 8443}, "ok"),
            ({**base, "server.port": None}, "invalid"),
            ({"gen_ai.provider.name": "other"}, "invalid"),
            ({"server.address": "other.example"}, "invalid"),
            ({}, "invalid"),
        ]
        for other, expected in cases:
            with self.subTest(other=other):
                first = [span(1), span(2, "chat", 1, **{"gen_ai.response.id": "same", **base})]
                second = [span(3), span(4, "chat", 3, **{"gen_ai.response.id": "same", **other})]
                for record in second:
                    record["traceId"] = "b" * 32
                report = usage.summarize([("writer", 1, self.write("first", first)),
                                          ("reviewer", 1, self.write("second", second))])
                self.assertEqual(report["status"], expected)
                if expected == "ok":
                    self.assertEqual(report["chat_usage"]["totals"]["input_tokens"]["total"], 200)


if __name__ == "__main__":
    unittest.main()
