"""Offline Copilot CLI OTel JSONL accounting (no network or third-party packages).

Run: python tools/rtl_spec_cost/usage.py --input writer 1 writer.jsonl \
    --input reviewer 1 reviewer.jsonl --output usage.json

Supported lines: OTLP resourceSpans/scopeSpans/spans envelopes, or individual
span dictionaries (camelCase IDs, snake_case IDs, or Python OTel context IDs).
Attributes may be a dictionary or OTLP key/value entries. Recognized OTLP
resourceMetrics/resourceLogs envelopes are counted as ignored signals; traces
remain mandatory. Other export shapes are rejected, never treated as zero.

One input file is one export for parent lookup. Supply the complete export,
after the CLI exits; split/unfinished exports cannot establish complete usage.
An invoke_agent is a root when its parent is absent from that export. Nonzero
missing parents are diagnosed because that root may really be a subagent.
Identical trace/span IDs are deduplicated; conflicting snapshots are rejected,
never guessed to be cumulative or incremental streaming records. Distinct
chat spans sharing a response ID across any input/trace are rejected unless
their provider/endpoint namespaces are demonstrably different. Missing
namespace information does not clear a duplicate suspicion. Repeated calls
without stable response IDs and entirely missing spans cannot be detected reliably.

Root units and root aggregate tokens are separate from per-model chat tokens.
Root model names describe the root request, not allocation across child models.
Input tokens include cache use; the four token fields must not be added together.
github.copilot.cost is a model multiplier, NEVER currency, and is not summed.
Missing measures yield null totals and explicit known_sum/coverage. Export
coverage is always unverified: success validates the supplied records only.

Reference (checked 2026-09-22):
https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#opentelemetry-monitoring
"""

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys


TOKEN_KEYS = {
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "cache_read_tokens": "gen_ai.usage.cache_read.input_tokens",
    "cache_write_tokens": "gen_ai.usage.cache_creation.input_tokens",
}
ROOT_KEYS = {"nano_aiu": "github.copilot.nano_aiu", **TOKEN_KEYS}


def _value(value):
    if not isinstance(value, dict):
        return value
    if len(value) != 1:
        raise ValueError("invalid OTLP attribute value")
    kind, item = next(iter(value.items()))
    if kind in ("stringValue", "intValue", "doubleValue", "boolValue", "bytesValue"):
        return item
    if kind == "arrayValue" and isinstance(item, dict):
        return [_value(v) for v in item.get("values", [])]
    if kind == "kvlistValue" and isinstance(item, dict):
        return _attributes(item.get("values", []))
    raise ValueError("unsupported OTLP attribute value")


def _attributes(raw):
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, list):
        raise ValueError("attributes must be a dictionary or OTLP list")
    result = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or "value" not in item:
            raise ValueError("malformed OTLP attribute entry")
        if item["key"] in result:
            raise ValueError("duplicate attribute key")
        result[item["key"]] = _value(item["value"])
    return result


def _id(value, length, optional=False):
    if optional and value in (None, "", "0" * length, "0x" + "0" * length):
        return None
    if not isinstance(value, str):
        raise ValueError("missing or non-string trace/span ID")
    value = value.lower().removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{%d}" % length, value) or not int(value, 16):
        raise ValueError("invalid trace/span ID")
    return value


