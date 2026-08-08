# TVBox 配置文件自动采集（GitHub Actions）

每隔 6 小时自动从 `sources.txt` 里列出的地址抓取 TVBox 的配置文件，保存到本仓库的 `tvbox/` 目录，并自动生成 GitHub Pages 订阅页。无需自己的服务器，完全跑在 GitHub 免费额度上。

## 目录结构

```
.
├── .github/workflows/collect.yml   # 定时任务（每 6 小时）
├── fetch_tvbox.py                  # 采集脚本（需 pycryptodome 解密）
├── sources.txt                     # 采集目标地址（每行一个）
├── repo.txt                        # 本地测试用仓库标识 owner/repo（云端自动读环境变量，可忽略）
├── tvbox/                          # 采集结果（自动生成，含下面两项）
│   ├── py.json                     #   自动生成的 TVBox 蜘蛛配置（sites 来自 .py，lives 来自采集源）
│   └── py/                         #   自动复制的蜘蛛脚本（供 py.json 的 ./py/<name>.py 加载）
├── docs/                           # 自动生成的订阅页（index.html + tvbox.json）
└── logs/                           # 运行日志（自动生成）
```

## 部署步骤

1. 在 GitHub 新建一个仓库（**推荐 Public**：Actions 免费额度无限；Private 每月 2000 分钟也够用）。
2. 把本目录里的全部文件推送到仓库（保持 `.github/workflows/collect.yml` 的相对路径不变）。
3. 进入仓库 **Actions** 标签页，能看到 "TVBox 自动采集" 工作流；到点会自动跑，也可点 `Run workflow` 手动触发测试。
4. 采集结果出现在 `tvbox/` 目录；打开 **Settings → Pages**，Source 选 `main` 分支 + `/docs` 目录，即可生成订阅页。

## 配置采集目标（sources.txt）

每行一个地址，`#` 开头为注释。支持两种写法：

**写法 1 · 单文件地址**：GitHub raw、第三方站点都行。
```
https://lytvs.top/%E8%80%81%E6%9D%A8TV%E7%BA%AF%E5%87%80%E7%89%88xxz.json
https://gh-proxy.com/https://raw.githubusercontent.com/FGBLH/HKL/refs/heads/main/ok海豚665
```
- 带 `gh-proxy` / `ghproxy` 等代理前缀时，脚本会**自动剥离前缀、直连 `raw.githubusercontent.com`**（GitHub Actions 内网直达，比代理稳）。
- 地址里含中文（如 `ok海豚665`、`老杨TV`）会自动做 percent-encode，不再出现 `UnicodeEncodeError`。

**写法 2 · 整仓库镜像 `REPO:OWNER/REPO[@分支]`**：自动列出仓库内全部「核心文件」并逐个抓取。
```
REPO:FGBLH/HKL
REPO:other/dev@dev
```
- 默认分支 `main`；可用 `@分支` 指定，如 `REPO:user/repo@master`。
- 只采集白名单扩展名（`.py` `.js` `.json` `.txt` `.m3u` `.md` `.yaml` `.html` 等），**自动跳过 `.jar` / `.png` / `.zip` 等二进制**，避免仓库被撑爆。
- 无扩展名的文件：仅当内容是 TVBox 配置（**支持自动解密**，或纯 JSON）才采集。
- 文件按原目录结构保存到 `tvbox/<OWNER>__<REPO>/...`。
- 若遇到 GitHub API 速率限制（未登录每 IP 60 次/小时），在仓库 `Settings → Secrets → Actions` 加 `GITHUB_TOKEN`（值取仓库的 `secrets.GITHUB_TOKEN`，Actions 默认注入）即可提升到 5000 次/小时。

## 自动解密加密接口

脚本内置对「肥猫工具箱」TVBox 接口编辑器 AES-128-CBC 加密格式的自动解密：

- 下载到全是十六进制、以 `2423`（即 `$#`）开头的文件时，自动识别为加密接口。
- 从 payload 中自包含的 key / iv 提取信息并解密。
- 解密成功后保存为 `.json`，并**继续抓取该配置里引用的 `.py` / `.js` / `.json` / `.txt` / `.m3u` 等核心文件**。

因此 `REPO:FGBLH/HKL` 里的 `ok海豚18` 这类无扩展名加密配置，会被自动解密并保存成 `ok海豚18.json`，同时把配置里提到的所有核心脚本一起采下来。

解密依赖 **pycryptodome**；云端工作流已自动安装，本地测试需先执行 `pip install pycryptodome`。

脚本会自动处理两件事，你不用操心：
- **中文文件名编码**：地址里含中文会自动做 percent-encode。
- **失败重试**：单个地址偶发 TLS 异常 / 5xx 会自动重试最多 4 次，不影响其他地址。

## 自动生成订阅页

