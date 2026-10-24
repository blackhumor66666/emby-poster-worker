# Emby Poster Worker

给 Emby 的海报叠加**画质徽章**（4K / HDR / Dolby Vision / Atmos / 编码）和**评分栏**（豆瓣 / IMDb / 烂番茄），然后写回为该条目的封面。跑一次之后，整库海报统一成 2:3 的成品样式。

> **它会覆盖你 Emby 里的 Primary 海报。** 首次部署请务必先用 `DRY_RUN=true` 跑一遍，确认渲染效果后再关闭。原图会自动缓存到 `cache/original/`，控制台可以逐条恢复。

当前版本见 [`.env.example`](.env.example) 里的 `APP_VERSION`（变更记录在 [CHANGELOG.md](CHANGELOG.md)）。设计目标是「放在 Emby 旁边的内网 Docker 服务」，默认不假设你能访问公网。

<!-- 建议补充 2~3 张截图，但不要用真实片库：请用几部无关的公开影片做测试库再截。
     推荐：1) 成品海报墙对比 2) 管理控制台首页 3) 单片的评分来源明细 -->

## 效果预览

**渲染后的海报**——顶部是画质徽章（分辨率 / 片源 / HDR / 音频），底部是三源评分条（豆瓣绿标、IMDb 黄标、烂番茄红标）：

<p align="center">
  <img src="docs/screenshots/poster-sample.jpg" width="300" alt="渲染后的海报：4K BLURAY HDR10 DOLBY 徽章，豆瓣 8.8 / IMDb 7.9 / 烂番茄 81%">
</p>

**同一个条目，处理前后**——原图先缓存再叠加，重绘永远基于干净原图，不会越叠越厚：

<p align="center">
  <img src="docs/screenshots/poster-before.jpg" width="250" alt="原始海报">
  <img src="docs/screenshots/poster-sample.jpg" width="250" alt="渲染后：徽章与评分就位">
</p>

**从 4K 原盘到低清网络源都成立**——徽章按媒体流实际信息生成，缺失的评分按显示方案收起，不留空槽：

<p align="center">
  <img src="docs/screenshots/poster-apollo.jpg" width="185" alt="4K BLURAY HDR10 DTS-HD，三源">
  <img src="docs/screenshots/poster-007.jpg" width="185" alt="4K BLURAY">
  <img src="docs/screenshots/poster-2046.jpg" width="185" alt="4K">
  <img src="docs/screenshots/poster-dune.jpg" width="185" alt="720P">
  <img src="docs/screenshots/poster-8mile.jpg" width="185" alt="SD">
</p>

**内置管理控制台**——纯静态页面，由 Worker 直接托管，不需要额外的运行时：

| 概览 | 影视库 | 海报样式 |
| --- | --- | --- |
| ![控制台概览](docs/screenshots/console-overview.png) | ![影视库管理](docs/screenshots/console-library.png) | ![海报样式](docs/screenshots/console-settings.png) |

## Quickstart（5 分钟）

```bash
git clone https://github.com/blackhumor66666/emby-poster-worker.git
cd emby-poster-worker
cp .env.example .env
```

编辑 `.env`，**只有这三项必须填**：

```env
EMBY_URL=http://your-emby-host:8096
EMBY_API_KEY=replace-with-an-emby-api-key
PUBLIC_BASE_URL=http://your-worker-host:8098
```

`PUBLIC_BASE_URL` 必须是 **Emby 容器能访问到** 的 Worker 地址，并且包含实际映射端口（宿主机上暴露的 8098，不是容器内的 8080）。

第一次先试运行，只渲染不写回。把 `.env` 里的 `DRY_RUN` 改成 `true`（想先只看一部片，同时设置 `ITEM_IDS=<Emby 条目ID>`）：

```bash
docker compose up -d --build
docker compose logs -f
```

跑完后打开 `cache/rendered/` 里的 JPG 检查效果。满意后把 `DRY_RUN` 改回 `false`，再 `docker compose up -d` 即可正式写入。

不想本地构建就用预构建镜像（`linux/amd64` + `linux/arm64`）：把 `docker-compose.yml` 里的 `build: .` 换成 `image:` 一行即可。

```yaml
services:
  emby-poster-worker:
    image: ghcr.io/blackhumor66666/emby-poster-worker:latest
    # 其余配置（env_file / ports / volumes）保持不变
```

然后 `docker compose up -d`（不要加 `--build`）。

