# AstrBot Dota2 助手插件（加强版）

查询 Dota2 玩家战绩、英雄、物品、比赛详情、实时比赛与职业赛事，
把结果渲染成**主题卡片图片**发送，并在卡片底部附上**赛后点评**。

支持自然语言（带"刀塔"）和 `/dota` 斜杠命令两种触发方式。

## 功能

| 查询 | 说明 | 示例 |
|------|------|------|
| 玩家资料 + 近期战绩 | 段位、胜率、近期 KDA | `查一下 Miracle 的刀塔战绩` |
| 英雄数据 | 属性、定位、胜率 | `/dota hero 灰烬之灵` |
| 物品信息 | 价格、属性、合成 | `/dota item 黑皇杖` |
| **单场比赛详情** | 双方阵容、KDA、经济、出装 + **录像解析赛后总结** | `/dota match 8831125663` |
| 实时比赛 | 正在进行的对局 | `/dota live` |
| 职业赛事 | 近期职业比赛 | `/dota pro` |
| 英雄出装 | 推荐出装与加点 | `火猫怎么出装` |

绑定 Steam ID 后可直接说"查一下我的刀塔战绩"：

```
/dota bind 899428504          # 32-bit 或 64-bit Steam ID
/dota unbind
```

**唤醒规则**：自然语言触发必须带上「刀塔」「刀塔比赛」或「dota」，
例如「查询我的最近一场刀塔比赛」；只说「查一下我的战绩」唤不醒插件。

## 卡片与赛后总结

- 查询结果渲染成卡片图片，**点评固定在图片最底部**（不是另一条消息）。
- **具体某一局**写**全面复盘**（见下）；其余查询写一段对应点评。
- 总结**用英雄名指代玩家**（不写昵称/账号 ID）。

### 单局复盘：来自 OpenDota 录像解析

查询**具体某一局**时，插件向 OpenDota 申请**录像解析**，用解析出的动态数据，
按**前期 / 中期 / 后期**的时间线写一篇复盘，覆盖四个栏目：

| 栏目 | 内容 |
|------|------|
| **比赛进程** | 局势变化、经济/经验曲线拐点、关键物品成型时点、视野控制 |
| **团战与关键目标** | 团战得失、推塔/兵营/肉山/不朽盾的归属与节奏 |
| **优势与失误** | 双方各自做对了什么、哪里没做好 |
| **亮眼与糟糕表现** | 点名本场最亮眼与最糟糕的 1-2 人（结合百分位/KDA） |

- 已解析的比赛：同步出图，无需等待。
- 未解析的比赛：先发一条"录像正在解析中"，后台解析完成后把对战数据与点评
  **合成同一张卡**补发（解析约需数分钟，超时则只发对战数据卡）。
- 可用 `enable_replay_summary` 关闭、退回仅用卡片静态数据写总结的旧逻辑。

## 图片输出

默认返回图片卡片。配置页可关掉「图片回复」（`enable_image_output`），
改为发送 Markdown 纯文本（数据可复制、可搜索）。
`/dota` 命令可用 `--text` / `--image` 临时覆盖单次输出。

### 渲染方式

- **本地 Pillow 渲染（默认，推荐）**：约 2 秒出图、样式稳定，
  图标与英雄立绘由插件缓存到本地后绘制。
- **网络 t2i（可选兜底）**：仅在本地渲染失败时尝试。
  实测该服务不稳定（超时/单端点 58 秒慢），**不建议作为主路径** ——
  保持 `prefer_local_render` 开启即可跳过它。
- 两条路径版式一致；都失败时回退为纯文本。

### 卡片为什么一定能发出去

卡片由发送前的钩子（`on_decorating_result`）输出。钩子若长时间同步等待
（先调模型写点评、再渲染出图），一旦超时本轮事件就被提前关闭，
此时 `set_result` 会**静默失效** —— 图片明明渲染好了却发不出去。

因此插件把这段等待限制在 `card_hook_budget`（默认 8 秒）：

- **窗口内完成** → 照旧作为本轮回复发出（单条消息，体验最好）；
- **超时** → 立刻让出钩子，改由后台任务用 `send_message` **主动推送**。
  后者不依赖本轮事件的生命周期，事件被关闭也照样送达。

实测 GLM-4-Flash 写一段点评只要 2~3 秒，所以正常情况都走第一条路径；
只有模型明显变慢时才转后台推送，卡片的送达不再依赖钩子的运气。

### 卡片主题

配置页下拉框四选一（`card_theme_preset`），不开放逐色自定义：

| 选项 | 风格 |
|------|------|
| `light` | **蓝白**（默认）：白底 + 蓝强调 |
| `dark-gold` | **深色暗金**：近黑暖调底 + 暗金强调 |
| `pink` | **手绘可爱风**：粉底 + 玫红强调，圆润可爱字体 + 手绘圆角边框 / 波点底纹 / 四角爱心星星 |
| `navy-gold` | **深蓝暗金**：深蓝底 `#0a1942` + 暗金标题、正文白色 |

