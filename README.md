# Emby Poster Worker

给 Emby 的海报叠加**画质徽章**（4K / HDR / Dolby Vision / Atmos / 编码）和**三源评分栏**（豆瓣 / IMDb / 烂番茄），写回为该条目的封面。跑一次，整库海报统一成 2:3 的成品样式。

> **它会覆盖你 Emby 里的 Primary 海报。** 首次部署请先用 `DRY_RUN=true` 跑一遍，确认效果后再关闭。原图会自动缓存，控制台可逐条恢复。

## 效果预览

**渲染后的海报**——顶部画质徽章，底部三源评分条：

<p align="center">
  <img src="docs/screenshots/poster-sample.jpg" width="300" alt="渲染后的海报：4K WEB-DL 徽章，豆瓣 9.1 / IMDb 8.6">
</p>

**同一个条目，处理前后**——原图先缓存再叠加，重绘永远基于干净原图，不会越叠越厚：

<p align="center">
  <img src="docs/screenshots/poster-before.jpg" width="250" alt="原始海报">
  <img src="docs/screenshots/poster-sample.jpg" width="250" alt="渲染后：徽章与评分就位">
</p>

**电影、剧集、动画、综艺一律适用**——徽章贴在海报顶部留白处，不压标题、不挡人脸；缺失的评分按显示方案自动收栏：

<p align="center">
  <img src="docs/screenshots/poster-cliff.jpg" width="185" alt="谍战剧：4K WEB-DL，两源">
  <img src="docs/screenshots/poster-anime.jpg" width="185" alt="动画：4K WEB-DL，两源">
  <img src="docs/screenshots/poster-drama.jpg" width="185" alt="都市剧：4K WEB-DL，两源">
  <img src="docs/screenshots/poster-kdrama.jpg" width="185" alt="韩剧：1080P，两源">
  <img src="docs/screenshots/poster-variety.jpg" width="185" alt="综艺：4K，两源">
</p>

**内置管理控制台**——纯静态页面，由 Worker 直接托管，不需要 Node、不需要数据库：

| 概览 | 影视库 | 海报样式 |
| --- | --- | --- |
| ![控制台概览](docs/screenshots/console-overview.png) | ![影视库管理](docs/screenshots/console-library.png) | ![海报样式](docs/screenshots/console-settings.png) |

## 快速开始

### 第 1 步：准备三个值

打开 Emby 网页端 → **设置 → 高级 → API 密钥** → 点「添加」新建一个 Key，复制下来。再把这三个值记下来：

| 要什么 | 长什么样 | 在哪拿 |
| --- | --- | --- |
| Emby 地址 | `http://192.168.1.10:8096` | 你平时访问 Emby 用的网址 |
| Emby API Key | 一串字母数字 | 上面新建的那个 |
| Worker 地址 | `http://192.168.1.10:8098` | 你准备部署 Worker 的那台机器的 IP，端口默认 8098 |

### 第 2 步：下载、填写、启动

```bash
git clone https://github.com/blackhumor66666/emby-poster-worker.git
cd emby-poster-worker
cp .env.example .env
```

用任意编辑器打开 `.env`，**只需要改文件开头的三行**，把第 1 步的值填进去，其他都不用动：

```env
EMBY_URL=http://192.168.1.10:8096
EMBY_API_KEY=粘贴你的Key
PUBLIC_BASE_URL=http://192.168.1.10:8098
```

> `PUBLIC_BASE_URL` 必须是 **Emby 能访问到**的地址。Emby 和 Worker 装在同一台机器就填那台机器的内网 IP，别填 `localhost`。

然后一条命令启动：

```bash
docker compose up -d --build
```

不想本地构建？把 `docker-compose.yml` 里的 `build: .` 换成 `image: ghcr.io/blackhumor66666/emby-poster-worker:latest`，再去掉 `--build` 即可。

### 第 3 步：先看效果（这一步不会动你的库）

把 `.env` 里的 `DRY_RUN=false` 改成 `DRY_RUN=true`，保存后再执行一次 `docker compose up -d`。

等一两分钟，打开项目目录下的 `cache/rendered/` 文件夹——里面就是渲染好的成品海报，直接点开检查：徽章位置对不对、评分全不全、有没有遮挡。

> 只想先试一部片？在 `.env` 里加一行 `ITEM_IDS=条目ID`（条目 ID 在 Emby 网页端该影片的地址栏里）。

### 第 4 步：满意后正式生效

把 `DRY_RUN` 改回 `false`，再来一次：

```bash
docker compose up -d
```

Worker 每小时扫一次库：新片、换了片源、升级了渲染规则的条目自动重绘，其余跳过。之后基本不用管它。

管理控制台在 `http://Worker地址:8098`。初始密码在容器日志里：`docker compose logs | grep -i bootstrap`，登录后记得改。

## 功能亮点

| 亮点 | 说明 |
| --- | --- |
| **画质徽章** | 分辨率、WEB-DL/BluRay/Remux 片源、HDR10 / Dolby Vision、Dolby / DTS-HD 音频、视频编码，全部从媒体流真实信息生成。剧集取全集最高画质 |
| **三源评分** | 豆瓣 + IMDb + 烂番茄。缺失的来源自动收栏，不留空槽；也可固定三栏用 `--` 占位 |
| **豆瓣分带身份校验** | 逐条核对标题 / 年份 / IMDb ID 一致才采信，不会把别的片的分贴上来；上游刷新期间评分短暂消失时连续确认 3 次才清除 |
| **永不叠加** | 原图永久缓存，重绘永远基于干净原图；控制台可对任何条目一键恢复原图 |
| **试运行模式** | `DRY_RUN=true` 完整渲染但不写 Emby、不保存状态，验收完关掉即可全库生效 |
| **内置控制台** | 概览统计、全库搜索、按问题类型筛选、批量重绘/恢复、评分覆盖、IMDb ID 绑定、样式实时预览。无 Node、无数据库 |
| **只处理你想处理的库** | 勾选指定影视库，其余库完全不读取、不渲染、不计失败；库 ID 变化时自动降级并告警，避免整库停摆 |
| **省心的自动化** | 每小时扫描新片、Webhook 入库即时渲染、原图定时打包备份、扫描失败推送告警、合集缺封面自动补齐 |
| **单容器交付** | 一个 Python 进程 + 两个依赖（Pillow、requests），无数据库、无外部服务 |

