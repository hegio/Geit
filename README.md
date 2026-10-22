# Geit

Geit 读取 `sources.txt`，把其中未以 `#` 注释的有效来源聚合成影视 App 可直接导入的 TVBox 配置。采集、去重、字段校验和健康标记都由 GitHub Actions 自动完成。

## 直接使用

完整配置（推荐）：

```text
https://raw.githubusercontent.com/hegio/Geit/main/geit-fish
```

追加本仓库 Python 站点的配置：

```text
https://raw.githubusercontent.com/hegio/Geit/main/geitpy-fish
```

两个订阅文件都是**明文 JSON，不带后缀**：影视 App 会把返回内容直接当配置解析，不需要前缀、Base64 或解密。

兼容旧入口的同一份完整配置：

```text
https://raw.githubusercontent.com/hegio/Geit/main/docs/tvbox.json
```

GitHub 的 `/blob/` 地址打开的是 HTML 网页，不是 JSON 文件，不能作为影视 App 订阅，否则会提示“配置取得失败”。请使用上面的 `raw.githubusercontent.com` 地址；如果本地网络无法直连 Raw，可在 App 允许时使用 `gh-proxy.org`、`ghfast.top` 等代理前缀。

## 产物

| 文件 | 用途 |
|---|---|
| `geit-fish` | 主订阅：聚合来源中的站点、直播源和 `spider.jar`，明文 JSON 无后缀 |
| `geitpy-fish` | 在 `geit-fish` 基础上追加 `py/` 内全部有效 Python 站点 |
| `tvbox/lives/merged.m3u` | 从 TXT/M3U 来源合并并按 URL 去重的直播列表，`tvg-id` 为稳定频道键 |
| `docs/channel_ledger.json` | 频道台账：来源、原始分组、协议、频道键和多线路关系 |
| `docs/classification_review.json` | 未命中规则的频道清单：仅报告并保留在 `merged.m3u`，便于补充 `tvbox/channel_rules.json` |
| `docs/tvbox.json` | `geit-fish` 的兼容副本，旧链接可直接使用 |
| `docs/sources.json` | 每个来源的抓取状态和解析结果 |
| `docs/health.json` / `.csv` | 链接健康与播放验证状态 |
| `docs/channel_health.json` | 频道级保守判定：保留、观察、候选淘汰；不自动删除 |
| `docs/dead_channels.json` | 连续确定性失败达上限、已从 `merged.m3u` 停止录入的频道 URL 清单 |
| `docs/removed_channels.json` / `.csv` | 剔除历史（频道名 + URL + 来源），人工可查可恢复 |
| `tvbox/whitelist.txt` | 手动白名单：命中即永不自动剔除 |
| `docs/health_state.json` | 健康检查游标、抽样进度和逐 URL 最近证据；供下一轮续跑 |
| `docs/ai_review.json` | 可选 AI 辅助报告：分类建议和失败聚类解释，不参与删除 |
| `docs/blocked.csv` | 已明确失败的链接，供人工复查或临时屏蔽 |
| `py/` | 去重后、通过 Python AST 与 Spider 核心方法检查的 Python 站点 |
| `回收/` | 清理前旧目录和重复文件，仅作追溯，不参与构建 |

`spider.jar` 在配置里写成绝对 HTTPS 地址，默认使用 `https://cnb.cool/fish2035/xs/-/git/raw/main/spider.jar`：这份镜像在影视 App 侧实测可以加载，而 `raw.githubusercontent.com` 的 JAR 在部分网络下取不到，会直接导致「配置取得失败」。仓库根目录仍保留一份同名 JAR 作为备份，不要用相对路径 `./spider.jar` 作为远程订阅地址，否则 App 无法确定 JAR 的位置。

`geit-fish` / `geitpy-fish` 的 `spider`、`logo`、`wallpaper` 三个字段会被保留：手动改过一次之后，后续自动聚合不会再把它们覆盖回空值。只有旧的 `raw.githubusercontent.com` JAR 地址会被强制换回 cnb.cool。

仓库自带 `py/` 目录下的 Python 站点地址统一加上 `https://ghfast.top/` 前缀，例如 `https://ghfast.top/https://raw.githubusercontent.com/hegio/Geit/main/py/xxx.py`，避免影视 App 直连 `raw.githubusercontent.com` 取不到文件。只对 GitHub 主机的地址加前缀：`ghfast.top` 不代理其他域名，第三方 `py`/`js` 链接（如 `jsnz.pages.dev`）保持原样，加前缀只会得到 HTTP 403。

## 来源格式

编辑 `sources.txt`，每行一个地址。空行和 `#` 开头的行会跳过。

支持：

- 普通 TVBox JSON
- 「肥猫工具箱」AES-CBC 加密 JSON
- TXT 直播源，格式为 `频道名,URL`，支持 `#genre#` 分组
- M3U / M3U8 播放列表
- `gh-proxy.com`、`gh-proxy.org`、`ghfast.top` 等代理前缀

