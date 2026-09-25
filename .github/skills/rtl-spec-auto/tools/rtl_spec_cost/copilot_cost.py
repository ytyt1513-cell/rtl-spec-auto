"""Copilot CLI capture and reproducible, offline cost reports (Python stdlib only).

All prices are estimates of the supplied trace, never invoices. CLI-reported
AIU and billing API snapshots are independent evidence, not additional costs.
"""
import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sys

try:
    from . import usage
    from .capture_usage import capture
except ImportError:
    import usage
    from capture_usage import capture

FIELDS = ('input', 'cache_read', 'cache_write', 'output')


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'),
                      object_pairs_hook=usage._object, parse_constant=usage._invalid_constant,
                      parse_float=str)  # retain monetary decimals without binary rounding


def decimal(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('expected a nonnegative decimal')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('invalid decimal') from exc
    if not result.is_finite() or result < 0:
        raise ValueError('expected a finite nonnegative decimal')
    return result


def number(value):
    return None if value is None else format(value, 'f')


def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def validate_rates(card):
    if (not isinstance(card, dict) or card.get('schema_version') != 1 or card.get('currency') != 'USD'
            or card.get('token_basis') != 'input_includes_cache_read_and_write'
            or card.get('rate_unit') != 'USD_per_million_tokens'
            or card.get('rate_columns') != list(FIELDS)):
        raise ValueError('unsupported rate-card schema, currency, token basis or units')
    date.fromisoformat(card['as_of'])
    if not isinstance(card.get('source'), str) or not card['source']:
        raise ValueError('rate-card source is required')
    if decimal(card['usd_per_ai_credit']) <= 0:
        raise ValueError('usd_per_ai_credit must be positive')
    if not isinstance(card.get('models'), dict) or not card['models']:
        raise ValueError('rate-card models must not be empty')
    for model, rate in card['models'].items():
        if not isinstance(rate, dict):
            raise ValueError('model rate must be an object: ' + model)
        threshold = rate.get('threshold_input_tokens')
        if threshold is not None and (type(threshold) is not int or threshold < 1):
            raise ValueError('invalid long-context threshold: ' + model)
        for tier in ('default', 'long') if threshold is not None else ('default',):
            prices = rate.get(tier)
            if not isinstance(prices, list) or len(prices) != 4:
                raise ValueError('four token prices required: ' + model)
            for i, price in enumerate(prices):
                if price is None and i == 2:  # cache creation not applicable
                    continue
                decimal(price)
    return card


def price_chat(row, card):
    result = {**row, 'estimated_usd': None, 'estimated_ai_credits': None,
              'tier': None, 'issues': []}
    counts = [row[k] for k in usage.TOKEN_KEYS]
    if any(v is None for v in counts):
        result['issues'].append('missing_token_measure')
        return result
    total, output, read, write = counts
    ordinary = total - read - write
    if ordinary < 0:
        result['issues'].append('cache_exceeds_total_input')
        return result
    model = row['model']
    if row['model_basis'] != 'response':
        result['issues'].append('resolved_model_missing')
    if model not in card['models']:
        result['issues'].append('model_not_in_rate_card')
    if result['issues']:
        return result
    rate = card['models'][model]
    threshold = rate.get('threshold_input_tokens')
    tier = 'long' if threshold is not None and total > threshold else 'default'
    prices = rate[tier]
    if prices[2] is None and write:
        result['issues'].append('cache_write_rate_not_applicable')
        return result
    amounts = dict(zip(FIELDS, (ordinary, read, write, output)))
    components = {key: Decimal(amounts[key]) * (decimal(price) if price is not None else Decimal(0))
                  / Decimal(1_000_000) for key, price in zip(FIELDS, prices)}
    usd = sum(components.values(), Decimal(0))
    result.update(tier=tier, billable_tokens=amounts,
                  rates_usd_per_million=dict(zip(FIELDS, prices)),
                  components_usd={k: number(v) for k, v in components.items()},
                  estimated_usd=number(usd),
                  estimated_ai_credits=number(usd / decimal(card['usd_per_ai_credit'])))
    return result


def sum_cost(rows, complete):
    known = [decimal(r['estimated_usd']) for r in rows if r['estimated_usd'] is not None]
    subtotal = sum(known, Decimal(0)) if known else None
    return {'priced_requests': len(known), 'unpriced_requests': len(rows) - len(known),
            'known_subtotal_usd': number(subtotal),
            'estimated_usd': number(subtotal) if complete and rows and len(known) == len(rows) else None}


def billing_snapshot(raw):
    """One account/period snapshot, no implied allocation to these CLI runs."""
    if not isinstance(raw, dict) or not isinstance(raw.get('usageItems'), list):
        raise ValueError('expected a GitHub AI-credit billing usage response')
    if not isinstance(raw.get('timePeriod'), dict) or not raw['timePeriod']:
        raise ValueError('billing timePeriod is required')
    owners = {k: raw[k] for k in ('user', 'organization', 'enterprise') if raw.get(k)}
    if len(owners) != 1 or not all(isinstance(v, str) and v.strip() for v in owners.values()):
        raise ValueError('billing snapshot requires exactly one account owner')
    period = raw['timePeriod']
    if set(period) - {'year', 'month', 'day'} or type(period.get('year')) is not int:
        raise ValueError('billing timePeriod requires an integer year and optional month/day')
    if 'day' in period and 'month' not in period:
        raise ValueError('billing day requires a month')
    if any(type(v) is not int for v in period.values()):
        raise ValueError('billing timePeriod values must be integers')
    date(period['year'], period.get('month', 1), period.get('day', 1))
    keys = ('grossQuantity', 'grossAmount', 'discountQuantity', 'discountAmount', 'netQuantity', 'netAmount')
    selected, ignored = [], 0
    for item in raw['usageItems']:
        if not isinstance(item, dict):
            raise ValueError('invalid billing usage item')
        if item.get('product') not in ('Copilot', 'Copilot AI Credits'):
            ignored += 1
            continue
        if item.get('unitType') not in ('credits', 'ai-credits'):
            raise ValueError('Copilot billing item is not in AI credits')
        values = {k: decimal(item.get(k)) for k in keys}
        # Billing amounts can be rounded independently; retain supplied values.
        selected.append({**item, **{k: number(v) for k, v in values.items()}})
    totals = {k: number(sum((decimal(i[k]) for i in selected), Decimal(0)))
              if selected else None for k in keys}
    return {'scope': 'account_period_not_this_run', 'owner': owners, 'time_period': raw['timePeriod'],
            'status': 'reported' if selected else 'no_copilot_items',
            'items': selected, 'ignored_non_copilot_items': ignored, 'totals': totals,
            'currency': 'USD', 'run_actual_billed_usd': None,
            'note': 'GitHub-reported snapshot, not a settled invoice. Do not add it to trace estimates.'}


def calculate(parsed, card):
    rows = [price_chat(r, card) for r in parsed['chat_records']]
    issues = []
    if parsed['status'] != 'ok':
        issues.append('usage_' + parsed['status'])
    if not rows:
        issues.append('no_chat_requests')
    if any(r['issues'] for r in rows):
        issues.append('unpriced_requests')
    root_keys = {(r['source'], r['trace_id'], r['span_id']) for r in parsed['root_records']}
    if any((r['source'], r['trace_id'], r['root_span_id']) not in root_keys for r in rows):
        issues.append('chat_not_connected_to_root')
    # Check supplied root totals against descendant chats, per invocation.
    # Matching does not prove that entire invocations were not lost in export.
    for root in parsed['root_records']:
        children = [r for r in rows if (r['source'], r['trace_id'], r['root_span_id']) ==
                    (root['source'], root['trace_id'], root['span_id'])]
        for key in usage.TOKEN_KEYS:
            if root[key] is None or not children or any(c[key] is None for c in children):
                issues.append('root_chat_comparison_unavailable')
                continue
            if root[key] != sum(c[key] for c in children):
                issues.append('root_chat_token_mismatch')
    issues = sorted(set(issues))
    total = sum_cost(rows, not issues)
    factor = decimal(card['usd_per_ai_credit'])
    total['estimated_ai_credits'] = number(decimal(total['estimated_usd']) / factor) if total['estimated_usd'] is not None else None
    total['known_subtotal_ai_credits'] = number(decimal(total['known_subtotal_usd']) / factor) if total['known_subtotal_usd'] is not None else None
    groups = defaultdict(list)
    for row in rows:
        groups[(row['stage'], row['round'], row['model'])].append(row)
    stages = []
    for (stage, round_number, model), items in sorted(groups.items(), key=lambda kv: str(kv[0])):
        subtotal = sum_cost(items, not issues)
        subtotal['estimated_ai_credits'] = number(decimal(subtotal['estimated_usd']) / factor) if subtotal['estimated_usd'] is not None else None
        stages.append(dict(stage=stage, round=round_number, model=model, **subtotal))
    roots = parsed['root_usage']
    nano = roots['totals']['nano_aiu']['total'] if roots and parsed['status'] == 'ok' else None
    return {'status': 'invalid' if parsed['status'] == 'invalid' else 'incomplete' if issues else 'estimated',
            'export_coverage': 'unverified', 'issues': issues, 'totals': total,
            'stages': stages, 'requests': rows,
            'reported_usage': {'nano_aiu': nano, 'aiu': number(Decimal(nano) / Decimal(1_000_000_000)) if nano is not None else None,
                               'ai_credits': None, 'usd': None,
                               'conversion_status': 'AIU_to_AI_credit_not_billing_reconciled'},
            'actual_billed_usd': None}


def markdown(report):
    def cell(value):
        return '未確定' if value is None else str(value).replace('|', '\\|').replace('\n', ' ')
    lines = ['# GitHub Copilot コスト計測', '', f"対象: {cell(report['label'])}", '',
             f"計測・計算状態: **{report['status']}** / エクスポート全体の完全性: 未検証", '',
             f"料金表: {report['rates']['as_of']} / [出典]({report['rates']['source']})", '',
             '| 工程 | 回 | 解決済みモデル | 計算済/未計算リクエスト | 推計 USD | 推計 AI credits |',
             '|---|---:|---|---:|---:|---:|']
    for g in report['stages']:
        lines.append(f"| {cell(g['stage'])} | {g['round']} | {cell(g['model'])} | {g['priced_requests']}/{g['unpriced_requests']} | {cell(g['estimated_usd'])} | {cell(g['estimated_ai_credits'])} |")
    t = report['totals']
    lines += ['', f"推計合計: **{cell(t['estimated_usd'])} USD / {cell(t['estimated_ai_credits'])} AI credits**",
              f"計算できた部分の小計: {cell(t['known_subtotal_usd'])} USD（欠測時は総額として使用不可）", '',
              f"CLI報告値: {cell(report['reported_usage']['nano_aiu'])} nano AIU / {cell(report['reported_usage']['aiu'])} AIU",
              'AIU→AI creditの請求側照合は未実施のため、この報告値から通貨へ換算しない。', '',
              '**推計は実請求額ではない。** 含有枠・契約割引・税・固定月額・為替・Auto等の個別条件は未適用。',
              '親の集計と子の使用量は加算しない。失敗工程も発生済み使用量を残す。', '',
              '根拠と欠測: [report.json](report.json) / [usage.json](usage.json) / [入力ハッシュ](sources.json)', '']
    if report['issues']:
        lines += ['計算上の未確認: ' + ', '.join(report['issues']), '']
    if report['executions']:
        lines += ['| 保存した実行 | 終了コード | 使用量取得状態 |', '|---|---:|---|']
        for e in report['executions']:
            lines.append(f"| {cell(e['capture'])} | {cell(e['command_exit_code'])} | {cell(e['usage_status'])} |")
        lines.append('')
    billing = report['billing']
    if billing:
        lines += ['## GitHub課金側の期間集計', '',
                  f"所有者: {cell(billing['owner'])} / 期間: {cell(billing['time_period'])}", '',
                  f"総使用: {cell(billing['totals']['grossQuantity'])} AI credits / 総額: {cell(billing['totals']['grossAmount'])} USD",
                  f"割引額: {cell(billing['totals']['discountAmount'])} USD / 正味額: {cell(billing['totals']['netAmount'])} USD", '',
                  'アカウント・期間全体のAPI報告値。この実行への配賦、推計との加算、確定請求書との同一視は行わない。', '']
    return '\n'.join(lines)


def report(inputs, rates_path, out, label, captures=(), billing_path=None):
    """Snapshot immutable evidence, then calculate from precisely those bytes."""
    if not inputs:
        raise ValueError('at least one telemetry input is required')
    paths = [Path(p).resolve() for _, _, p in inputs]
    if len(set(paths)) != len(paths):
        raise ValueError('same telemetry file supplied twice')
    for stage, round_number, _ in inputs:
        if stage not in ('writer', 'reviewer') or type(round_number) is not int or round_number < 1:
            raise ValueError('stage must be writer/reviewer and round a positive integer')
    card = validate_rates(load(rates_path))
    billing = billing_snapshot(load(billing_path)) if billing_path else None
    directory = Path(out).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    (directory / 'sources').mkdir()
    evidence = []

    def snapshot(source, name):
        source = Path(source).resolve()
        b = source.read_bytes()
        target = directory / 'sources' / name
        target.write_bytes(b)
        evidence.append({'original_path': str(source), 'path': 'sources/' + name,
                         'sha256': hashlib.sha256(b).hexdigest(), 'bytes': len(b)})
        return target

    card = validate_rates(load(snapshot(rates_path, 'rates.json')))
    frozen_inputs = []
    for i, (stage, round_number, p) in enumerate(inputs):
        # Missing capture output stays missing rather than becoming zero usage.
        target = snapshot(p, f'{i:03d}-telemetry.jsonl') if Path(p).is_file() else Path(p).resolve()
        frozen_inputs.append((stage, round_number, target))
    executions = []
    for i, folder in enumerate(captures):
        target = snapshot(Path(folder) / 'execution.json', f'execution-{i:03d}.json')
        e = load(target)
        executions.append({'capture': str(folder), 'command_exit_code': e.get('command_exit_code'),
                           'usage_status': e.get('usage_status')})
    if billing_path:
        billing = billing_snapshot(load(snapshot(billing_path, 'billing.json')))
    parsed = usage.summarize(frozen_inputs)
    result = calculate(parsed, card)
    result.update(schema_version=1, label=label, generated_at=datetime.now(timezone.utc).isoformat(),
                  rates=card, executions=executions, billing=billing)
    save(directory / 'usage.json', parsed)
    save(directory / 'sources.json', evidence)
    save(directory / 'report.json', result)
    (directory / 'REPORT.md').write_text(markdown(result), encoding='utf-8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    for name in ('report', 'run'):
        p = commands.add_parser(name)
        p.add_argument('--rates', type=Path, required=True, help='explicit dated rate card; never silently fetch prices')
        p.add_argument('--out', type=Path, required=True, help='new output directory, never overwrite evidence')
        p.add_argument('--label', default='Copilot CLI')
        p.add_argument('--billing', type=Path, help='one optional saved AI-credit billing API snapshot')
        if name == 'report':
            p.add_argument('--input', nargs=3, action='append', default=[], metavar=('STAGE', 'ROUND', 'JSONL'))
            p.add_argument('--capture', type=Path, action='append', default=[], help='capture directory containing execution.json and telemetry.jsonl')
        else:
            p.add_argument('--stage', choices=('writer', 'reviewer'), required=True)
            p.add_argument('--round', type=int, required=True)
            p.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.action == 'run':
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
            if not command or args.round < 1:
                raise ValueError('command after -- and positive round required')
            validate_rates(load(args.rates))  # fail before launching a chargeable CLI
            if args.billing:
                billing_snapshot(load(args.billing))
            # Capture owns creation of this new directory. Report has its own new child.
            capture(args.stage, args.round, args.out, command)
            result = report([(args.stage, args.round, args.out / 'telemetry.jsonl')],
                            args.rates, args.out / 'cost', args.label, [args.out], args.billing)
            exit_code = load(args.out / 'execution.json')['command_exit_code']
            if exit_code:
                print(f"Command failed ({exit_code}); observed costs saved: {args.out / 'cost' / 'REPORT.md'}")
                return exit_code if exit_code > 0 else 1
        else:
            inputs = [(s, int(r), Path(p)) for s, r, p in args.input]
            for folder in args.capture:
                execution = load(folder / 'execution.json')
                inputs.append((execution['stage'], execution['round'], folder / 'telemetry.jsonl'))
            result = report(inputs, args.rates, args.out, args.label, args.capture, args.billing)
        print(f"{result['status']}: {args.out}")
        return {'estimated': 0, 'incomplete': 1, 'invalid': 2}[result['status']]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
