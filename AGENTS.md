# ゲーム機操作CLI：AI向けの手順書

このフォルダーのCLIで、DS・3DS・Switch・ゲームキューブの実機に入力を送り、画面を取得する。人向けの詳しい説明は`README.md`にある。ここには、AIが操作するときの決まりと手順だけを書く。

コマンドはこのフォルダーで実行する。

```powershell
python .\console_cli.py --profile .\my-3ds.json press a
```

## 守ること

- 操作してよい本体と、そのプロファイル（`--profile`）をユーザーに確かめてから入力する。ほかのソフトやほかのAIが使っている本体には送らない。
- `--profile`は毎回付ける。省くと、このフォルダーの`device.json`が使われる。プロファイルが無ければ`examples\*-device.example.json`をコピーして作り、作ったことをユーザーに伝える。
- 初めて使う形のコマンドは、まず`--dry-run`で確かめる。`--dry-run`は機器を開かない。
- セーブ、データの削除、ソフトリセット、HOME、`power`は、ユーザーの指示があるときだけ行う。
- 3DS Controllerの`firmware`（modern／legacy）とDS FTの`revision`（1〜3）は推測しない。分からなければユーザーに聞く。
- 入力を途中で止めるときは`stop`を使う。入力中のプロセスを強制終了すると、ボタンが押されたまま残る。強制終了してしまったときや、途中で切れたときも、`stop`を送れば全部離れる。
- ほかのソフトを止める、機器を抜く、ドライバーを変えるといった操作はしない。
- 画面を見て確かめていないことを「できた」と報告しない。

## 機種ごとの指定

| 本体 | 入力の`backend` | 映像の`backend` | 送れるもの |
|---|---|---|---|
| 3DS | `qingpi`、`3dscon`（`firmware`が必要） | `ll-spa3`、`n3dsxl`、`loopy-old3ds` | ボタン、十字キー、スライドパッド、タッチ。`3dscon`はCスティックも |
| DS | `dsft`（`revision`が必要） | `ds-ftd2` | ボタン、十字キー、電源。タッチは無い |
| Switch・ゲームキューブ | `ns` | `dshow` | ボタン、十字キー、左右のスティック。タッチは無い |

- ボタン：`a b x y l r zl zr start select home power plus minus lclick rclick capture`。その本体に無いボタンを指定すると、機器を開く前にエラーになる。
- 十字キー：`up down left right up-right down-right down-left up-left`
- スティック：0〜255で中央が128。`lstick`（＝`circle`）が左、`rstick`（＝`cstick`）が右。
- タッチ：下画面の1〜320、1〜240。

## 基本の流れ

```powershell
$capture = "$env:TEMP\my-3ds\capture"   # 本体ごとに分ける

# 1. 映像を開始する
python .\console_cli.py --profile .\my-3ds.json capture start --capture-dir $capture

# 2. 今の画面を見る
python .\console_cli.py --profile .\my-3ds.json snapshot .\screen.png --capture-dir $capture

# 3. 入力する
python .\console_cli.py --profile .\my-3ds.json press a
python .\console_cli.py --profile .\my-3ds.json state --buttons l r --lstick 128 0 --seconds .3
python .\console_cli.py --profile .\my-3ds.json sequence .\steps.json

# 3'. 入力・待ち・画面の取得を1回で行う
python .\console_cli.py --profile .\my-3ds.json press a --wait .3 --snapshot .\after.png --capture-dir $capture
'{"steps":[{"seconds":0.1,"buttons":["a"]},{"seconds":0.3}]}' | python .\console_cli.py --profile .\my-3ds.json sequence - --snapshot .\after.png --capture-dir $capture

# 途中で止める（実行中の入力を全部離して終わらせる）
python .\console_cli.py --profile .\my-3ds.json stop

# 4. 終わったら映像を止める
python .\console_cli.py --profile .\my-3ds.json capture stop --capture-dir $capture
```

## 入力のきまり

- 入力は1回ごとに全部離して終わる。押しっぱなしのまま画面を見ることはできない。続けて押す操作は`sequence`に並べる（1〜128ステップ、各5秒・合計30秒まで。ステップの間では自動で離さない。書かなかったボタンは、そのステップでは離した状態になる）。
- `press`の既定は「0.12秒押して、離したあと0.4秒待つ」。`--wait 0`にすると、画面が変わる前の画像を見てしまうことがある。入力のあとに画面を見るなら、`--wait`は0.2以上にする。
- `--snapshot`は、`--wait`が過ぎたあとに撮れた画面だけを返す。映像が止まっているときは、入力を送らずにエラーになる。

## 結果の読み方

