# モデル工程の再現試行

自動テストの backend 代役は工程制御だけを検証する。意味の品質は別コンテキストのモデルで試す。
この文書の期待する動作と過去の指摘は執筆モデルに渡さず、fixtures/ の生RTLと通常の材料だけを渡す。

`run.py prepare legacy_latch --rtl-dir <skill>/tests/fixtures --out <fresh-output>` と compact_ctrl で試す。
各試行は新しい材料と文脈を使う。TASK→執筆→check --draft→独立review→check の順で実行する。
モデル・ツール版、修正回数、入力診断、機械検査、独立承認を記録し、文章の完全一致では採点しない。

legacy_latch の原本から成立する動作:

- 旧式宣言の7ポートと W パラメータ。親は bus_shell、valid の受動観測先は watch_flag。
- rst_n は非同期アクティブLowで value/valid を0にする。クロック待ちの遅延を記述しない。
- clear と put が同時なら後の代入により put が勝つ。clearだけでは value を変えない。
- put が連続すると各クロックで value を上書きする。無入力では値を保持する。

compact_ctrl の原本から成立する動作:

- ANSI共有宣言の8ポート。done は出力。省略された方向・幅は同じ宣言内で継承する。
- cancel は busy の処理完了と start より優先。取消で dout は変わらず done は0になる。
- busy中のstartは受け付けない。startを保持すると受付と完了を交互に行う。
- doneのクリアと新たな受付は同時に成立できる。done=0だけから待機へ遷移したと判断しない。

規則、モード表、状態図、期待波形でこれらが矛盾しないことを評価する。
図の外部入力CDC印は推定であり、入力元クロックが無い場合は確定しない。
機械検査のNG0と独立レビューの承認を別々に記録する。RTLの機能検証を実行していなければその旨を記録する。

大きな回路の評価には、利用先の複数FSM・CDC・背圧・ラッパを含む代表RTLも追加する。
小さいfixtureの合格だけから全RTLでの品質を保証しない。
