# ゲーム機操作CLI

DS・3DS・Switch・ゲームキューブの実機にボタン入力を送り、キャプチャー機器から画面を取得する、Windows向けのコマンドラインツールです。Claude CodeやCodexなどのAIに実機を操作させることを想定しています。AI向けの手順書は[AGENTS.md](AGENTS.md)です（Claude Codeは[CLAUDE.md](CLAUDE.md)から読み込みます）。

結果はJSONで標準出力に出ます。入力は毎回、最後に全部のボタンを離して終わるので、途中で失敗しても押しっぱなしになりません。

## 対応している機器

| 本体 | 入力（`--backend`） | 映像（`capture --backend`） |
|---|---|---|
| 3DS | `qingpi`：Qingpi<br>`3dscon`：3DS Controller | `ll-spa3`：LL-SPA3<br>`n3dsxl`：New 3DS LL用キャプチャー基板（FT60x）<br>`loopy-old3ds`：Loopy製の初代3DS用キャプチャー基板 |
| DS | `dsft`：FT232Hを使ったDS本体の入力基板（DS FT） | `ds-ftd2`：DS用キャプチャー基板（FT232H） |
| Switch・ゲームキューブ | `ns`：NX Macro ControllerのSwitch用マイコンと同じ形式のシリアル入力 | `dshow`：HDMIキャプチャーボードなどのDirectShow機器 |

入力と映像の機器は、別々に選べます。送れる入力は機器ごとに違います。

| 入力 | ボタン | 十字キー | スティック | タッチ |
|---|---|---|---|---|
| `qingpi` | a b x y l r zl zr start select home | 8方向 | スライドパッド | 下画面 |
| `3dscon` | a b x y l r zl zr start select home | 8方向 | スライドパッド、Cスティック | 下画面 |
| `dsft` | a b x y l r start select power | 8方向 | なし | なし |
| `ns` | a b x y l r zl zr plus minus lclick rclick home capture（start／selectはplus／minusと同じ） | 8方向 | 左右 | なし |

## 必要なもの