**刻意不做的事**：不改标题/简介/分类等元数据（除你手动确认的 IMDb、豆瓣 ID 绑定）；不抓豆瓣、不内置评分账号、不绕过任何数据源配额；不做「按客户端显示不同海报」——同一张图会被所有客户端复用，这是刻意取舍。

## 豆瓣评分来自神医助手 Pro

Worker **不抓取豆瓣**。豆瓣评分读取的是 [神医助手 Pro](https://github.com/sjtuross/StrmAssistant)（StrmAssistant）在本地产出的评分缓存，Worker 逐条做身份校验后使用。

- **神医助手是干什么的**：一个 Emby 元数据增强插件，负责把豆瓣的评分、ID、简介等刮削进 Emby，并在本机生成评分缓存。没有它，Emby 里就没有豆瓣分，Worker 也无从读取。
- **两个工具怎么配合**：神医助手负责「把豆瓣数据带进 Emby」，Worker 负责「把这些数据画到海报上」。Worker 只读神医助手的缓存文件，两者互不干扰。
- **没有神医助手也能用**：IMDb / 烂番茄评分走 Emby 字段 + MDBList / OMDb，不受影响；只是豆瓣列会显示 `--`。

相关地址：

| 资源 | 地址 |
| --- | --- |
| 插件主页（社区版） | <https://github.com/sjtuross/StrmAssistant> |
| Pro 版下载 | <https://github.com/sjtuross/StrmAssistant.Releases/releases> |
| 使用 Wiki | <https://github.com/sjtuross/StrmAssistant/wiki> |
| Pro 版功能说明 | <https://github.com/sjtuross/StrmAssistant/wiki/PRO版的功能> |
| Pro 授权获取 | <https://t.me/strmassistant/71> |

> 神医助手从 v2.0.0.0 起进入 Pro 授权模式，与本 Worker 无关；Worker 读取的是它产出的本地缓存文件，不涉及它的授权流程。配置好神医助手并跑完「豆瓣元数据」任务后，把它的数据目录以只读方式挂给 Worker（`DOUBAN_CACHE_HOST_PATH`，见 `.env.example` 注释）即可。

## 常用配置

| 变量 | 作用 |
| --- | --- |
| `DRY_RUN` | 试运行，只渲染不写回 |
| `ITEM_IDS` | 只处理指定条目，逗号分隔 |
| `LIBRARY_IDS` | 只处理指定影视库 |
| `RATING_DISPLAY_STRATEGY` | `compact` 自动收栏 / `fixed` 固定三栏 / `quality_only` 只留徽章 |
| `IMAGE_DELIVERY_MODE` | `url` 让 Emby 引用 Worker 图片（推荐）/ `upload` 直传 Emby / `dual` 两者都做 |
| `BACKUP_ENABLED` | 原图定时打包备份到独立卷 |
| `WEBHOOK_TOKEN` | 启用 Emby Webhook 即时渲染 |
| `ALERT_WEBHOOK_URL` | 扫描失败时推送告警 |

完整清单和逐项注释见 [`.env.example`](.env.example)。代理默认不需要；若必须走代理，记得把 Emby 地址加入 `NO_PROXY`。

## 常见问题

**发布新版本后，已部署的实例会自动更新吗？**
不会。Docker 镜像不会自动升级，这是刻意的——绘制规则变化可能触发全库重绘。更新用 `docker compose pull && docker compose up -d`；升级前建议看一眼 [CHANGELOG.md](CHANGELOG.md) 里 `RENDER_VERSION` 有没有变。

**发布者需要一直开着服务器吗？**
不需要。镜像由 GitHub Actions 构建并托管在 ghcr.io，使用者直接 pull，不依赖发布者的任何机器。

**豆瓣列全是 `--`？**
说明没有配置神医助手的豆瓣缓存，见上面「豆瓣评分来自神医助手 Pro」一节。

**想恢复某张海报？**
控制台 → 影视库 → 选中条目 → 恢复原图。原图在 `cache/original/`。

**控制台初始密码？**
`docker compose logs | grep -i bootstrap`，首次登录后请立即修改。

## 开发

`app.py` 是组装根（HTTP 路由、扫描循环、Emby 访问），其余模块可独立导入，`tests/test_module_boundaries.py` 锁定依赖方向：

| 模块 | 职责 |
| --- | --- |
| `badges.py` | 从媒体元数据生成画质徽章 |
| `rendering.py` | 海报合成 |
| `render_queue.py` | 持久化单条队列（与整库扫描串行） |
| `douban_source.py` | 带身份校验的豆瓣缓存读取 |
| `rating_policy.py` / `rating_review.py` / `media_match.py` | 评分准确性守卫 |
| `storage.py` | 原子写入工具 |

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -t . -v
```

CI 在每次 push / PR 执行同一套测试，打 `v*` 标签时构建并推送镜像。改渲染规则请用同一素材做前后 MD5 对比。

## 许可

MIT。安全策略与使用风险见 [SECURITY.md](SECURITY.md)，变更记录见 [CHANGELOG.md](CHANGELOG.md)。
