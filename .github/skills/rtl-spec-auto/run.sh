#!/usr/bin/env bash
# rtl-spec-auto: Copilot CLI で仕様書を 1 本、無人で作る (骨格 → 執筆 → 機械検査 → 任意で別コンテキストの review → 機械検査)。
#
#   .github/skills/rtl-spec-auto/run.sh <module> [--mode basic|extended] [--review] [--writer MODEL] [--reviewer MODEL]
#                                       [--rtl-dir rtl] [--spec-dir doc/spec]
#
# 既定: --mode extended (親 RTL・隣接 spec の概要・architecture も読む)、執筆 claude-opus-5、review claude-sonnet-5。
#       --mode basic は執筆 gpt-5.6-luna (RTL と骨格だけ読む)。モデル名は `copilot` 対話中の /model 一覧の表記に合わせる (要確認)。
# 環境変数: COPILOT_EXTRA (追加オプション。権限プロンプトで止まるとき --allow-all-tools。サンドボックス内でのみ)
# ログ: <spec-dir>/.rtl-spec-auto/<module>-<phase>.md (Copilot の --share。git 管理外)
set -euo pipefail

m="" ; mode=extended ; review=0 ; RTL=rtl ; SPEC=doc/spec ; WRITER="" ; REVIEWER="claude-sonnet-5"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="$2"; shift ;;
    --review) review=1 ;;
    --writer) WRITER="$2"; shift ;;
    --reviewer) REVIEWER="$2"; shift ;;
    --rtl-dir) RTL="$2"; shift ;;
    --spec-dir) SPEC="$2"; shift ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) m="$1" ;;
  esac
  shift
done
[[ -n "$m" ]] || { echo "usage: run.sh <module> [--mode basic|extended] [--review] [--writer M] [--reviewer M] [--rtl-dir D] [--spec-dir D]" >&2; exit 2; }
if [[ -z "$WRITER" ]]; then
  case "$mode" in basic) WRITER="gpt-5.6-luna" ;; extended) WRITER="claude-opus-5" ;; *) echo "mode は basic か extended" >&2; exit 2 ;; esac
fi

SK=".github/skills/rtl-spec-auto"
PERM=(--allow-tool='shell(py:*),shell(python:*),shell(python3:*),read,write' --no-ask-user)
LOGDIR="$SPEC/.rtl-spec-auto"; mkdir -p "$LOGDIR"
PY=py; command -v py >/dev/null 2>&1 || PY=python3; command -v "$PY" >/dev/null 2>&1 || PY=python

rtl="$RTL/$m.v"; [[ -f "$rtl" ]] || rtl="$RTL/$m.sv"
[[ -f "$rtl" ]] || { echo "RTL がない: $RTL/$m.v" >&2; exit 2; }

# 1. 骨格 (既存の spec があれば .new.md へ = 更新モード)
if [[ -f "$SPEC/$m.md" ]]; then
  "$PY" "$SK/scripts/rtl_extract.py" "$rtl" --rtl-dir "$RTL" -o "$SPEC/$m.new.md" --force
  task="更新: 既存の $SPEC/$m.md の本文を保ち、$SPEC/$m.new.md の I/O 表・パラメータ表・動作モード表・境界値候補との差分だけを反映して .new.md を削除する。"
else
  "$PY" "$SK/scripts/rtl_extract.py" "$rtl" --rtl-dir "$RTL" -o "$SPEC/$m.md"
  task="新規: $SPEC/$m.md の TODO と候補表をすべて確定する。"
fi

# 2. 執筆
copilot -p "/rtl-spec-auto スキルの手順で $m の仕様書を完成させる。モードは ${mode}。RTL は $rtl、spec ディレクトリは $SPEC。$task 手順 4 の check_io / check_spec --strict が NG 0 になるまで直し、最後に 3 行で報告する。" \
  --model="$WRITER" "${PERM[@]}" ${COPILOT_EXTRA:-} --share="$LOGDIR/$m-draft.md"

# 3. 検査 (モデルの自己申告を信用しない)
"$PY" "$SK/scripts/check_io.py" --rtl-dir "$RTL" --spec-dir "$SPEC" "$m" || true
"$PY" "$SK/scripts/check_spec.py" --strict --rtl-dir "$RTL" --spec-dir "$SPEC" "$m" || draft_ng=1

# 4. 任意: 別コンテキストで review (執筆側の会話は渡さない)
if [[ $review -eq 1 ]]; then
  copilot -p "/rtl-spec-auto review $m (RTL は $rtl、spec は $SPEC/$m.md)。反証から入り、blocking を直接修正し、check_spec --strict を再実行して報告する。" \
    --model="$REVIEWER" "${PERM[@]}" ${COPILOT_EXTRA:-} --share="$LOGDIR/$m-review.md"
  "$PY" "$SK/scripts/check_io.py" --rtl-dir "$RTL" --spec-dir "$SPEC" "$m" || true
  "$PY" "$SK/scripts/check_spec.py" --strict --rtl-dir "$RTL" --spec-dir "$SPEC" "$m" || review_ng=1
fi

echo "--- done: $SPEC/$m.md (mode=$mode writer=$WRITER review=$review; log: $LOGDIR/)"
[[ -z "${draft_ng:-}${review_ng:-}" ]] || { echo "check_spec --strict に NG が残っている。ログを確認する。" >&2; exit 1; }