采集器会先剥离代理前缀直连，失败时再尝试备用代理。来源中的相对路径会基于原始地址解析；含 `{{SECRET_...}}` 占位符、真实 token、API key、Cookie、密码或 Bearer 凭据的站点会被跳过，不会猜测或公开密钥。

## 自动流程

工作流位于 `.github/workflows/collect.yml`，默认每 18 小时运行一次，也可在 Actions 页面手动触发。链接健康检查独立位于 `.github/workflows/health.yml`，默认每 24 小时运行一次。

GitHub Actions 的 cron 不能直接表达“每 18 小时”：`0 */18 * * *` 只会在 00、18 点触发，实际间隔是 18/6 小时交替。因此 `collect.yml` 每小时唤醒一次，再用 epoch 小时对 18 取模放行。`health.yml` 也每小时唤醒一次，但会读取 `docs/health_state.json` 中上一次成功完成的 `last_run.finished_at`，只有满 24 小时才真正执行；没有状态文件时立即执行。手动触发不受 24 小时 gate 限制。

每次运行会：

1. 读取 `sources.txt`，抓取并识别 JSON / TXT / M3U。
2. 解密加密配置，提取并校验其中的 Python 文件。
3. 按站点 API、直播 URL 和频道 URL 去重；生成稳定频道键并输出可追溯台账。
4. 按文件内容哈希去重 Python；同一内容的重复文件移入忽略提交的 `回收/重复Python/`。
5. 用 `tvbox/channel_rules.json` 分类频道；未命中项写入 `docs/classification_review.json`，但仍保留在 `merged.m3u`。
6. 生成 `geit-fish`、`geitpy-fish`、`merged.m3u`、文档页和健康报告。
7. 运行语法与结构校验，然后只提交生成产物和必要脚本。

采集器不会把第三方原始配置原样提交。`config_security.py` 会递归检查生成配置中的 URL 参数、认证头、用户信息和敏感字段；本地生成和 GitHub Actions 静态校验都会在发现真实凭据时中止。

## 健康标记

```powershell
python check_health.py --workers 16 --max-channels 500
```

`check_health.py` 读取来源、站点、直播和合并频道，输出 `docs/health.json`、`docs/health.csv` 与 `docs/blocked.csv`。健康工作流会安装 `ffprobe`，对支持的直链和 HLS 首条媒体流做播放探测。写盘前会脱敏 `accountinfo`、`token`、`sig`、`signature`、`key` 等查询参数和 URL 用户信息，避免诊断文件泄露凭据。常见状态：

- `reachable`：HTTP 可访问。
- `http_error`：403、404、超时或连接失败。
- `manifest_ok`：播放列表可下载，但频道播放仍需复测。
- `requires_app`：需要影视 App 和对应 spider 解析。
- `playback_unverified`：HTTP 200，不能证明视频真的能播放。
- `probe_ok` / `probe_failed`：使用 `ffprobe` 探测到或未探测到媒体流。

健康状态是带时间戳的诊断结果，不会因为一次失败就永久删除来源。HTTP 200 不等于可播放，完整频道验证应结合播放列表内容、媒体分片和影视 App 实测。HTTP 探测会依次尝试默认客户端、Lavf、okhttp 和 ExoPlayer 四种 UA；只有所有兼容客户端都明确返回 404/410 时才记为确定性失败。任一客户端成功即视为可达。

`docs/blocked.csv` 会汇总明确 HTTP 失败、ffprobe 失败和配置无法解析的条目；`requires_app`、`reachable` 和 `playback_unverified` 不会被当作永久失效。地区限制、CDN、UA、代理和临时超时都可能造成误判，因此标记用于复查，不会自动改动 `sources.txt`。

`docs/channel_health.json` 是频道级台账，记录每条 URL 的频道键、来源、原始分组、确定性失败次数和处置建议。当前只报告 `keep`、`observe`、`observe_soft`、`observe_upstream`、`skip_internal`、`candidate_remove`，不会自动修改 `merged.m3u`。健康工作流每轮只抽检一部分频道，未抽到的线路会保留上一轮记录；已经不在频道台账中的旧 URL 才会被移除。超时、网络不可达、5xx、403/429/451、运营商内网地址和 `playback_unverified` 都不会被当作删除依据；只有连续多次确定性失败才进入 `candidate_remove`。恢复判定同样保守：只有 `probe_ok`（ffprobe 成功解码媒体流）才清零失败次数，`reachable`、`manifest_ok`、`playback_unverified` 都保留历史。

同一主机在本轮同时失败至少 6 条、且失败占该主机已检查线路一半以上，并至少有 3 条硬失败时，会整体降级为 `upstream_incident` / `observe_upstream`。这是为了区分“单条线路坏了”和“整个上游临时挂了”，上游故障绝不累计删除次数。

频道台账为每条线路保存 `url_hash`（原始 URL 的 SHA-256）和脱敏后的 `url`。健康检查使用哈希匹配，因此带 token、签名等敏感参数的直播地址也能稳定关联，同时不会把真实凭据写进公开报告。