选定后**所有卡片统一生效**：单场战报卡、玩家战绩、英雄数据、物品、实时比赛、
职业赛事用的是同一套令牌（底色/面板/正文/强调色/点评区块），不会出现
「只有单场卡换肤、其它查询还是白底黑字」的割裂。

### 英雄名称

卡片英雄名统一为 Valve 官方中文译名（`Ember Spirit` → `灰烬之灵`）。
查询时官方中文名、俗称（火猫、水人）、英文名、内部名均可命中。

### 字体

pink（手绘可爱风）主题内置 **站酷快乐体（ZCOOL KuaiLe）**
（`assets/fonts/zcool-kuaile.ttf`，SIL Open Font License 1.1，可免费商用）。
缺字时自动回退到系统中文字体。其余主题使用系统字体，不额外打包。

## 安装

1. 将插件目录放入 AstrBot 的 `data/plugins/`
2. 安装依赖：`pip install -r requirements.txt`
3. 重启 AstrBot 或在插件管理页重载

## 配置

在 AstrBot 插件管理页配置：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `card_theme_preset` | 卡片主题（四选一） | `light` |
| `enable_image_output` | 图片回复；关闭改发 Markdown | `true` |
| `prefer_local_render` | 优先本地渲染（**建议保持开启**） | `true` |
| `enable_llm_summary` | 由插件调大模型生成卡片末尾点评 | `true` |
| `summary_provider_id` | 生成总结的模型（**别选推理模型**） | 空（用当前会话模型） |
| `summary_timeout` | 生成总结超时（秒） | `180` |
| `summary_max_chars` | 非单局点评的最大字数（单局复盘固定上限 1200） | `500` |
| `enable_replay_summary` | 单局总结改用录像解析数据 | `true` |
| `replay_parse_timeout` | 等待录像解析上限（秒） | `720` |
| `steam_api_key` | Steam API Key（可选，OpenDota 无数据时兜底） | 空 |
| `request_timeout` | API 请求超时（秒） | `15` |
| `cache_ttl_seconds` | 数据缓存时间（秒） | `86400` |
| `default_language` | 英雄/物品名称语言 | `cn` |
| `enable_fallback_commands` | 是否启用 `/dota` 命令 | `true` |

### 点评模型：不要选推理模型

推理模型会把输出预算花在"思考"（`reasoning_content`）上，正文可能返回空，
导致总结静默退回成主对话模型的文字。**看名字判断不可靠**（有些叫 flash 的
其实是推理模型），推荐实测可用的 **`GLM/GLM-4-Flash-250414`**（约 10 秒出结果、
正文完整）。选中它后，可把 `summary_timeout` 从 180 改小到 30，失败回退更快。

### Steam API Key（可选）

OpenDota 默认无需 Key。若某些比赛数据 OpenDota 取不到，可在
https://steamcommunity.com/dev/apikey 申请后配置 `steam_api_key`，
插件会在 OpenDota 返回空时自动切换 Valve API。

## 数据源

| API | 用途 |
|-----|------|
| [OpenDota](https://docs.opendota.com/) | 玩家 / 英雄 / 物品 / 比赛 / 实时 / 职业（默认） |
| Valve Steam Web API | OpenDota 无数据时的备选 |
| 本地 Pillow / AstrBot t2i | 卡片渲染（本地优先，t2i 兜底） |

## 开发

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
ruff check .
```

### 代码结构

```
main.py                    # 入口、斜杠命令、on_decorating_result 出图钩子
compat.py                  # AstrBot API 兼容层
_conf_schema.json          # 配置项定义
assets/                    # card.html 模板、英雄/物品 ID 与名称表
core/templates.py          # 各查询结果的 Markdown 组织
core/card_renderer.py      # 渲染调度（本地优先 + t2i 兜底 + 校验）、主题预设
core/image_renderer.py     # 本地 Pillow 渲染器
core/summarizer.py         # 卡片末尾点评（分场景调模型、录像解析总结）
core/opendota.py           # OpenDota 客户端 + 录像解析（request/轮询）
core/hero_names.py         # 英雄名翻译与匹配
core/icon_cache.py         # 图标/立绘本地缓存 + data URI 内联
tools/                     # 9 个 LLM 工具 + 结果交付
```

## 致谢

本插件基于 [yarizm/astrbot_plugin_dota2assistant](https://github.com/yarizm/astrbot_plugin_dota2assistant)
修改加强而来，数据查询与 OpenDota/Valve 对接的地基工作出自原作者。
加强版主要改动：卡片改为四套主题预设、单局总结改用 OpenDota 录像解析数据、
本地渲染优先、新增图片输出开关等配置。

## License

MIT