def _span(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        raise ValueError("expected a span dictionary with a name")
    context = raw.get("context", {})
    if not isinstance(context, dict):
        raise ValueError("span context must be a dictionary")
    trace = raw.get("traceId", raw.get("trace_id", context.get("trace_id")))
    sid = raw.get("spanId", raw.get("span_id", context.get("span_id")))
    parent = raw.get("parentSpanId", raw.get("parent_span_id", raw.get("parent_id")))
    attrs = _attributes(raw.get("attributes", {}))
    operation = attrs.get("gen_ai.operation.name", raw["name"].split(" ")[0])
    if not isinstance(operation, str):
        raise ValueError("operation name must be a string")
    return {
        "trace": _id(trace, 32), "id": _id(sid, 16),
        "parent": _id(parent, 16, optional=True), "operation": operation,
        "attributes": attrs, "raw": raw,
    }


def _spans(record):
    if not isinstance(record, dict):
        raise ValueError("JSONL record must be an object")
    if "resourceSpans" not in record:
        return [_span(record)]
    resources = record["resourceSpans"]
    if not isinstance(resources, list) or not resources:
        raise ValueError("resourceSpans must be a nonempty list")
    result = []
    for resource in resources:
        scopes = resource.get("scopeSpans") if isinstance(resource, dict) else None
        if not isinstance(scopes, list) or not scopes:
            raise ValueError("expected nonempty scopeSpans")
        for scope in scopes:
            spans = scope.get("spans") if isinstance(scope, dict) else None
            if not isinstance(spans, list) or not spans:
                raise ValueError("expected nonempty spans")
            result.extend(_span(span) for span in spans)
    return result


def _count(value):
    # OTel intValue is a decimal string. Do not silently round fractional counts.
    if isinstance(value, bool):
        raise ValueError("boolean usage is invalid")
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    if isinstance(value, int) and value >= 0:
        return value
    if (isinstance(value, float) and math.isfinite(value) and 0 <= value < 2 ** 53
            and value.is_integer()):
        return int(value)
    raise ValueError("usage must be a nonnegative integer")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("non-finite JSON number")


def _response_namespace(attrs):
    """Only service identity scopes response IDs; model/trace/stage never do."""
    provider = attrs.get("gen_ai.provider.name")
    address = attrs.get("server.address")
    provider = provider.strip().lower() if isinstance(provider, str) else None
    address = address.strip().lower().rstrip(".") if isinstance(address, str) else None
    # server.address is a hostname/IP, not a URL. Unusable values are unknown.
    if address and ("/" in address or any(c.isspace() for c in address)):
        address = None
    port = attrs.get("server.port")
    try:
        port = _count(port)
        if not 1 <= port <= 65535:
            port = None
    except ValueError:
        port = None
    return provider or None, address or None, port


def _distinct_namespaces(first, second):
    # Missing provider/host cannot prove disjoint service namespaces. An absent
    # port also cannot distinguish a default-port request from an explicit one.
    if not all(first[:2]) or not all(second[:2]):
        return False
    return (first[:2] != second[:2]
            or (first[2] is not None and second[2] is not None and first[2] != second[2]))


def _aggregate(rows, keys):
    result = {"span_count": len(rows)}
    for field in keys:
        observed = [row[field] for row in rows if row[field] is not None]
        result[field] = {
            "total": sum(observed) if rows and len(observed) == len(rows) else None,
            "known_sum": sum(observed) if observed else None,
            "observed_spans": len(observed), "missing_spans": len(rows) - len(observed),
        }
    return result


def _groups(rows, keys):
    groups = {}
    for row in rows:
        key = row["stage"], row["round"], row["model"]
        groups.setdefault(key, []).append(row)
    return [dict(stage=key[0], round=key[1], model=key[2], **_aggregate(items, keys))
            for key, items in sorted(groups.items(), key=lambda pair: str(pair[0]))]


def summarize(inputs):
    """Read (stage, round, Path) triples; return a report even for invalid records."""
    diagnostics, exports, global_ids = [], [], {}

    def diagnostic(severity, code, source, **details):
        diagnostics.append(dict(severity=severity, code=code, source=str(source), **details))

    for stage, round_number, path in inputs:
        path = Path(path)
        spans = {}
        exports.append((stage, int(round_number), path, spans))
        try:
            with path.open(encoding="utf-8-sig") as stream:
                for lineno, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line, object_pairs_hook=_object,
                                            parse_constant=_invalid_constant)
                        if isinstance(record, dict) and set(record) in ({'resourceMetrics'}, {'resourceLogs'}):
                            signal = next(iter(record))
                            if not isinstance(record[signal], list):
                                raise ValueError('invalid non-trace envelope')
                            diagnostic('info', 'non_trace_signal_ignored', path, line=lineno, signal=signal)
                            continue
                        parsed = _spans(record)
                    except (ValueError, TypeError) as exc:
                        diagnostic("error", "invalid_record", path, line=lineno, reason=str(exc))
                        continue
                    for span in parsed:
                        key = span["trace"], span["id"]
                        if key in global_ids:
                            previous, previous_path = global_ids[key]
                            if previous_path != path:
                                diagnostic("error", "span_reused_across_inputs", path, line=lineno)
                            elif previous["raw"] != span["raw"]:
                                diagnostic("error", "conflicting_span_snapshots", path, line=lineno)
                            else:
                                diagnostic("info", "identical_span_deduplicated", path, line=lineno)
                            continue
                        global_ids[key] = span, path
                        spans[key] = span
        except (OSError, UnicodeError) as exc:
            diagnostic("error", "unreadable_input", path, reason=str(exc))
        if not spans:
            diagnostic("error", "no_spans", path)

    roots, chats = [], []
    response_ids = {}
    for stage, round_number, path, spans in exports:
        root_traces, usage_traces = set(), set()
        for span in spans.values():
            attrs = span["attributes"]
            if span["operation"] in ("invoke_agent", "chat"):
                usage_traces.add(span["trace"])
            ancestry, current = set(), span
            while current is not None:
                if current["id"] in ancestry:
                    diagnostic("error", "parent_cycle", path, span_id=span["id"])
                    break
                ancestry.add(current["id"])
                current = spans.get((current["trace"], current["parent"]))
            is_root = span["operation"] == "invoke_agent" and (span["trace"], span["parent"]) not in spans
            if span["parent"] and (span["trace"], span["parent"]) not in spans:
                diagnostic("warning", "missing_parent", path, span_id=span["id"])
            if span["parent"] == span["id"]:
                diagnostic("error", "self_parent", path, span_id=span["id"])
            if span["raw"].get("endTimeUnixNano") in ("0", 0):
                diagnostic("warning", "unfinished_span", path, span_id=span["id"])
            if not is_root and span["operation"] != "chat":
                continue
            model = (attrs.get("gen_ai.response.model") if not is_root else None) or attrs.get("gen_ai.request.model")
            if not isinstance(model, str) or not model:
                model = None
                diagnostic("warning", "missing_model", path, span_id=span["id"])
            # Preserve request-level facts for pricing before aggregation: long
            # context tiers cannot be selected from stage-wide token totals.
            ancestor, root_id = span, None
            visited = set()
            while ancestor is not None and ancestor['id'] not in visited:
                visited.add(ancestor['id'])
                if ancestor['operation'] == 'invoke_agent':
                    root_id = ancestor['id']
                ancestor = spans.get((ancestor['trace'], ancestor['parent']))
            row = dict(stage=stage, round=round_number, model=model,
                       source=str(path), trace_id=span['trace'], span_id=span['id'],
                       root_span_id=root_id,
                       model_basis='response' if attrs.get('gen_ai.response.model') else 'request',
                       error_type=attrs.get('error.type'),
                       start_time=span['raw'].get('startTimeUnixNano'),
                       end_time=span['raw'].get('endTimeUnixNano'))
            keys = ROOT_KEYS if is_root else TOKEN_KEYS
            missing = []
            for field, attribute in keys.items():
                row[field] = None
                if attrs.get(attribute) is None:
                    missing.append(field)
                else:
                    try:
                        row[field] = _count(attrs[attribute])
                    except ValueError as exc:
                        diagnostic("error", "invalid_measure", path, span_id=span["id"], field=field, reason=str(exc))
            if missing:
                diagnostic("warning", "missing_measures", path, span_id=span["id"], fields=missing)
            if is_root:
                root_traces.add(span["trace"])
                roots.append(row)
            else:
                response = attrs.get("gen_ai.response.id")
                if response is not None:
                    if not isinstance(response, str) or not response.strip():
                        diagnostic("error", "invalid_response_id", path, span_id=span["id"])
                    else:
                        namespace = _response_namespace(attrs)
                        for previous_namespace, previous_path, previous_span in response_ids.get(response, []):
                            if not _distinct_namespaces(namespace, previous_namespace):
                                diagnostic("error", "repeated_chat_response", path,
                                           span_id=span["id"], namespace=namespace,
                                           previous_source=str(previous_path),
                                           previous_trace_id=previous_span["trace"],
                                           previous_span_id=previous_span["id"],
                                           previous_namespace=previous_namespace)
                        response_ids.setdefault(response, []).append((namespace, path, span))
                chats.append(row)
        if not usage_traces:
            diagnostic("warning", "no_root_invoke_agent", path)
        for trace in sorted(usage_traces - root_traces):
            diagnostic("warning", "no_root_invoke_agent", path, trace_id=trace)

    invalid = any(d["severity"] == "error" for d in diagnostics)
    incomplete = any(d["severity"] == "warning" for d in diagnostics)
    report = {
        "schema_version": 1,
        "status": "invalid" if invalid else "incomplete" if incomplete else "ok",
        "export_coverage": "unverified",
        "inputs": [dict(stage=s, round=int(r), path=str(p)) for s, r, p in inputs],
        "root_usage": {"model_basis": "root requested model; not child-model cost allocation",
                       "totals": _aggregate(roots, ROOT_KEYS), "groups": _groups(roots, ROOT_KEYS)},
        "chat_usage": {"model_basis": "response model, falling back to requested model",
                       "totals": _aggregate(chats, TOKEN_KEYS), "groups": _groups(chats, TOKEN_KEYS)},
        "root_records": roots if not invalid else [],
        "chat_records": chats if not invalid else [],
        "diagnostics": diagnostics,
        "notes": [
            "nano_aiu is read only from root invoke_agent; 1 AIU = 1000000000 nano_aiu.",
            "github.copilot.cost is a billing model multiplier, not currency; it is not summed.",
            "Root and chat usage overlap; do not add them. Cache tokens are separate overlapping input measures.",
            "null means unavailable or incomplete; known_sum is only the observed subtotal.",
            "Complete export coverage and distinct-ID streaming duplicates without response IDs cannot be verified.",
            "Response ID collisions across inputs/traces are invalid unless provider/endpoint namespaces are provably different; unknown namespaces never clear a collision.",
            "An export is one input file. Missing parent spans can make a subagent appear to be a root.",
        ],
    }
    if invalid:
        # Even plausible partial numbers must not look like a usable complete result.
        report["root_usage"] = None
        report["chat_usage"] = None
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", nargs=3, action="append", required=True,
                        metavar=("STAGE", "ROUND", "JSONL"), help="repeat per writer/reviewer export")
    parser.add_argument("--output", type=Path, required=True, help="explicit JSON report path")
    args = parser.parse_args(argv)
    inputs, input_paths = [], set()
    for stage, round_text, path_text in args.input:
        if stage not in ("writer", "reviewer") or not round_text.isdecimal() or int(round_text) < 1:
            parser.error("STAGE must be writer/reviewer and ROUND a positive integer")
        path = Path(path_text).resolve()
        if path in input_paths:
            parser.error("the same input file cannot be assigned twice")
        input_paths.add(path)
        inputs.append((stage, int(round_text), path))
    output = args.output.resolve()
    for path in input_paths:
        if output == path or (output.exists() and path.exists() and os.path.samefile(output, path)):
            parser.error("output must not overwrite an input (including links)")
    report = summarize(inputs)
    try:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except OSError as exc:
        parser.error(str(exc))
    print(f"{report['status']}: {output}")
    return {"ok": 0, "incomplete": 1, "invalid": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
