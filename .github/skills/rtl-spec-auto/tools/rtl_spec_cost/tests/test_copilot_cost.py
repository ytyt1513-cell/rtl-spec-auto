"""Synthetic data only. These tests never invoke Copilot or a network API."""
import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from tools.rtl_spec_cost import copilot_cost as cost
from tools.rtl_spec_cost.tests.test_usage import span


def rates():
    return {'schema_version': 1, 'as_of': '2026-09-24', 'source': 'synthetic test fixture',
            'currency': 'USD', 'usd_per_ai_credit': '0.01',
            'token_basis': 'input_includes_cache_read_and_write',
            'rate_unit': 'USD_per_million_tokens', 'rate_columns': list(cost.FIELDS),
            'models': {'test-model': {'threshold_input_tokens': 1000,
                                     'default': ['2', '.2', '2.5', '10'],
                                     'long': ['4', '.4', '5', '15']}}}


def records(total=1000, read=600, write=200, output=100, sid=1):
    attrs = {'gen_ai.usage.input_tokens': total, 'gen_ai.usage.output_tokens': output,
             'gen_ai.usage.cache_read.input_tokens': read,
             'gen_ai.usage.cache_creation.input_tokens': write,
             'github.copilot.nano_aiu': 2_000_000_000}
    return [span(sid, **attrs), span(sid + 1, 'chat', sid, **attrs,
                                   **{'gen_ai.response.model': 'test-model'})]


class CostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rate_path = self.root / 'rates.json'
        cost.save(self.rate_path, rates())

    def write(self, rows, name='input.jsonl'):
        p = self.root / name
        p.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
        return p

    def calculate(self, rows):
        parsed = cost.usage.summarize([('writer', 1, self.write(rows))])
        return cost.calculate(parsed, rates())

    def test_hand_calculated_oracle_and_root_not_added(self):
        result = self.calculate(records())
        self.assertEqual(result['status'], 'estimated')
        self.assertEqual(result['totals']['estimated_usd'], '0.00202')
        self.assertEqual(result['totals']['estimated_ai_credits'], '0.202')
        self.assertEqual(result['reported_usage']['aiu'], '2')
        self.assertIsNone(result['reported_usage']['usd'])
        self.assertIsNone(result['actual_billed_usd'])
        self.assertEqual(result['requests'][0]['billable_tokens']['input'], 200)

    def test_tier_per_request_total_including_cache_and_boundary(self):
        result = self.calculate(records(800, 600, 0) + records(800, 600, 0, sid=3))
        self.assertEqual([r['tier'] for r in result['requests']], ['default', 'default'])
        self.assertEqual(self.calculate(records())['requests'][0]['tier'], 'default')
        result = self.calculate(records(1001, 900, 0))
        self.assertEqual(result['requests'][0]['tier'], 'long')
        self.assertEqual(result['totals']['estimated_usd'], '0.002264')

    def test_missing_cache_unknown_model_auto_and_not_applicable_write(self):
        for mutation in ('missing', 'unknown', 'auto', 'requested-only', 'impossible-cache'):
            with self.subTest(mutation=mutation):
                rows = records()
                a = rows[1]['attributes']
                if mutation == 'missing':
                    del a['gen_ai.usage.cache_creation.input_tokens']
                elif mutation == 'unknown':
                    a['gen_ai.response.model'] = 'unknown-version'
                elif mutation == 'auto':
                    a['gen_ai.response.model'] = 'auto'
                elif mutation == 'requested-only':
                    del a['gen_ai.response.model']
                else:
                    a['gen_ai.usage.cache_read.input_tokens'] = 1001
                result = self.calculate(rows)
                self.assertIsNone(result['totals']['estimated_usd'])
                self.assertEqual(result['status'], 'incomplete')
        row = cost.usage.summarize([('writer', 1, self.write(records()))])['chat_records'][0]
        card = rates()
        card['models']['test-model']['default'][2] = None
        self.assertIn('cache_write_rate_not_applicable', cost.price_chat(row, card)['issues'])

    def test_known_subtotal_not_misrepresented_as_total(self):
        rows = records() + records(sid=3)
        rows[-1]['attributes']['gen_ai.response.model'] = 'unknown'
        result = self.calculate(rows)
        self.assertIsNone(result['totals']['estimated_usd'])
        self.assertEqual(result['totals']['known_subtotal_usd'], '0.00202')
        self.assertEqual(result['totals']['unpriced_requests'], 1)

    def test_duplicate_conflict_missing_parent_and_mismatched_totals(self):
        rows = records()
        conflict = deepcopy(rows[-1])
        conflict['attributes']['gen_ai.usage.output_tokens'] += 1
        result = self.calculate(rows + [conflict])
        self.assertEqual(result['status'], 'invalid')
        self.assertIsNone(result['totals']['known_subtotal_usd'])
        rows[0]['parentSpanId'] = 'f' * 16
        self.assertIsNone(self.calculate(rows)['totals']['estimated_usd'])
        rows = records()
        rows[0]['attributes']['gen_ai.usage.input_tokens'] += 1
        result = self.calculate(rows)
        self.assertIn('root_chat_token_mismatch', result['issues'])
        self.assertIsNone(result['totals']['estimated_usd'])

    def test_nested_agent_not_counted_as_another_root(self):
        rows = records()
        nested = deepcopy(rows[0])
        nested['spanId'] = '3' * 16
        nested['parentSpanId'] = rows[0]['spanId']
        rows[1]['parentSpanId'] = nested['spanId']
        result = self.calculate([rows[0], nested, rows[1]])
        self.assertEqual(result['reported_usage']['aiu'], '2')
        self.assertEqual(result['totals']['estimated_usd'], '0.00202')

    def test_same_trace_orphan_chat_cannot_be_a_complete_total(self):
        rows = records()
        orphan = records(sid=3)[1]
        orphan['parentSpanId'] = ''
        result = self.calculate(rows + [orphan])
        self.assertEqual(result['status'], 'incomplete')
        self.assertIn('chat_not_connected_to_root', result['issues'])
        self.assertIsNone(result['totals']['estimated_usd'])
        self.assertEqual(result['totals']['known_subtotal_usd'], '0.00404')

    def test_billing_snapshot_units_scope_no_job_allocation(self):
        item = {'product': 'Copilot AI Credits', 'sku': 'AI Credit', 'model': 'test-model',
                'unitType': 'ai-credits', 'grossQuantity': 100, 'grossAmount': 1,
                'discountQuantity': 80, 'discountAmount': .8, 'netQuantity': 20, 'netAmount': .2}
        for owner, product, unit in [('user', 'Copilot AI Credits', 'ai-credits'),
                                     ('organization', 'Copilot', 'credits')]:
            raw = {owner: 'synthetic', 'timePeriod': {'year': 2026, 'month': 9},
                   'usageItems': [{**item, 'product': product, 'unitType': unit},
                                  {'product': 'Actions', 'unitType': 'minutes'}]}
            result = cost.billing_snapshot(raw)
            self.assertEqual(result['totals']['netAmount'], '0.2')
            self.assertEqual(result['ignored_non_copilot_items'], 1)
            self.assertIsNone(result['run_actual_billed_usd'])
            self.assertEqual(result['owner'], {owner: 'synthetic'})
            raw['usageItems'][0]['unitType'] = 'requests'
            with self.assertRaises(ValueError):
                cost.billing_snapshot(raw)

    def test_billing_requires_valid_owner_and_calendar_period(self):
        base = {'user': 'synthetic', 'timePeriod': {'year': 2026}, 'usageItems': []}
        for period in ({'bad': 1}, {'year': True}, {'year': 2026, 'month': 13},
                       {'year': 2026, 'month': 2, 'day': 30}, {'year': 2026, 'day': 1}):
            with self.subTest(period=period), self.assertRaises(ValueError):
                cost.billing_snapshot({**base, 'timePeriod': period})
        with self.assertRaises(ValueError):
            cost.billing_snapshot({**base, 'user': 1})

    def test_invalid_rate_values_fail_closed(self):
        for value in ('NaN', 'Infinity', '-1', True):
            card = rates()
            card['models']['test-model']['default'][0] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                cost.validate_rates(card)

    def test_json_money_precision_and_non_object_rate_card(self):
        p = self.root / 'decimal.json'
        p.write_text('{"amount": 1.0000000000000000001}', encoding='utf-8')
        self.assertEqual(cost.load(p)['amount'], '1.0000000000000000001')
        with self.assertRaises(ValueError):
            cost.validate_rates([])

    def test_report_evidence_duplicate_exports_and_no_overwrite(self):
        p = self.write(records())
        out = self.root / 'report'
        result = cost.report([('writer', 1, p)], self.rate_path, out, 'synthetic only')
        self.assertEqual(result['status'], 'estimated')
        self.assertEqual((out / 'sources/000-telemetry.jsonl').read_bytes(), p.read_bytes())
        self.assertIn('0.202', (out / 'REPORT.md').read_text(encoding='utf-8'))
        with self.assertRaises(FileExistsError):
            cost.report([('writer', 1, p)], self.rate_path, out, 'again')
        second = self.write(records(), 'copy.jsonl')
        result = cost.report([('writer', 1, p), ('reviewer', 1, second)],
                             self.rate_path, self.root / 'duplicates', 'duplicates')
        self.assertEqual(result['status'], 'invalid')

    def test_failed_process_keeps_cost_and_error_code(self):
        child = self.root / 'synthetic_child.py'
        child.write_text('import os, pathlib, sys\npathlib.Path(os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"]).write_bytes(pathlib.Path(sys.argv[1]).read_bytes())\nsys.exit(7)\n', encoding='utf-8')
        p = self.write(records())
        out = self.root / 'run'
        with contextlib.redirect_stdout(io.StringIO()):
            code = cost.main(['run', '--rates', str(self.rate_path), '--stage', 'writer', '--round', '1',
                              '--out', str(out), '--', sys.executable, str(child), str(p)])
        self.assertEqual(code, 7)
        result = cost.load(out / 'cost/report.json')
        self.assertEqual(result['totals']['estimated_usd'], '0.00202')
        self.assertEqual(result['executions'][0]['command_exit_code'], 7)

    def test_zero_usage_and_missing_file_are_distinct(self):
        result = self.calculate(records(0, 0, 0, 0))
        self.assertEqual(cost.decimal(result['totals']['estimated_usd']), 0)
        result = cost.report([('writer', 1, self.root / 'missing.jsonl')],
                             self.rate_path, self.root / 'missing-report', 'missing')
        self.assertEqual(result['status'], 'invalid')
        self.assertIsNone(result['totals']['estimated_usd'])


if __name__ == '__main__':
    unittest.main()