管理控制台：`http://你的Worker地址:8098`。初始密码在容器日志里：

```bash
docker compose logs | grep -i bootstrap
```

## 前提条件

- **Emby 4.9+**，需要一个有「读取媒体 + 修改图片」权限的 API Key。
- **豆瓣分不会自动就有。** 本工具不抓豆瓣，只读取神医助手 Pro（Shenyi Assistant Pro）在本地产出的缓存。没有这份缓存时，豆瓣列会全部显示 `--`，其余评分（IMDb / 烂番茄 / Emby 自带字段）照常工作。详见下方「给其他 Emby 用户的配置清单」。
- **IMDb / 烂番茄分**优先从 Emby 字段读；缺失时用 MDBList（推荐）或 OMDb 补。两者都需要自己申请 API Key，不填就只是留空。
- Worker 容器要能访问 Emby，**Emby 容器也要能访问 `PUBLIC_BASE_URL`**（默认模式下 Emby 会代理 Worker 的图片地址）。这两条网络不通是最常见的「装好了但海报空白」原因。

## 核心功能

### 海报渲染

- 每小时扫一次 Movie / Series / Season。新条目、媒体信息变化（换分辨率、加 HDR 轨道）、原图被替换、渲染规则升级，都会触发重绘；其余条目直接跳过，不浪费 CPU。
- 输出统一为 2:3（默认 1000×1500），异形海报补边而不是裁切，保证徽章和评分条在固定海报位的客户端上不被切掉。
- 顶部徽章按媒体流信息生成：分辨率、WEB-DL/Remux 等片源、SDR/HDR10/Dolby Vision、视频与音频编码。剧集取全集里的最高画质，HDR / Atmos 有则显示。
- 原始海报永远先缓存到 `cache/original/`，重绘基于缓存原图进行，**重复运行绝不会把徽章叠加两次**；也可以在控制台对任何条目一键恢复原图。

### 评分系统

- **豆瓣**：读取神医助手 Pro（Shenyi Assistant Pro）在本地产出的评分缓存，逐条做身份校验（标题 / 年份 / IMDb ID 一致才采信），上游刷新导致评分短暂消失时连续确认 3 次才清除旧分，避免一次元数据刷新抹掉整库评分。
- **IMDb / 烂番茄**：优先读 Emby 自带字段；缺失时经 MDBList（推荐，一个 Key 查两源）或 OMDb 补齐。多 Key 轮换、单 Key 冷却、请求限速、连续失败熔断、错误结果缓存，全部内置。
- 每个条目的每个来源都会记录：当前值、来源（缓存 / Emby / MDBList / OMDb / 人工覆盖）、缺失原因（无 ID、缓存无分、Key 失效……）、最近核查时间。控制台按原因分类筛选，缺什么一目了然。
- 支持人工覆盖：按条目 ID 或 `片名|年份` 指定评分，写明原因，覆盖值优先于一切自动来源。

### 管理控制台

纯静态 Web UI，由 Worker 直接托管，无 Node、无数据库：

- **概览**：入库范围、缺分、需要人工关注的数量，评分与封面覆盖率，状态拆解（每个条目为什么被计入）。
- **影视库**：全库搜索、按问题类型筛选、批量重绘 / 恢复、单片评分覆盖与 IMDb ID 修正绑定。
- **海报样式**：切换 compact / fixed / quality_only 显示方案，调整徽章与评分栏的位置、大小、间距，实时预览。
- **数据来源**：逐个添加、替换、检测 API Key；连接状态与最近刷新结果可见。
- 登录用 PBKDF2-SHA256 加盐哈希验证；首次启动未设 `ADMIN_PASSWORD` 时会生成随机密码并打印一次到容器日志。

### 自动化与兜底

- **范围限制**：勾选指定影视库后其余库完全不读取、不渲染、不计失败；库被重建导致 ID 变化时自动降级为不限制并告警，避免整库停摆。
- **Webhook 即时渲染**：Emby 入库时推一条 Webhook，单片立刻进队列渲染，不必等下一轮小时扫描（需要设置 `WEBHOOK_TOKEN`）。
- **原图备份**：可选把 `cache/original/` 定时打包到另一个卷（`BACKUP_*` 系列），原图是唯一无法重建的数据。
- **失败告警**：扫描失败数超阈值时 POST 到企业微信 / 钉钉 / Slack 机器人（`ALERT_WEBHOOK_URL`）。
- **合集封面补齐**：Emby 自动合集经常没有封面，Worker 在扫描开头触发图片刷新，TMDB 确实没有合集海报的复用成员封面，7 天重试。
- **诊断报告**：`/health`、每次扫描的完整报告、待人工复核清单、媒体健康报告（缺流、文件名与实际画质冲突、HDR 冲突、字幕语言）。