每次采集后脚本生成：
- `docs/tvbox.json`：汇总清单（`updated` / `count` / `sources[{name,url,raw}]`），可当作「一个链接订阅全部」的汇总源。
- `docs/index.html`：可视页，每个源带「订阅」直链与「复制」按钮。

订阅链接走 `cdn.jsdelivr.net/gh/OWNER/REPO@分支/tvbox/文件名.json`（比 raw 更稳定、TVBox 兼容好）。仓库标识云端自动读 `GITHUB_REPOSITORY` / `GITHUB_REF_NAME`，本地测试才用 `repo.txt`。

**开启订阅页**：仓库 → **Settings → Pages → Source：Deploy from a branch → 选 `main` 分支、`/docs` 目录** → Save。稍等片刻访问 `https://OWNER.github.io/REPO/`。仓库需 **Public**，jsDelivr 才能拉到文件。

## tvbox/py.json（蜘蛛 + 直播源配置）

脚本每次运行还会在 **`tvbox/` 目录**下生成一份 `py.json`（同时把蜘蛛 `.py` 复制到 `tvbox/py/`），这是一份可直接作为 TVBox 配置使用的「蜘蛛版」清单（配合 `py.jar` 本地 Python 解析）：

- `spider`：固定为 `./py.jar`（需你自备 `py.jar` 才能启用本地 Python 解析）。
- `sites`：**自动生成**自本次采集到的所有 `.py` 蜘蛛脚本——每个 `.py` 生成一个 `type=3` 站点，`api` 指向 `./py/<name>.py`。脚本同时把 `.py` 复制到 `tvbox/py/`，使 `./py/<name>.py` 在 GitHub Pages / jsDelivr 下可直接加载。
- `lives`（**来自采集源，自动合并**）：脚本扫描本次采集到的所有 TVBox 配置（如老杨TV纯净版 `xxz.json`、`ok海豚665.json`、`Web鱼壳海豚566.json` 等），提取它们的 `lives` 直播源，以 `name + url` 为唯一键去重后，**追加在模板原有直播源（灵鹿直播、家用直播）之后**。所以你**不用手填直播源**，每次运行都会按最新采集结果自动刷新。
- `rules` / `doh` / `flags` / `ijk` / `ads`：来自脚本内嵌的静态模板，可按需修改 `fetch_tvbox.py` 里的 `PYJSON_TEMPLATE_JSON` 常量（改 `lives` / `rules` / `ads` 等都在这里）。

订阅方式：`https://cdn.jsdelivr.net/gh/OWNER/REPO@分支/tvbox/py.json`

> 想**固定**某些直播源不被后续采集覆盖或增减，直接改 `PYJSON_TEMPLATE_JSON` 里的 `lives` 即可；脚本只会把采集到的新源补在后面，已存在的不会重复。若某采集源里的直播源失效，下次采集更新后 py.json 对应条目也会跟着更新。

## Telegram 推送通知（可选）

每次采集结束会推送一条汇总到 Telegram：**成功 / 总数**、失败列表（最多 15 条）、订阅页链接、运行时间。配置步骤如下：

1. 找 [@BotFather](https://t.me/BotFather) 新建机器人，拿到 `HTTP API Token`（形如 `123456789:AAE...`）。
2. 给机器人发任意一条消息，再到 [@userinfobot](https://t.me/userinfobot) 拿到你的 `Chat ID`（纯数字，群聊为负号开头）。
3. 仓库 → **Settings → Secrets → Actions → New repository secret**，新增两个密钥：
   - `TG_BOT_TOKEN` = 第 1 步的 Token
   - `TG_CHAT_ID` = 第 2 步的 Chat ID
4. 推送代码后下次运行自动生效，无需改脚本。

未配置这两个 Secret 时脚本自动跳过推送（正常运行、正常采集），不会报错。推送失败（如 Token 失效）也只记日志、不影响采集结果。

## 调整采集频率

编辑 `.github/workflows/collect.yml` 里的 cron（UTC 时间）：
- 每 6 小时：`0 */6 * * *`（默认）
- 每 3 小时：`0 */3 * * *`
- 每天 02:30：`30 2 * * *`

GitHub 计划任务高峰可能延迟几分钟，属正常。

## 本地测试

```bash
# 解密依赖
pip install pycryptodome

python fetch_tvbox.py
```

结果写入本地 `tvbox/`，`logs/collect.log` 有每次运行明细（含 `OK` / `FAIL` / `SKIP`）。

## 注意事项

- 单个地址失败不会中断，日志记 `FAIL`，其余照常采集。
- 站点有 WAF / 限流，`lytvs.top` 这类站对同一地址会时而 200、时而 503/404、时而 TLS 异常。重试能扛偶发错误，但**扛不住硬封 IP**——若 GitHub Actions 长期 0 文件，请改用本地 / VPS 定时运行，或在仓库 `Settings → Secrets → Actions` 加 `HTTPS_PROXY` 走代理。
- 仓库会随时间累积提交，如需瘦身可开 GitHub 的 "Delete workflow runs"。