- Windows 10／11（64bit）
- Python 3（3.12で確認）と[pyserial](https://pypi.org/project/pyserial/)
- .NET Framework 4.8（Windowsに標準で入っています）
- 使う機器に合わせたDLL。ライセンスの条件がそれぞれ違うため同梱していません。各自で入手して、`CaptureCli.exe`と同じフォルダーに置いてください。

| DLL | 使う機器 | 入手先 |
|---|---|---|
| `ftd2xx.dll` | DS用キャプチャー基板、DS FT入力 | [FTDI](https://ftdichip.com/)のD2XXドライバー（64bit） |
| `FTD3XX.dll` | New 3DS LL用キャプチャー基板 | [FTDI](https://ftdichip.com/)のD3XXドライバー（64bit） |
| `CyUSB.dll` | LL-SPA3 | [kategray/CyUSB](https://github.com/kategray/CyUSB)の`library/c_sharp`をビルドしたもの |
| `libusb-1.0.dll` | Loopy製の初代3DS用キャプチャー基板 | [libusb v1.0.26](https://github.com/libusb/libusb/releases/tag/v1.0.26)の64bit版 |

`Capture3DS.dll`（[capture3ds-sharp](https://github.com/Nazza-Rella/capture3ds-sharp) v1.2.0）と`DirectShowLib-2005.dll`は同梱しています。

## 準備

1. リリースのzipを展開します。ソースから使うときは、`.\build.ps1`で`CaptureCli.exe`を作ります。
2. `python -m pip install -r requirements.txt`でpyserialを入れます。
3. 上の表のDLLのうち、使う機器の分を置きます。
4. 機器の識別情報を調べます。どのコマンドも機器を開かず、一覧を出すだけです。

   ```powershell
   python .\console_cli.py devices                      # USBシリアル（COM番号、VID、PID、シリアル番号）
   python .\console_cli.py devices --backend dsft       # DS FTの候補
   python .\console_cli.py capture list --backend n3dsxl # 映像の機器。stable_idを控える
   ```

5. `examples`の`*-device.example.json`をコピーして、本体ごとのプロファイルを作ります。`device.json`という名前でこのフォルダーに置くと、`--profile`を省いたときに使われます。

   ```json
   {
     "input": {"backend": "3dscon", "vid": "0x16c0", "pid": "0x0483", "serial": "あなたの機器のシリアル", "firmware": "modern"},
     "capture": {"backend": "n3dsxl", "stable_id": "N3dsxl:serial:あなたの機器のシリアル"}
   }
   ```

   - 入力の機器は、VID・PID・シリアル番号がすべて一致する1台を選びます（COM番号では選びません）。見つからないときや2台以上あるときは、何も送らずにエラーになります。
   - 3DS Controllerは、`firmware`に`modern`か`legacy`を書きます。NX Macro Controllerの「FW:x.4-」が`modern`、「FW:-x.3」が`legacy`です。自動では判定しません。
   - DS FTは、`revision`に基板の版（1〜3）を書きます。版によってB・X・Yの配線が違います。

## 入力

```powershell
python .\console_cli.py --profile .\my-3ds.json press a                       # 0.12秒押して離す
python .\console_cli.py --profile .\my-3ds.json press up --seconds .3         # 十字キー
python .\console_cli.py --profile .\my-3ds.json touch 160 120                 # 下画面をタッチ
python .\console_cli.py --profile .\my-3ds.json lstick 255 128 --seconds .5   # 左スティック（スライドパッド）を右へ
python .\console_cli.py --profile .\my-3ds.json state --buttons l r --lstick 128 0 --seconds .3   # 同時入力
python .\console_cli.py --profile .\my-3ds.json sequence .\examples\sequence.example.json        # 操作の列
python .\console_cli.py --profile .\my-3ds.json release                       # 全部離す
python .\console_cli.py --profile .\my-3ds.json press a --dry-run             # 送信データを表示するだけ（機器を開かない）
```

`--profile`はサブコマンドの前に書きます。プロファイルの代わりに、`--backend`・`--vid`・`--pid`・`--serial`・`--firmware`・`--revision`でも指定できます。

- 1回の入力は「全部離す→約60ms待つ→指定の状態にする→そのまま保持→全部離す→約80ms待つ」の順です。押している時間（`--seconds`）は0〜5秒、離したあとの待ち時間（`--wait`）は0〜30秒で、既定はそれぞれ0.12秒と0.4秒です。
- スティックは0〜255で、中央が128です。`lstick`は`circle`、`rstick`は`cstick`と同じです（`rstick`が使えるのは3DS Controllerと`ns`だけです）。
- タッチは下画面の1〜320、1〜240です。
- `sequence`は、ポートを1回だけ開いて、各ステップの状態を順に送ります。ステップの間で自動的に離すことはしません。1〜128ステップ、各5秒・合計30秒までです。ステップに書かなかったボタンは離した状態、スティックは中央になります。ファイル名の代わりに`-`を渡すと、標準入力から読みます。

  ```json
  {"steps": [
    {"seconds": 0.10, "circle": [255, 128], "touch": [160, 120]},
    {"seconds": 0.06, "circle": [128, 0], "touch": [160, 120], "buttons": ["l"]},
    {"seconds": 0.05}
  ]}
  ```

- 入力の結果はJSONで出ます。`timing_ms`には、機器を開くまで（connect）、入力（input）、待ち（wait）、撮影（snapshot）、合計（total）の時間がミリ秒で入ります。失敗したときも、失敗した段階（`phase`）と時間を出します。
- 終了コードは、成功が0、失敗が1、`stop`で止められたときが3です。

### 途中で止める

```powershell
python .\console_cli.py --profile .\my-3ds.json stop
```

実行中の入力に停止の合図を送り、ボタンを全部離して終了したのを確かめてから戻ります。実行中の入力が無いとき（強制終了されて、ボタンが押されたまま残っているときも）は、`stop`自身が全部離します。どのプロセスも強制終了しません。

入力コマンドは実行中、`input-sessions`の機器ごとのフォルダーに自分の情報を書き込み、押している間と待っている間は5msごとに停止の合図を確認します。同じ機器へ2つの入力が同時に送られることはありません。

## 映像

```powershell
python .\console_cli.py --profile .\my-3ds.json capture start --capture-dir "$env:TEMP\my-3ds\capture"
python .\console_cli.py --profile .\my-3ds.json capture status --capture-dir "$env:TEMP\my-3ds\capture"
python .\console_cli.py --profile .\my-3ds.json snapshot .\screen.png --capture-dir "$env:TEMP\my-3ds\capture"
python .\console_cli.py --profile .\my-3ds.json capture stop --capture-dir "$env:TEMP\my-3ds\capture"
```

- `capture start`は、別プロセス（`CaptureCli.exe`）で取り込みを始め、最初の画面が届いた時点で戻ります。15秒たっても届かなければ、取り込みを止めてエラーにします。
- 出力先には、`latest.png`（最新の画面）と`status.json`（画面の番号・時刻・上下画面の位置）が、既定で150msごとに書き込まれます。間隔は`--save-interval-ms`（20〜10000）で変えられます。
- `--capture-dir`は、開始・状態・停止・撮影のすべてで同じものを指定します。省くと、このフォルダーの`capture`（LL-SPA3）か`capture-バックエンド名`になります。本体を複数使うときは、本体ごとに分けてください。
- `snapshot`は、3秒以上前の画面しか無いときは返さずにエラーにします。
- 1台の機器を、2つの取り込みが同時に開くことはありません。ほかのソフトが使っている機器は、開けないことがあります。
- USBの読み取りが2回続けて失敗したときは、機器を開き直して、同じ出力先のまま続けます。出力先の`<セッションID>.stdout.log`に`Reconnecting`／`Reconnected`が残ります。

入力したあとの画面は、`--snapshot`を付けると同じコマンドで取れます。待ち時間（`--wait`）が過ぎたあとに撮れた画面だけを返します。映像が止まっているときは、入力を送らずにエラーになります。

```powershell
python .\console_cli.py --profile .\my-3ds.json press a --wait .3 --snapshot .\after.png --capture-dir "$env:TEMP\my-3ds\capture"
```

### 画像の座標

| 本体 | 画像の大きさ | 上画面 | 下画面 |
|---|---|---|---|
| 3DS | 400×480 | (0,0)〜(399,239) | (40,240)〜(359,479) |
| DS | 256×384 | (0,0)〜(255,191) | (0,192)〜(255,383) |
| HDMI | 機器の解像度 | 1画面 | なし |

3DSの下画面の点(x, y)をタッチするときは、`touch`に`x - 39`と`y - 239`を渡します。

### HDMIのキャプチャーボード（dshow）

```powershell
python .\console_cli.py capture list --backend dshow
python .\console_cli.py capture start --backend dshow --device "Dshow:@device:pnp:..." --size 1280x720 --mjpg
```

`--device`には、一覧の`stable_id`をそのまま渡します。`--size`と`--mjpg`を付けると、その解像度・形式のうち一番fpsの高いものを機器に要求します。当てはまるものが無ければ機器の既定のままで、`process.json`の`requested_format_applied`が`false`になります。解像度を小さめにすると、画像を読むのも速くなります。

OpenCVのDirectShowでは黒い画面しか取れない機器（AVerMedia Live Gamer EXTREME 2など）も、`CaptureCli.exe`なら取り込めます。機器によっては、開いてから最初の画面まで数秒かかります。

### LL-SPA3の鍵

LL-SPA3はプロダクトキーが無くても動きます（無いときは、約260秒ごとに映像の再初期化が入ります）。キーは`CaptureCli.exe`の隣の`Capture3DS.json`から読みます。n3DS_viewを使ったことがあるPCでは、そのキャッシュから自動で読み込んで保存します。`Capture3DS.json`は個人の鍵なので、共有しないでください。

## OBSで配信する（3DS・DS）

```powershell
python .\console_cli.py --profile .\my-3ds.json capture start --obs top --capture-dir "$env:TEMP\my-3ds\capture"
```

`--obs`を付けると、出力先にOBSのブラウザソース用のページ（`obs.html`）を置き、画面と本体の音を流します。画面は`both`（上下、既定）・`top`・`bottom`から選べます。

1. 開始時のJSONの`obs_page`が、置かれた`obs.html`です。
2. OBSで「ブラウザ」ソースを追加し、「ローカルファイル」にチェックして`obs.html`を選びます。幅1280、高さ720にして、「OBSで音声を制御する」にチェックを入れます。
3. 音はこのソースから出ます。映像は、音と合わせるために約0.25秒遅れて表示されます。

HDMIのキャプチャーボードには使えません。OBSの「映像キャプチャデバイス」で直接取り込んでください。

## 安全のための決まり

- 入力は、始めと終わりに必ず全部離します。送信に失敗したときやCtrl+Cで止めたときも、離す処理を行います。離せたことを確認できなかったときは、成功を返しません。
- 本体を選び間違えないよう、VID・PID・シリアル番号の完全一致で選びます。
- `--dry-run`のときは、機器の一覧の取得も接続もしません。
- ほかのソフトや入力を強制終了しません。
- フレーム単位の正確さは保証しません（Windowsの処理やUSBの転送時間が加わるため）。乱数調整のように厳密なタイミングが必要な操作には、専用のマクロツールを使ってください。

## 動作の確認状況

| 組み合わせ | 実機 |
|---|---|
| Qingpi＋LL-SPA3（3DS） | 確認済み |
| 3DS Controller（legacy）＋New 3DS LL用キャプチャー基板 | 確認済み |
| 3DS Controller（modern） | 送信データのみ確認 |
| DS用キャプチャー基板 | 確認済み |
| DS FT入力 | 送信データのみ確認 |
| Switch・ゲームキューブ（ns）入力 | 送信データのみ確認 |
| HDMI（dshow） | 確認済み（AVerMedia Live Gamer EXTREME 2、720×480 YUY2 60fps） |
| Loopy製の初代3DS用キャプチャー基板 | 未確認 |

送信データは、全組み合わせの対応表（`tests\vectors-*.txt`）と照合しています。

## 開発

```powershell
.\build.ps1             # CaptureCli.exeを作る
.\test.ps1              # 作り直してから、PythonとC#のテストを実行する（機器は開かない）
.\test.ps1 -SkipBuild   # 取り込み中で、exeを上書きできないとき
```

- `console_cli.py`：コマンドの本体
- `input_protocols.py`：入力の送信データを作る部分（機器は開かない）
- `dsft_transport.py`：DS FT（FT232H）を開いて書き込む部分
- `CaptureCli.cs`：映像を取り込む別プロセス（`CaptureCli.exe`）
- `obs`：OBSのブラウザソース用のページ

`CaptureCli.exe`は単体でも動きます（`CaptureCli.exe list BACKEND`、`CaptureCli.exe stream BACKEND OUTPUT_DIR STABLE_ID SESSION_TOKEN`）。止めるときは、出力先に`stop-<SESSION_TOKEN>.request`を作ります。普段は`console_cli.py`から使ってください。

## ライセンス

[MIT](LICENSE)です。同梱しているライブラリのライセンスは`licenses`にあります。

- Capture3DS：MIT（一部はCuteCaptureのApache License 2.0）
- DirectShowLib：LGPL 2.1（ソースは`licenses\DirectShow.NET\source`）