### 刻意不做的事

- 不改标题、简介、分类等元数据（唯一例外：你手动确认的 IMDb / 豆瓣 ID 绑定）。
- 不抓豆瓣、不内置任何评分账号；不绕过任何数据源的配额或 ToS。
- 不做「按客户端显示不同海报」——同一张 Primary 图会被所有客户端复用，这是刻意取舍，原因见下方「Display boundary and client strategy」。

## 文档导航

| 想了解 | 看这里 |
| --- | --- |
| 具体渲染了什么、评分怎么取 | [What it does](#what-it-does) 与 [Rating automation](#rating-automation) |
| 每个环境变量的含义 | [`.env.example`](.env.example)（带注释，最全） |
| 怎么配置、先测什么 | [给其他 Emby 用户的配置清单](#给其他-emby-用户的配置清单) |
| 部署细节、代理、NAS | [Deploy](#deploy) |
| 代码结构 | [Module layout](#module-layout) |
| 版本变化 | [CHANGELOG.md](CHANGELOG.md) |
| 安全与使用风险 | [SECURITY.md](SECURITY.md) |

## What it does

- Keeps an untouched copy of each original poster in the cache, so reruns do not stack overlays.
- Normalizes generated posters to the common 2:3 canvas; extra space is padded instead of cropping quality badges or rating bars on clients with fixed poster slots.
- Stores rating provenance and missing-data reasons per item, and logs aggregate rating results at the end of each scan.
- Supports optional per-item rating overrides in `RATING_OVERRIDES_FILE`; see `rating-overrides.example.json`.
- Caches paginated series media summaries, detects Worker-rendered images before using them as originals, and writes a marker into generated JPEGs.
- Produces a consistent 1000x1500 poster by default while preserving the full artwork on unusually tall or wide source posters.
- Uses Asia/Shanghai timestamps by default, suppresses routine poster HTTP noise, emits periodic scan progress, and caps Docker JSON logs at 20 MB × 3 files.
- Writes atomic state/cache/render files, exposes `/health`, and writes scan and review reports. The console checks configured API keys when their status is first viewed and shows the last IMDb/Rotten Tomatoes refresh result, current source coverage and MDBList batch counts. A changed rating-provider key pool triggers one batched refresh so stored OMDb results can migrate to MDBList without waiting for the normal weekly cycle. Orphan cache reporting is enabled by default; deletion requires `CACHE_GC_ENABLED=true`.
- Limits OMDb request frequency, retries transient failures with backoff, opens a circuit after repeated upstream failures, and skips title search for configurable concert/music genres when no IMDb ID exists.
- Reads resolution, SDR/HDR/Dolby Vision, video codec, audio codec and source hints from Emby's media metadata and file name.
- For a series poster, summarizes episode streams: the highest available resolution is shown, and HDR/Dolby Vision/Atmos appear when present in the series episodes.
- Uses an identity-checked Shenyi Assistant Douban cache as the authoritative Douban source. Generic Emby `CommunityRating` is not treated as verified Douban by default; compatibility fallback is explicit only. Rating identity changes force a full re-resolution.
- Reads IMDb and Rotten Tomatoes ratings from Emby fields first, batches missing IMDb/Rotten Tomatoes scores through MDBList when configured, and falls back to OMDb. Supports comma-separated key pools with per-key cooldowns and caches rating provenance.
- Uploads the generated JPEG to Emby's Primary image endpoint.
- Scans the full Movie/Series library hourly for new items, changed media, replaced images and stale render versions.
- Restricts production to selected Emby libraries when a scope is configured; unselected libraries are never read, rendered or counted as failures. See 影视库范围 below.
- Refreshes rating sources on independent default intervals: Douban every three days, IMDb and Rotten Tomatoes every seven days. A refresh pass shares one OMDb lookup when needed. Missing ratings remain `--`; an empty result is cached until the next source cycle instead of being retried on every hourly scan.


### 影视库范围（Library scope）

默认不限制：Worker 处理 Emby 里所有 Movie / Series / Season。开启「影视库范围限制」后，只处理勾选的影视库，其余库完全跳过——不读取、不渲染、不产生失败记录。

- 在 **系统设置 → 影视库范围** 勾选需要制作海报的库并保存，改完立即生效，无需重启。
- 新建的影视库默认不勾选，避免像 9KG 这类不需要评分封面的库被自动处理。
- 归属判定以**物理路径**为准。Emby 的 `ParentIds` 参数对部分虚拟库不可靠（会返回不属于该库的条目），所以扫描请求仍带 `ParentIds` 做预筛，但最终以路径匹配收口；限制开启时，解析不出库的条目一律跳过。
- 环境变量 `LIBRARY_IDS=id1,id2` 仍然可用，配了就等同于开启限制，适合无人值守部署。
- 安全阀：勾选的 ID 若与 Emby 现有库完全对不上（例如库被重建导致 ID 变化），限制自动降级为「不限制」并写警告，避免整库停摆。
- 队列任务（Webhook、单条重渲染）同样受范围约束，被跳过的条目返回 `ignored`，不再计为失败。

### MDBList and OMDb rating fallback

The worker uses this order for missing IMDb/Rotten Tomatoes values:

1. Manual item override.
2. Emby `CriticRating` for Rotten Tomatoes only when `CRITIC_RATING_IS_RT=true` and the field is known to be populated by the configured upstream. Generic `CommunityRating` is not verified Douban.
3. MDBList batch lookup by IMDb ID.
4. OMDb lookup by IMDb ID.

Configure keys individually from the console **数据来源** page. Each key can be added, replaced, tested or removed independently; the worker rotates only on authentication or rate-limit failures, applies per-key cooldowns, and caches results under `cache/mdblist/` and `cache/omdb/`. Environment pools (`MDBLIST_API_KEYS`, `OMDB_API_KEYS`) remain available for unattended/bootstrap deployments. Multiple keys belonging to the same MDBList account share that account's quota; they are for resilience, not for bypassing provider limits.

```env
MDBLIST_API_KEYS=
OMDB_API_KEYS=
```

MDBList regular accounts are queried in batches of ten IMDb IDs, so a full scan does not issue one request per title.

### 豆瓣评分自动核对与可信度

默认读取 `/data/douban`（由 Compose 以只读方式挂载）。Worker 支持神医助手官方 `cache/<douban_id>_<imdb_id>/all.json` 布局，也兼容旧的分类目录；评分必须通过 JSON/目录身份核对，IMDb ID 只用于唯一定位或冲突校验。此来源优先于通用的 `CommunityRating`。每次整库扫描开始时生成 `cache/reports/douban-audit.json`；控制台 **数据来源 → 豆瓣评分自动审计** 显示已核对数、与 Emby 分数冲突数、无匹配缓存数及缓存超过阈值的数量。报告保留逐条原因；这不是联网实时评分验证。

若更重视准确性，可设置 `DOUBAN_REQUIRE_VERIFIED_CACHE=true`：没有可信的 ID 匹配缓存时，海报**不显示豆瓣分**，即使 Emby 存在 `CommunityRating`；IMDb/RT 不受影响。默认严格校验，不把未核实的 CommunityRating 当作豆瓣分。`DOUBAN_CACHE_STALE_DAYS=90` 仅标记旧缓存供审计，不会自行改动或猜测评分；请由上游神医助手定期刷新缓存。

## 评分显示方案（Rating display strategy）

三种全局预设，改完在下一轮扫描生效：

- Quality badges remain overlaid on the poster artwork with the existing compact chips, HDR colors and Dolby Vision/Atmos assets.
- Ratings remain in the existing translucent bottom overlay with the green Douban mark, yellow IMDb mark, red RT tomato mark, typography and fixed three-slot visual language.
- `RATING_DISPLAY_STRATEGY=compact` omits unavailable sources when the strategy calls for a one- or two-source layout; the visual treatment itself does not change.
- In compact one- and two-source cases, the rating groups are left-anchored. The two-source case therefore leaves more room on the right for VidHub, 网易爆米花 and similar players that paint an unavoidable rating overlay.
- Set `RATING_DISPLAY_STRATEGY=fixed` to retain three visible source slots with `--` placeholders exactly like the current library view. Use `RATING_DISPLAY_STRATEGY=quality_only` (aliases: `none`, `clean`) to keep only the top quality badges and omit the rating footer. This is a global poster preset, not a client switch.

For side-by-side review, render several real cached library posters in one-, two- and three-source cases:

```bash
python make_v18_previews.py
```

The generated review sheets are written under `cache/previews/v18-layouts/` and are local-only artifacts.

## Rating automation

The worker reads an identity-checked Shenyi Assistant Pro Douban cache and does not scrape Douban directly. A unique official cache match by current IMDb ID can recover a missing Douban ID; missing or conflicting identity remains untrusted.

The Shenyi Assistant UI action named `全库应用豆瓣元数据（无需刷新元数据）` is not exposed as a separate standard Emby scheduled task on this server. The worker therefore does not simulate that UI action. If a plugin update exposes an API or scheduled task for the apply step, it can be connected safely; until then, the worker only treats ratings present in Emby as authoritative.

The hourly scan is only for detecting new or changed media and stale posters. Rating lookups are throttled by the per-source refresh settings, so the worker does not repeatedly query external rating sources.

## 给其他 Emby 用户的配置清单

**神医助手 Pro 是推荐的上游元数据提供者，但仅安装插件还不等于评分已经写入 Emby。** 第一次部署前请先抽查几部电影和剧集：`CommunityRating` 是否为豆瓣分、`CriticRating` 是否为烂番茄影评人分、`ProviderIds.Imdb` 是否正确。Worker 不会调用神医助手的任务 API，也不会把任意 Emby 社区分自动识别成豆瓣；若上游写入的是 TMDB/IMDb 等评分，在控制台关闭“CommunityRating 当作豆瓣”。豆瓣 Access Token 和 MDBList Key 属于神医助手自己的可选设置，不会自动传递给 Worker；Worker 的 MDBList/OMDb Key 请在“数据来源”页分别添加。

最少需要配置：

1. Emby 地址和具有读取媒体/更改海报权限的 API Key；在“数据来源”页逐个添加并检测 Key。
2. `PUBLIC_BASE_URL`：**Emby 容器能够访问到的** Worker 地址，必须包含实际映射端口；浏览器能打开并不代表 Emby 容器也能访问。可用 `IMAGE_DELIVERY_MODE=upload` 避免依赖 Worker URL，但仍需配置 Emby 连接。
3. 持久化的 `CACHE_HOST_PATH` 和 `CONFIG_HOST_PATH`（NAS Compose）或本地 `./cache`、`./config`；前者保存原海报/评分缓存，后者保存管理员凭据。确保目录不公开且可写。
豆瓣缓存目录通常是神医助手 Pro 的 `metadata_root` 目录（其中包含 `cache/` 和 `override/` 子目录）。Compose 默认把它以只读方式挂到 `/data/douban`，Worker 从 `/data/douban/cache` 读取评分文件。用 `DOUBAN_CACHE_HOST_PATH` 指向你自己的宿主机绝对路径；没有这份缓存时保持默认的空 `./douban-cache` 即可——不会报错，但豆瓣列会全部显示 `--`，此时 Worker 只用 IMDb/烂番茄/Emby 自带字段。

4. 如果能把神医助手 Pro 的豆瓣缓存目录以只读方式挂载给 Worker，设置 `DOUBAN_CACHE_DIR` 指向包含神医助手 `cache/` 目录的根目录，Worker 会优先读取对应 `all.json` 的豆瓣评分；找不到经身份校验的本地缓存时不回退到 Emby `CommunityRating`。如需避免把未核实的社区评分标成豆瓣，设置 `DOUBAN_REQUIRE_VERIFIED_CACHE=true`（缺少匹配缓存则隐藏豆瓣分）。神医助手刷新期间若评分暂时消失，Worker 默认连续 `DOUBAN_MISSING_CONFIRMATIONS=3` 次确认后才清除旧评分，避免一次元数据刷新抹掉整库评分。建议先选少量 `ITEM_IDS` 验证海报、评分来源及恢复原图流程，再整库运行。

**代理不是默认必需项。** 在 Worker 容器内能够直连 Emby 和评分服务时，保持 `HTTP_PROXY`、`HTTPS_PROXY` 为空。若网络必须通过代理访问 OMDb，配置这两个变量，并把 Emby 的地址/主机名加入 `NO_PROXY`，避免 LAN 流量绕远路。MDBList 默认直连，因为某些代理会让 MDBList 超时；仅当直连 MDBList 不通且代理可用时，设置 `MDBLIST_USE_PROXY=true`。在管理页面逐个检测 Key：网络错误和 Key 无效是不同问题，先排查 DNS/网络/代理再更换 Key。多 Key 不能绕过同一 MDBList 账户的共享配额。

共享前应确保仓库中没有 `.env`、`config/`、`cache/`、API Key、媒体路径、内部域名或真实海报；不要把截图中的 Key 当作示例。部署时使用模板并为自己的环境重新生成密钥。Worker 会替换 Emby Primary 海报，先在测试库试运行，并保留原图备份与回滚方案。

## Module layout

`app.py` is the composition root (HTTP routes, scan loop, Emby access). Leaf
modules stay importable on their own — `tests/test_module_boundaries.py` locks
the direction so a circular import cannot sneak in:

| Module | Responsibility |
| --- | --- |
| `badges.py` | Badge rules from media metadata (`quality_badges(item, settings)`) |
| `rendering.py` | Poster compositing; everything environmental arrives via `RenderContext` |
| `storage.py` | Atomic JSON/text helpers |
| `render_queue.py` | Durable single-item queue (serialises with full scans) |
| `douban_source.py` | Identity-checked read of the Shenyi Douban cache |
| `rating_policy.py` / `rating_review.py` / `media_match.py` | Rating accuracy guards |

Rendering is compared byte-for-byte: render the same item before and after a
refactor and diff the MD5, so an unintended pixel shift is caught before deploy.

## Deploy

Copy `.env.example` to `.env` and fill in your own values. The template is
grouped and commented and is the authoritative list of variables; only these
three are required to start:

```env
EMBY_URL=http://your-emby-host:8096
EMBY_API_KEY=replace-with-an-emby-api-key
PUBLIC_BASE_URL=http://your-worker-host:8098
```

Before the first real run, set `DRY_RUN=true` and inspect `cache/rendered/`.

For a first test, set `ITEM_IDS` to one Emby item ID. Leave it empty to scan the selected libraries. Create an Emby API key in Dashboard -> Advanced -> API Keys. Keep `.env` private and never commit it.

Build and start it from this directory:

```bash
docker compose up -d --build
```

Start with one test item, verify the poster in Emby, VidHub and Infuse, then remove `ITEM_IDS` to process the library. Keep the cache volume persistent. If Emby refreshes metadata and replaces manually uploaded images, disable image replacement for these libraries or run this worker after refreshes.

With `REQUIRE_ALL_RATINGS=false`, all items are published with the three fixed rating slots. A missing value is shown as `--`; it is replaced automatically on a later scan when the source becomes available.

V1.5 writes `rating_details` and per-source `rating_checked_at` timestamps into each state file. V1.8 adds `series-media/` cache files and a JPEG marker so a generated Primary image is not silently treated as a clean original when the original cache is missing. Status values include `ok`, `no_rating`, `no_match`, `no_api_key`, `rate_limited`, `invalid_api_key`, `lookup_error`, and `override`. This separates a genuine missing score from an external lookup problem. The optional override file accepts an Emby item id or `Name|ProductionYear` as the key and may provide `douban`, `imdb_id`, `imdb`, `rt`, and `reason` fields. Overrides take precedence over automatic values and are included in the render signature.

For series, the worker aggregates paginated episode media sources and caches the summary for seven days by default. It shows the highest available resolution and any HDR/Dolby Vision/Atmos capability; mixed-quality series should use season-level posters for exact labels.

The container publishes port 8098. `/health` returns the current scan state and `/data/cache/reports/last-scan.json` contains the last completed scan summary, while `/data/cache/reports/review-needed.json` lists items requiring manual review. `CACHE_GC_ENABLED=false` is intentionally conservative: the worker reports orphaned item caches but does not delete them until explicitly enabled. The application log timezone defaults to `Asia/Shanghai` (Beijing time); set `LOG_TIMEZONE` if another zone is needed. Routine poster HTTP 200 requests are hidden by default, while scan progress and non-OK rating diagnostics remain visible. Docker log rotation is configured to 20 MB per file with three retained files in the supplied Compose files. Emby must be able to reach `PUBLIC_BASE_URL`; if the NAS uses another LAN address or port, change that value. The worker first sets Emby's Primary image URL to its generated image and only falls back to Emby's base64 image upload endpoint if that fails. This is important for libraries whose `poster.jpg` lives on a read-only 115 mount.


## Open-source deployment

The repository contains only generic templates. Never publish `.env`, `cache/`,
`config/`, rendered posters, API keys, NAS paths, proxy addresses or private
domain names — see [SECURITY.md](SECURITY.md).

- `docker-compose.yml` — 通用部署，用相对路径挂载。
- `docker-compose.nas.yml` — 群晖 / 威联通风格，用 `CACHE_HOST_PATH`、`CONFIG_HOST_PATH`、`DOUBAN_CACHE_HOST_PATH` 指定宿主机绝对路径。
- 预构建镜像：`ghcr.io/blackhumor66666/emby-poster-worker`（`main` 分支推送即构建 `linux/amd64` 与 `linux/arm64`）。

改完代码跑一遍测试：

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -t . -v
```

CI 在每次 push / PR 执行同样的命令（`.github/workflows/tests.yml`），打 `v*` 标签时构建并推送镜像（`.github/workflows/docker.yml`）。


## Display boundary and client strategy

The JPEG uploaded to Emby contains only the source poster, quality badges and (when enabled) the rating footer. It does **not** contain the title, item id or the Python-style rating dictionary shown under the posters in the V1.8 review screenshot. That line is review-sheet metadata generated by `make_v18_previews.py`; it is never written into the poster JPEG. Review annotations are now hidden by default; set `V18_PREVIEW_ANNOTATIONS=true` only when a labeled contact sheet is useful.

A per-client display configuration is intentionally not enabled in the V1.8 deployment. The worker assigns one Emby Primary image to each item, and Emby/client caches may reuse that same image across Web, TV, mobile and third-party clients. A User-Agent-based variant endpoint could technically return different files, but it would make cache invalidation, image replacement and debugging unreliable; a client can also request an image through Emby's proxy without preserving the original request context. The practical result would be “sometimes the wrong variant appears”, which is worse than a consistent poster.

The safe approach is one universal image plus a global preset:

- `compact`: current V1.8 look; missing ratings are omitted and one/two-source groups stay left-aligned.
- `fixed`: compatibility look with three rating slots and `--` placeholders.
- `quality_only`: no rating footer, only quality badges; useful when a client already displays ratings or when a clean poster is preferred.

## 媒体健康报告与图片下发方式

两项面向生产的细节：

- **Media health report**: every completed scan writes `MEDIA_HEALTH_REPORT_FILE` (default `/data/cache/reports/media-health.json`). It records missing Primary/video/audio streams, filename-vs-stream quality conflicts, HDR conflicts and subtitle-language findings. It is a diagnostic report only and never blocks poster rendering.
- **Image delivery mode**: `IMAGE_DELIVERY_MODE=url` keeps the current URL-first behavior; `upload` stores the JPEG inside Emby; `dual` uploads first and then attempts to set the Worker URL as Primary. The current NAS deployment remains on `url` to avoid an unnecessary full-library upload, but `upload` is available as a resilience fallback.

The rating footer is source-driven in `compact` mode. In the two-source layout, the IMDb group is intentionally pulled slightly left from the right edge so VidHub/网易爆米花 default badges are less likely to cover it, while the placement remains stable for clients such as Infuse. If RT is unavailable, the RT mark is omitted from the generated JPEG rather than rendered as an empty slot. If a client still shows an old empty RT slot, refresh that client's image cache or force a metadata/image refresh; the current Emby Primary image for the checked series was byte-identical to the Worker render and contained only the Douban group.

## 设计取舍与后续方向

后续改动应该以「在所有客户端上让海报本身更好看」为标准，而不是增加脆弱的客户端探测：

1. **Smart badge priority** — keep resolution + HDR/Dolby/Atmos, but collapse low-value chips first on narrow poster slots so the artwork remains clean.
2. **Content-aware presets** — use a separate restrained preset for series, concerts and documentaries instead of exposing every badge on every title.
3. **Collection identity** — optional small, consistent collection/season markers; useful for franchises without turning the poster into a dashboard.
4. **Data confidence and review queue** — distinguish confirmed ratings from missing or guessed ratings and surface only items needing manual review.
5. **Safe preview matrix** — compare the same posters at poster ratios and common thumbnail sizes before changing the artwork rules.
6. **Rollback-ready rendering** — keep originals and render signatures, so any visual experiment can be reversed without re-downloading posters.

The current V1.8 deployment keeps the safe part of this plan: no client-specific image variants, an explicit clean/quality-only preset, and a guard for items where all rating sources are missing so compact rendering never creates an invalid zero-column layout.

## 数据准确性诊断

管理控制台“概览”页会把评分完整度和数据一致性分开显示，并按严重程度列出可执行诊断：

- **豆瓣评分未同步**：已核实的豆瓣缓存与海报值不一致，优先让神医助手应用豆瓣元数据后重新扫描。
- **OMDb API 无效/暂不可用**：OMDb 返回无效 Key、网络错误或限流；Worker 不会用 TMDB、豆瓣或其他评分冒充 IMDb/RT。
- **评分缺失**：来源没有评分时保持空值，不生成猜测数据。
- **评分海报缺失/媒体资源异常**：可直接跳转到影视库问题列表。

每个条目同时保存评分来源、身份和状态，概览页的“豆瓣评分已核对”只统计当前使用已核实缓存的资源。扫描完成后，诊断会在下一次概览刷新时立即显示；当新缓存出现、身份发生变化或兼容回退模式中的 CommunityRating 发生变化时，才会触发对应的提前刷新。

## 管理控制台

Worker 内置一个无需 Node 运行时的管理控制台，启动后访问 `HTTP_PORT` 对外映射的地址即可。首次启动会在 `ADMIN_DATA_DIR`（默认 `/data/config`，compose 里固定挂载到 `/data/config`）下创建 `auth.json` 保存管理员凭据；如果没有设置 `ADMIN_PASSWORD`，随机初始密码只会写入容器日志一次，首次登录后必须修改。密码以 PBKDF2-SHA256 加盐哈希存储，不是明文。

控制台支持：

- 概览、评分覆盖率、封面覆盖率和媒体健康统计。
- 影视库搜索、问题筛选、单片评分覆盖、IMDb ID 修正、重绘和恢复原图。
- `compact`、`fixed`、`quality_only` 三种评分显示方案。
- 评分栏与质量徽章的位置、大小和偏移调整。
- Emby 与 OMDb 连接状态、扫描进度、最近扫描报告。
- 管理员密码修改，以及运行时保存 Emby 地址、公开海报地址和扫描策略；API Key 在“数据来源”页逐个管理。

管理配置、管理员认证和评分覆盖都会保存在缓存卷内，不要把 `cache/` 提交到公开仓库。

## 防止任务长时间无响应

后台扫描使用独立线程执行，管理页面不会等待整库扫描完成。扫描状态会持续记录当前资源、已处理数量、最近进度时间和耗时；Emby 请求默认超时为 30 秒，管理接口读取影视库默认超时为 12 秒。可以通过 `EMBY_REQUEST_TIMEOUT`、`ADMIN_API_TIMEOUT` 和 `EMBY_PAGE_SIZE` 调整，但不建议把超时设置过大。影视库读取使用分页，并带有重复分页保护，避免 Emby 异常时无限读取。

如果 Emby 不可用，服务仍会先启动管理页面，并把健康状态标记为 `needs_configuration` 或失败，而不是在启动阶段退出。手动扫描不会重复启动已有扫描任务。

## 常见问题

### 发布新版本后，已部署的实例会自动更新吗？

**不会。** Docker 镜像不会自动升级，这是刻意的——绘制规则变化可能触发全库重绘，自动更新等于把这类变更静默推进你的库。

更新方式：

```bash
docker compose pull && docker compose up -d
```

想自动跟进可以用 Watchtower 之类的工具，但建议先看一眼 [CHANGELOG.md](CHANGELOG.md) 里 `RENDER_VERSION` 有没有变——变了就意味着下一轮扫描会全库重绘。

### 发布者需要一直开着什么服务器吗？

不需要。镜像由 GitHub Actions 在云端构建并托管在 ghcr.io，使用者直接 pull；发布者这边只需要 GitHub 仓库本身。

### 豆瓣列全是 `--`？

正常——说明没有配置神医助手 Pro 的豆瓣缓存，见「给其他 Emby 用户的配置清单」第 3、4 条。IMDb / 烂番茄 / Emby 字段不受影响。

### 想恢复某张海报？

控制台 → 影视库 → 选中条目 → 恢复原图。原图在 `cache/original/`，开启 `BACKUP_ENABLED` 后另有定时打包备份。

### 控制台初始密码是什么？

`docker compose logs | grep -i bootstrap`，首次登录后请立即修改。