- 入力の結果はJSONで出る。`released`が`true`なら全部離れている。`timing_ms`には、connect（機器を開くまで）・input・wait・snapshot・totalの時間（ms）が入る。遅いときは、CLIの処理・自分の判断待ち・機器の接続のどれが原因かを、これで見分ける。
- 失敗したときも、`phase`（失敗した段階）と時間がJSONで出る。エラーの文は標準エラーに出る。
- 終了コード：成功0、失敗1、`stop`で止まった3。
- `capture start`のJSONの`directory`が出力先。`latest.png`が最新の画面で、`status.json`に画面の番号と時刻（`captured_utc`）、上下画面の位置が入る。

## 画像と座標

- 3DSの画像は400×480。上画面が(0,0)〜(399,239)、下画面が(40,240)〜(359,479)。下画面の点(x, y)をタッチするときは、`touch`に`x - 39`と`y - 239`を渡す。
- DSの画像は256×384。上画面は(0,0)から、下画面は(0,192)から。
- HDMI（dshow）は1画面そのまま。`--size 1280x720`のように小さく取ると、読むのも速い。

## 速く動かすために

全体の速さは、ほぼAIの判断で決まる。CLIは1回0.1〜0.8秒で、画像の遅れは0.2秒以内。

1. 同じ操作の繰り返しは、AIが1手ずつ判断せず、Pythonのスクリプトにして手元で回す。AIは、止まったときと結果だけを見る。
2. 画像を毎回読まない。決まった画面かどうかは数値で判定する。OpenCVの正規化相関の例：

   ```python
   import cv2
   frame = cv2.imread('screen.png')
   template = cv2.imread('template/menu.png')
   x, y, w, h = 40, 240, 320, 240          # 探す範囲（合成画像の座標）
   score = cv2.matchTemplate(frame[y:y+h, x:x+w], template, cv2.TM_CCOEFF_NORMED).max()
   print(round(float(score), 3))           # 0.9前後以上なら一致
   ```

   テンプレートは、`frame[y:y+h, x:x+w]`を`cv2.imwrite`で切り出して作る。
3. 画像を見るときは、必要な範囲だけ切り出して読む。
4. 数手先まで決まっている操作は、`sequence`でまとめて送ってから見る。入力のあとに画面を見るときは、`--snapshot`を付けて1回で呼ぶ。
5. フレーム単位のタイミング（乱数調整など）や、動くものへの即時の反応は、AIからは間に合わない。専用のマクロツールに任せる。

## OBSで配信する（3DS・DS）

```powershell
python .\console_cli.py --profile .\my-3ds.json capture start --obs top --capture-dir $capture   # both／top／bottom
```

- 開始時のJSONの`obs_page`が、出力先に置かれた`obs.html`。OBSの「ブラウザ」ソースで「ローカルファイル」として開く（幅1280、高さ720、「OBSで音声を制御する」にチェック）。
- 配信中も、`snapshot`・`--snapshot`はそのまま使える。
- HDMI（dshow）には使えない。キャプチャーボードはOBSで直接取り込む。
- 配信の開始・終了や、OBSの設定の変更は、ユーザーの指示があるときだけ行う。

## うまくいかないとき

| 表示 | すること |
|---|---|
| `Exactly one matching USB serial device is required` | `devices`（DS FTは`devices --backend dsft`）で、プロファイルのVID・PID・シリアルを確かめる |
| `requires explicit --firmware` ／ `--revision` | ユーザーに聞いて、プロファイルに書く |
| `is not available on the ... backend` | その本体に無いボタン。機種の表を見直す |
| `Another input command is running on this device` | 前の入力が終わるのを待つか、`stop`で止める |
| `already has a running session` | `capture status`で確かめる。自分のセッションなら`capture stop` |
| `No first frame in 15 seconds` | 本体の電源と映像の出力、ほかのソフトが同じキャプチャー機器を使っていないかを確かめる |
| `Capture frame is stale` | 映像が止まっている。`capture status`で確かめてから開始し直す |
| 画面が1秒ほど止まってから戻った | USBの読み取りが失敗し、自動で開き直した。出力先の`*.stdout.log`に`Reconnecting`が残る。何もしなくてよい |
| `No active capture session`（`--snapshot`のとき） | 入力は送られていない。`capture start`してからやり直す |
| `No capture frame newer than the input arrived` | 入力は済んでいる。`capture status`で、映像が止まっていないか確かめる |
| `All-input release was attempted but not confirmed` | 機器が抜けた可能性がある。つなぎ直して`stop`を送り、ユーザーに伝える |
| `Stop requested, but the input command did not exit` | 入力側が応答していない。ユーザーに伝え、強制終了はしない |
| `--obs is only for the 3DS/DS backends` | dshowには使えない。OBSで直接取り込む |
| OBSに「映像を待っています」「映像が届いていません」と出る | `capture status`で映像が動いているか、ソースのファイルが今の出力先の`obs.html`かを確かめる |

## ほかの注意

- 映像の取り込み中（`CaptureCli.exe`が動いている間）は、`build.ps1`でexeを作り直さない。テストは`.\test.ps1 -SkipBuild`で行う。
- `Capture3DS.json`はLL-SPA3の個人の鍵なので、共有・アップロードしない。
