# Geit

Geit 读取 `sources.txt`，把其中未以 `#` 注释的有效来源聚合成影视 App 可直接导入的 TVBox 配置。采集、去重、字段校验和健康标记都由 GitHub Actions 自动完成。

## 直接使用

完整配置（推荐）：

```text
https://raw.githubusercontent.com/hegio/Geit/main/geit.json
```

追加本仓库 Python 站点的配置：

```text
https://raw.githubusercontent.com/hegio/Geit/main/geitpy.json
```

鱼壳/海豚兼容入口（明文 JSON，不使用前缀、Base64 或加密）：

```text
https://raw.githubusercontent.com/hegio/Geit/main/geit-fish.txt
https://raw.githubusercontent.com/hegio/Geit/main/geitpy-fish.txt
```

兼容旧入口的同一份完整配置：

```text
https://raw.githubusercontent.com/hegio/Geit/main/docs/tvbox.json
```

GitHub 的 `/blob/` 地址打开的是 HTML 网页，不是 JSON 文件，不能作为影视 App 订阅。影视 App 会出现“配置取得失败”或类似提示。请使用上面的 `raw.githubusercontent.com` 地址；如果本地网络无法直连 Raw，可在 App 允许时使用 `gh-proxy.org` 等代理前缀。

## 产物

| 文件 | 用途 |
|---|---|
| `geit.json` | 聚合来源中的站点、直播源和 `spider.jar`，TVBox 可直接导入 |
| `geitpy.json` | 在 `geit.json` 基础上追加 `py/` 内全部有效 Python 站点 |
| `geit-fish.txt` | 与 `geit.json` 站点/直播相同的明文 JSON，补齐鱼壳/海豚常用顶层字段 |
| `geitpy-fish.txt` | 与 `geitpy.json` 站点/直播相同的明文 JSON，包含 Python 站点 |
| `tvbox/lives/merged.m3u` | 从 TXT/M3U 来源合并并按 URL 去重的直播列表 |
| `docs/tvbox.json` | `geit.json` 的兼容副本，旧链接可直接使用 |
| `docs/sources.json` | 每个来源的抓取状态和解析结果 |
| `docs/health.json` / `.csv` | 链接健康与播放验证状态 |
| `docs/blocked.csv` | 已明确失败的链接，供人工复查或临时屏蔽 |
| `py/` | 去重后、通过 Python AST 与 Spider 核心方法检查的 Python 站点 |
| `回收/` | 清理前旧目录和重复文件，仅作追溯，不参与构建 |

`spider.jar` 使用仓库根目录中的文件，并在配置里写成绝对 raw 地址。不要把本地相对路径 `./spider.jar` 用在远程订阅中，否则 App 无法确定 JAR 的位置。

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

工作流位于 `.github/workflows/collect.yml`，默认每 18 小时运行一次，也可在 Actions 页面手动触发。链接健康检查独立位于 `.github/workflows/health.yml`，默认每 72 小时运行一次。

每次运行会：

1. 读取 `sources.txt`，抓取并识别 JSON / TXT / M3U。
2. 解密加密配置，提取并校验其中的 Python 文件。
3. 按站点 API、直播 URL 和频道 URL 去重。
4. 按文件内容哈希去重 Python；同一内容的重复文件移入忽略提交的 `回收/重复Python/`。
5. 生成 `geit.json`、`geitpy.json`、两个明文 fish JSON、`merged.m3u`、文档页和健康报告。
6. 运行语法与结构校验，然后只提交生成产物和必要脚本。

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

健康状态是带时间戳的诊断结果，不会因为一次失败就永久删除来源。HTTP 200 不等于可播放，完整频道验证应结合播放列表内容、媒体分片和影视 App 实测。

`docs/blocked.csv` 会汇总明确 HTTP 失败、ffprobe 失败和配置无法解析的条目；`requires_app`、`reachable` 和 `playback_unverified` 不会被当作永久失效。地区限制、CDN、UA、代理和临时超时都可能造成误判，因此标记用于复查，不会自动改动 `sources.txt`。

## 本地运行

```powershell
python -m pip install pycryptodome
$env:GITHUB_REPOSITORY = 'hegio/Geit'
$env:GITHUB_REF_NAME = 'main'
$env:PYTHONIOENCODING = 'utf-8'
python fetch_tvbox.py
python check_health.py --workers 16 --max-channels 300
```

工作流使用 `pycryptodome` 解密；安装失败会直接中止，不会悄悄跳过加密来源。

## 注意

- `sources.txt` 由维护者管理，脚本只读取，不覆盖或重排。
- 公开仓库中的第三方配置可能包含敏感信息，提交前应确认来源授权。
- 若曾把含真实 token 的配置提交或分享出去，应立即在对应服务端轮换该凭据；仅从最新文件删除并不能让旧提交中的值失效。
- 频道失效具有时效性，建议定期查看 `docs/health.csv` 并在影视 App 中复测。
