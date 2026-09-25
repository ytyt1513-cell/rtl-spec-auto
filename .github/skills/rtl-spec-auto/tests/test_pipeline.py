"""Behavioral regressions; run: python -m unittest discover -s <skill>/tests -v.

Backend stubs test orchestration, not model quality. Independent model trials are
recorded separately in the worklog. No Copilot/network is used by these tests.
"""
import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
from rtlparse import parse, parents
from source_inventory import inventory
import check_edges

spec = importlib.util.spec_from_file_location("pipeline_runner", SKILL / "run.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

RTL = """module dut(input clk, rst, input d, output reg q);
always @(posedge clk) if (rst) q <= 0; else q <= d;
endmodule
"""
DOC = """# dut
## 1. 概要
同期式の1ビット保持回路。
### 1.1 用語
clkの立上りで取り込む。入力変化をcycle 0とする。
### 1.2 周辺接続
単体入力。接続図は未生成。
## 2. インターフェース
| Signal | Dir | Bits | 種別 | 意味 |
|---|---|---|---|---|
| clk | in | 1 | 値 | 基準クロック |
| rst | in | 1 | レベル | 同期リセット |
| d | in | 1 | 値 | 入力データ |
| q | out | 1 | 値 | 保持データ |
## 3. 機能
### 3.1 機能一覧
| ID | 機能 | 概要 |
|---|---|---|
| F-1 | 保持 | 入力を保持 |
### 3.2 保持
入力をクロックで保持する。
| ID | 規則 |
|---|---|
| R-01 | リセットが有効なら次の立上りで出力を0にする。 |
| R-02 | リセットが無効なら次の立上りで入力値を出力に保持する。 |
![R-01](dut_exp_R-01.svg)
cycle 1のリセットをcycle 2で反映する。
## 4. 横断
### 4.1 動作モード
モードは無い。
### 4.2 同一cycleの優先順
R-01をR-02より優先する。
### 4.3 リセット
R-01を参照。
## 5. 代表シーケンス
R-01の波形を参照。
## 6. 制約・非対応
入力は立上り前に確定する。
## 7. 付録
### 7.1 規則一覧
R-01〜R-02。
### 7.2 派生規則
該当なし。
### 7.3 情報源
dut.v。
"""
WAVE = {"signal": [{"name": "clk", "wave": "p..."}, {"name": "rst", "wave": "010."},
                    {"name": "q", "wave": "1.0."}], "head": {"text": "R-01"}}


class Cases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rtl-spec-test-")
        self.root = Path(self.temp.name).resolve()
        self.rtl = self.root / "source with spaces"
        self.rtl.mkdir()
        self.out = self.root / "result with spaces"

    def tearDown(self):
        self.temp.cleanup()

    def source(self, name, text):
        p = self.rtl / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def prepare(self):
        self.source("dut.v", RTL)
        with contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(["prepare", "dut", "--rtl-dir", str(self.rtl), "--out", str(self.out), "--no-diagram"])
        self.assertEqual(code, 0)

    def product(self):
        (self.out / "dut.md").write_text(DOC, encoding="utf-8")
        runner.save(self.out / "dut_exp_R-01.json", WAVE)

    def approve(self):
        runner.save(self.out / "review.json", dict(verdict="approve", artifact_sha256=runner.snapshot(self.out),
                    findings=[], input_assessment="No diagram; ports checked against original RTL.",
                    reviewed_files=["dut.md", "_rtl/dut.v", "dut_exp_R-01.json"]))

    def test_port_layouts_have_same_interface(self):
        variants = [
            "input clk, rst, input [7:0] d, output reg [7:0] q",
            "input clk\n, rst\n, input [7:0] d\n, output reg [7:0] q",
            "input\nwire clk, rst, input wire\n[7:0]\nd, output reg\n[7:0] q",
            "(* keep = 1 *) input clk, rst, input signed [7:0] d, output logic [7:0] q",
        ]
        expected = [("clk", "in", 1), ("rst", "in", 1), ("d", "in", 8), ("q", "out", 8)]
        for variant in variants:
            with self.subTest(variant=variant):
                p = self.source("dut.v", "module dut(" + variant + "); endmodule")
                self.assertEqual([(x["name"], x["dir"], x["width"]) for x in parse(p)["ports"]], expected)

    def test_old_style_wrapped_declarations(self):
        p = self.source("old.v", "module old(a,b,y); input [7:0]\n a,b; output [7:0] y; endmodule")
        self.assertEqual([(x["name"], x["dir"], x["width"]) for x in parse(p)["ports"]],
                         [("a", "in", 8), ("b", "in", 8), ("y", "out", 8)])

    def test_compact_declaration_regression(self):
        p = self.source("compact.v", "module compact(input clk, input rst_n, input start, cancel, input [7:0] din, output reg [7:0] dout, output reg busy, done); endmodule")
        self.assertEqual([(q["name"], q["dir"], q["width"]) for q in parse(p)["ports"]],
                         [("clk", "in", 1), ("rst_n", "in", 1), ("start", "in", 1), ("cancel", "in", 1),
                          ("din", "in", 8), ("dout", "out", 8), ("busy", "out", 1), ("done", "out", 1)])
        self.out.mkdir()
        md = self.out / "bad.md"
        md.write_text(DOC.replace("| d | in | 1 |", "| cancel | in | 1 |").replace("| q | out | 1 |", "| done | in | 1 |"), encoding="utf-8")
        code, output = runner.process([sys.executable, SKILL / "scripts/check_func_spec.py", md, "--rtl", p])
        self.assertNotEqual(code, 0)
        self.assertIn("rst_n", output)
        self.assertIn("done", output)

    def test_subroutine_arguments_are_not_module_ports(self):
        p = self.source("old.v", "module old(a,y); input a; output y; function f; input x; f=x; endfunction endmodule")
        self.assertEqual([x["name"] for x in parse(p)["ports"]], ["a", "y"])

    def test_multi_instance_declaration_is_diagnosed(self):
        self.source("bundle.v", "module leaf(input a); endmodule\nmodule top(input a); leaf one(a), two(a); endmodule")
        report = inventory(self.rtl, self.out)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("MULTI_INSTANCE", [d["code"] for d in report["diagnostics"]])

    def test_nested_bundle_and_positional_order(self):
        self.source("nested/bundle.sv", "module leaf(y,a); input a; output y; assign y=a; endmodule\n"
                    "module parent(input x, output z); leaf u(z,x); endmodule")
        result = inventory(self.rtl, self.out)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(set(result["modules"]), {"leaf", "parent"})
        self.assertEqual(parents(self.out / "_rtl", "leaf")[0]["conns"], {"y": "z", "a": "x"})

    def test_bundled_model_trial_inputs(self):
        report = inventory(SKILL / "tests/fixtures", self.out)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(set(report["modules"]), {"legacy_latch", "compact_ctrl", "bus_shell", "watch_flag"})
        self.assertEqual(len(report["modules"]["compact_ctrl"]["ports"]), 8)
        parent = parents(self.out / "_rtl", "legacy_latch")[0]
        self.assertEqual(parent["parent"], "bus_shell")
        self.assertEqual(parent["conns"]["valid"], "pending")
        self.assertIn("watch_flag", parent["neighbors"])

    def test_short_named_connection(self):
        self.source("bundle.v", "module leaf(input a); endmodule\nmodule top(input a); leaf u(.a); endmodule")
        self.assertEqual(inventory(self.rtl, self.out)["status"], "ready")
        self.assertEqual(parents(self.out / "_rtl", "leaf")[0]["conns"], {"a": "a"})

    def test_nested_parameters_and_nonansi_header_order(self):
        self.source("bundle.v", "module leaf #(parameter W=1)(y,a); input a; output y; endmodule\n"
                    "module top(input a, output z); leaf #(.W($clog2((4+4)))) u(z,a); endmodule")
        report = inventory(self.rtl, self.out)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(parents(self.out / "_rtl", "leaf")[0]["conns"], {"y": "z", "a": "a"})

    def test_case_only_module_names_do_not_overwrite(self):
        self.source("bundle.v", "module Leaf(input a); endmodule\nmodule leaf(input b); endmodule")
        report = inventory(self.rtl, self.out)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("COLLIDING_MODULE_PATH", [d["code"] for d in report["diagnostics"]])

    def test_duplicate_module_is_not_silently_selected(self):
        self.source("one.v", RTL)
        self.source("nested/two.sv", RTL)
        report = inventory(self.rtl, self.out)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("DUPLICATE_MODULE", [d["code"] for d in report["diagnostics"]])

    def test_build_file_list_excludes_duplicate(self):
        one = self.source("one.v", RTL)
        self.source("nested/other.v", RTL)
        listing = self.root / "selected.txt"
        listing.write_text("# build A\n" + str(one.relative_to(self.root)) + "\n", encoding="utf-8")
        self.assertEqual(inventory(self.rtl, self.out, file_list=listing)["status"], "ready")

    def test_unsupported_inputs_fail_closed(self):
        inputs = ["`ifdef A\n" + RTL + "`endif", "module dut(input logic [1:0][3:0] a); endmodule",
                  "module dut(input a); leaf u(.*); endmodule", "module dut(input a); leaf u[2:0](a); endmodule"]
        for i, raw in enumerate(inputs):
            with self.subTest(raw=raw):
                self.source("dut.v", raw)
                result = inventory(self.rtl, self.root / str(i))
                self.assertEqual(result["status"], "blocked")

    def test_cp932_is_explicit(self):
        p = self.rtl / "dut.v"
        p.write_bytes(("// 日本語\n" + RTL).encode("cp932"))
        self.assertEqual(inventory(self.rtl, self.out)["status"], "blocked")
        self.assertEqual(inventory(self.rtl, self.root / "cp932", "cp932")["status"], "ready")

    def test_prepare_rejects_stale_output(self):
        self.prepare()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(["prepare", "dut", "--rtl-dir", str(self.rtl), "--out", str(self.out)]), 2)

    def test_machine_success_is_not_review_success(self):
        self.prepare()
        self.product()
        errors, _ = runner.machine_check(self.out)
        self.assertEqual(errors, [])
        self.assertTrue(runner.review_errors(self.out))
        self.approve()
        self.assertEqual(runner.review_errors(self.out), [])
        with (self.out / "dut.md").open("a", encoding="utf-8") as f:
            f.write("\n変更\n")
        self.assertTrue(any("stale" in e for e in runner.review_errors(self.out)))

    def test_bad_wave_and_missing_image_cannot_pass(self):
        self.prepare()
        self.product()
        runner.save(self.out / "dut_exp_R-01.json", {**WAVE, "edge": ["a->b missing"]})
        errors, _ = runner.machine_check(self.out)
        self.assertTrue(errors)
        (self.out / "dut.md").write_text(DOC + "\n![missing](absent.svg)\n", encoding="utf-8")
        errors, _ = runner.machine_check(self.out)
        self.assertTrue(any("image missing" in e for e in errors))

    def test_renderer_failure_is_not_ignored(self):
        self.prepare()
        self.product()
        with patch.object(runner, "renderer", return_value=None):
            errors, _ = runner.machine_check(self.out)
        self.assertTrue(any("renderer unavailable" in e for e in errors))

    def test_blank_waveform_and_empty_rules_do_not_pass(self):
        self.prepare()
        self.product()
        runner.save(self.out / "dut_exp_R-01.json", {"signal": [{}], "head": {"text": "R-01"}})
        errors, _ = runner.machine_check(self.out)
        self.assertTrue(any("no nonempty lanes" in e for e in errors))
        (self.out / "dut.md").write_text(DOC.replace("| R-01 |", "| deleted-01 |").replace("| R-02 |", "| deleted-02 |"), encoding="utf-8")
        errors, _ = runner.machine_check(self.out)
        self.assertTrue(any("no behavior rules" in e for e in errors))

    def test_blocking_review_never_approves(self):
        self.prepare()
        self.product()
        self.approve()
        p = self.out / "review.json"
        report = json.loads(p.read_text(encoding="utf-8"))
        report["findings"] = [{"severity": "blocking", "detail": "wrong latency", "rule": "R-02"}]
        runner.save(p, report)
        self.assertTrue(any("unresolved" in e for e in runner.review_errors(self.out)))

    def test_process_failure_and_timeout_return_failure(self):
        self.assertNotEqual(runner.process([sys.executable, "-c", "raise SystemExit(7)"])[0], 0)
        self.assertNotEqual(runner.process([sys.executable, "-c", "import time; time.sleep(1)"], timeout=0.01)[0], 0)

    def test_empty_wave_directory_is_not_success(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertNotEqual(check_edges.main(["check_edges.py", str(self.root)]), 0)

    def test_grouped_waveform_is_checked(self):
        p = self.root / "grouped.json"
        runner.save(p, {"signal": [["group", {"name": "d", "wave": "01", "node": "aa"}]]})
        errors, _, _ = check_edges.check_file(p)
        self.assertTrue(any("重複" in e for e in errors))

    def test_source_change_invalidates_review(self):
        self.prepare()
        self.product()
        self.approve()
        (self.out / "_rtl/dut.v").write_text(RTL + "// changed", encoding="utf-8")
        self.assertTrue(runner.review_errors(self.out))

    def test_backend_repair_clears_old_ng_and_reviews_new_artifacts(self):
        self.source("dut.v", RTL)
        real_process, real_which = runner.process, shutil.which
        writes, reviews = [], []

        def backend(argv, log=None, timeout=900, cwd=None):
            if str(argv[0]) != "fake-copilot":
                return real_process(argv, log, timeout, cwd)
            if "--version" in argv:
                return 0, "test backend"
            self.out = Path(log).parent
            if "rtl-spec-writer" in argv:
                writes.append(1)
                self.product()
                if len(writes) == 1:
                    (self.out / "dut.md").write_text(DOC.replace("| rst | in", "| rst | out"), encoding="utf-8")
            else:
                reviews.append(1)
                self.approve()
            return 0, "backend fixture"

        with patch.object(runner, "process", side_effect=backend), patch.object(runner.shutil, "which", side_effect=lambda n: "fake-copilot" if n == "fake" else real_which(n)), contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(["run", "dut", "--rtl-dir", str(self.rtl), "--work", str(self.root / "runs"),
                                "--copilot", "fake", "--rounds", "2", "--no-diagram"])
        self.assertEqual(code, 0)
        self.assertEqual((len(writes), len(reviews)), (2, 1))
        self.assertEqual(json.loads((self.out / "result.json").read_text(encoding="utf-8"))["status"], "complete")


if __name__ == "__main__":
    unittest.main()