### 失效直播源自动清理

健康工作流会把每一轮的结果累加到 `docs/removable.json`，只统计 `geit-fish` 里的 `live` 直播源（它们每轮全量检查，能真正凑够连续次数；频道是抽样轮转的，同一条很可能连续几轮都没被抽到，因此不参与计数）。

- 计入失败：仅高置信确定性失效，例如所有兼容 UA 都返回 404/410，或 ffprobe 在无网络异常的前提下明确报告媒体流不可解码。
- 不计入失败：**超时**、连接被拒、DNS 解析失败、网络不可达、TLS/证书错误、5xx、403/429/451 等地区限制/鉴权/限流。健康工作流跑在 GitHub 境外 runner 上，国内直播源超时率很高，按这些环境因素计数会误删活着的源。
- 恢复判定：只有 `probe_ok` 才清零；`reachable`、`manifest_ok`、`playback_unverified` 不会清空已有失败历史。

前 2 次连续失败只写入 `pending` 供人工预判，不会删除；第 3 次确定性失败（`FAIL_STREAK_LIMIT = 3`）后该源进入 `removable` 列表并在日志里打印名称与来源。这里的 `live` 报告**只报告、不删除**；真正会被自动停止录入的是 `merged.m3u` 中的单个频道 URL（`docs/dead_channels.json`），直播大类始终由人工增减。

聚合侧的读取是容错的：`docs/dead_channels.json` 缺失、为空或无法解析都直接跳过清理，所以聚合不依赖健康报告存在。

次数必须是**连续的**：`count` 只在"本轮抽到该 URL 且又是确定性失效"时 +1。同一轮里若该 URL 有一次 `probe_ok`（ffprobe 成功解码媒体流），`count` 立即归零并从剔除名单中消失；本轮没被抽到、超时、403/429/451、临时 5xx、登录/版权闸门、上游整体故障都只保留原计数，不加也不清零。

### 直播源白名单（手动否决自动剔除）

真正会被自动停止录入的是 `tvbox/lives/merged.m3u` 里的**单个频道 URL**（见 `docs/dead_channels.json`），直播大类从不自动删除。如果你希望某个源无论如何都保留，把它写进 `tvbox/whitelist.txt`（手动维护，一行一条）：

```text
# 完整 URL（精确匹配，同时保护该主机）
https://example.com/live/cctv1.m3u8
# 主机或域名后缀（example.com、a.example.com 都算）
vip4.ddyunbo.com
```

命中白名单的频道 URL 判定为 `whitelisted`：不计失败次数、不进入 `candidate_remove`、不会从 `merged.m3u` 剔除，也不会写入 `docs/removed_channels.json`。`live` 直播源同理，`removable.json` 中会带 `whitelisted: true`。想让某个源重新接受自动检测，删掉或注释对应行即可，下一轮健康检查会自动复检，确认恢复后重新录入。该文件同时被两个工作流监听，改动会立即触发重新聚合。

### 可选 AI 辅助

AI 默认关闭，主工作流不依赖任何模型 API。它只读取已脱敏的待归类频道和失败聚类，生成 `docs/ai_review.json` 中的分类建议、错误解释和人工复核方向；AI **不决定失效、不删除线路、不修改 `merged.m3u`、不写 `removable.json`**。API 不可用、超时或返回格式错误时自动 fail-open，健康检查照常完成。

健康工作流手动触发时可选择 `none` / `local` / `cloud` / `auto`。定时运行默认读取仓库变量 `GEIT_AI_MODE`，未设置就是 `none`。可选配置：

- `local`：`STRATA_BASE_URL`、`STRATA_MODEL`、`STRATA_WIRE_API`（`chat` 或 `responses`）、`STRATA_API_KEY`（可回退 `LOCAL_API_KEY`）。
- `cloud`：`AI_BASE_URL`、`AI_MODEL`、`AI_API_KEY`（可回退 `MAAS_API_KEY`）。
- `auto`：先尝试本地，失败后尝试云端；两者都不可用只写失败状态，不影响主流程。

模型输出会再次经过凭据扫描；检测到疑似真实凭据时拒绝写盘。

## 本地运行

```powershell
python -m pip install pycryptodome
$env:GITHUB_REPOSITORY = 'hegio/Geit'
$env:GITHUB_REF_NAME = 'main'
$env:PYTHONIOENCODING = 'utf-8'
python fetch_tvbox.py
python check_health.py --workers 16 --max-channels 300
```

工作流固定使用 Python 3.12 和 `pycryptodome` 解密；依赖安装失败会直接中止，不会悄悄跳过加密来源。

## 注意

- `sources.txt` 由维护者管理，脚本只读取，不覆盖或重排。
- 公开仓库中的第三方配置可能包含敏感信息，提交前应确认来源授权。
- 若曾把含真实 token 的配置提交或分享出去，应立即在对应服务端轮换该凭据；仅从最新文件删除并不能让旧提交中的值失效。
- 频道失效具有时效性，建议定期查看 `docs/health.csv` 并在影视 App 中复测。
