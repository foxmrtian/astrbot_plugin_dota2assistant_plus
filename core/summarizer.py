"""赛后总结生成：由插件自己调用 AstrBot 里的对话大模型产出点评。

为什么不让 Agent（主对话模型）的回复充当总结：

* Agent 这一轮可能因为上游 503、工具调用序列损坏等原因直接报错，
  那样图片底部就没有总结（实测私聊里就出现过）；
* 部分平台的回复会被其它插件改写（例如 outputpro），拿到的不一定是原始点评；
* 插件自己调模型，输入是**结构化的卡片数据**，总结质量更稳定、可复现。

调用失败时一律返回空串，由调用方回退到 Agent 的回复文本，
保证「图片一定有总结」这条底线不破。
"""
from __future__ import annotations

import asyncio
import re

from ..compat import logger
from .templates import KIND_MATCH, detect_kind, markdown_to_plain

# 喂给模型的卡片数据上限，避免超长 prompt。
# 点评要求包含 3 个固定维度（天辉关键 / 夜魇关键 / 一句话总结），
# 比早期「一句话点评」需要更多材料，因此上限相应放宽。
_MAX_INPUT_CHARS = 8000

_SYSTEM_PROMPT = (
    "你是资深 Dota2 数据分析师，正在复盘一整场比赛。"
    "请严格按下面三点写赛后总结。"
    "格式上**必须先写出小标题**，且只能是小标题 + 正文这两层：\n"
    "小标题固定为以下三行（原样照抄，不要改写、不要加序号）：\n"
    "### 天辉方的表现\n"
    "### 夜魇方的表现\n"
    "### 一句话总结\n"
    "每个小标题下面紧跟该栏目正文，三个栏目各写一段，顺序固定。\n"
    "重要：材料分成两段，段标题以「## 天辉」「## 夜魇」开头"
    "（标题后面还跟着胜负与团队总击杀，如「## 天辉 获胜 · 击杀 41」），"
    "每段标题下方紧跟该队的五名玩家，"
    "**不要跨段取人、不要把天辉玩家写进夜魇**。\n"
    "**称呼规则（必须遵守）**：一律用**英雄名**称呼玩家，"
    "例如「灰烬之灵」「敌法师」。"
    "不要写玩家昵称、不要写账号 ID、不要写「匿名玩家」「玩家1」这类占位词 —— "
    "卡片上的昵称经常是乱码或压根没有，用英雄名才能让读者对上号。"
    "英雄名写在每段标题行（### ）里，也可能出现在头像图片的 alt 中。\n"
    "**篇幅（硬约束，最容易违反）**：全文（三个栏目的正文相加）"
    "**不得超过 500 字**，超出的部分会被截掉。"
    "宁少勿多 —— 写满 500 字只是上限，写到 300-400 字往往更好读。"
    "因此**不要逐一点评每个英雄**，只挑最有信息量的说。\n"
    "**点评对象（硬约束）**：每方只挑**最突出的 1-2 人**讲，"
    "且必须能说清「为什么突出」，优先挑这两类：\n"
    "  * **特别突出**：经济/经验大幅领先全队，KDA 漂亮，或参战率很高（积极开团）；\n"
    "  * **特别落后**：经济明显垫底，KDA 很差，或参战率明显偏低于 100%（避战）。\n"
    "每方最多 2 人；若某队五个人表现平平均匀、没有明显突出或落后的，"
    "就用一句话概括该队整体，**不要为了凑数而人人写一句**。"
    "宁可只写 1 人，也不要写满 5 人。\n"
    "1. **天辉方的表现**：从**天辉五名玩家**里按上面的规则挑 1-2 人，"
    "写出英雄名并给出四项数据——**经济、经验、KDA、参战率**，"
    "用一两句话说清他好/差在哪（对线、团战、带线、节奏、避战等），"
    "最后用一句话说明天辉这一局输或赢的关键点在哪。\n"
    "2. **夜魇方的表现**：同样从**夜魇五名玩家**里挑 1-2 人，"
    "写出英雄名及其**经济、经验、KDA、参战率**并点评，"
    "最后用一句话说明夜魇输赢的关键点在哪。\n"
    "3. **一句话总结**：用一句话点明整场比赛输赢的关键在哪儿。\n"
    "说明："
    "「经济」用金钱（net_worth）与 GPM；「经验」用 XPM；"
    "「参战率」卡片上已算好，含义是**该玩家参战次数相对队友水平的高低**："
    "每次击杀、助攻、死亡都算一次战斗接触，K+A+D 越多越参战，"
    "所以 100% 表示与队友平均水平持平，明显低于 100% 说明该玩家"
    "击杀、助攻、死亡都偏少，是**避战**（只顾刷钱、不参与战斗）。"
    "点评时可据此判断谁在积极开团、谁在避战。\n"
    "格式要求：\n"
    "- 只用中文。第 1、2 点各写 2-3 句，第 3 点严格一句话；\n"
    "- 正文里提到英雄时，用两个星号把英雄名包住，写成 **英雄名** 的样子"
    "（**只写星号本身，不要把星号放进反引号或代码块里** —— "
    "`` 那种反引号会被原样画到卡片上），让英雄名在卡片上醒目；"
    "小标题行本身**不要**加星号；\n"
    "- 点名到的英雄必须**逐项写出四项数据**：金钱（如 18.6k）、XPM、"
    "KDA（必须写成「KDA 12/6/12」的斜杠格式）、参战率（如 86%）"
    "—— 四项缺一不可；没点名的英雄不用列数据；\n"
    "- 并引用卡片中的真实数据，不要编造、不要泛泛而谈；\n"
    "- 绝不输出玩家昵称、账号 ID 或「匿名玩家」等字样；\n"
    "- 不要输出除上述三个小标题以外的任何标题，不要用表格、代码块；\n"
    "- 某一点数据不足时据实说明，不要跳过该点。"
)

# ──────────────────────────────────────────────
# 非对局场景：一段式点评
# ──────────────────────────────────────────────

# 除「具体某一局的比赛详情」外，其余查询的卡片里没有分队、也没有十名玩家，
# 套用三点式赛后总结只会写出与上方列表对不上的内容（用户反馈的实际问题：
# 查近期战绩时，总结仍在讲「双方队伍表现」）。这些场景改成**一段式点评**，
# 并要求点评必须落在卡片上真实存在的字段上。
_REVIEW_COMMON = (
    "你是资深 Dota2 数据分析师，正在为用户解读一份查询结果。"
    "请针对下面这份数据写**一段点评**（不要分小标题、不要写「### 」、"
    "不要分点列条，就一整段连续的话）。\n"
    "**最重要的原则**：你写的每一句都必须能对应到上方数据里**真实出现**的"
    "字段与数值，只解读这份数据本身。不要引入数据里没有的信息"
    "（例如数据里没有胜率就别提胜率、没有版本强度就别谈版本），"
    "更不要自己编造数字 —— 用户会拿点评与上面的列表逐条对照。\n"
    "**称呼规则（必须遵守）**：提到英雄时一律用**英雄名**（如「敌法师」"
    "「灰烬之灵」）。不要输出玩家昵称、账号 ID 或「匿名玩家」"
    "这类占位词，除非用户问的就是自己的资料、且数据里只有昵称。\n"
    "**篇幅**：150-300 字最合适，不要超过 300 字。"
    "宁可短一点讲透，也不要为凑字数重复列表里已有的数字。"
)

# 每种卡片配一段「这份数据里有什么、该从哪个角度点评」的说明。
# 这些说明同时起到**约束模型不跑偏**的作用：明确告诉它手上有什么，
# 它就不会去讲卡上没有的东西。
_REVIEW_SCOPES: dict[str, str] = {
    "player": (
        "这份数据是**某位玩家的资料与近期战绩**：包含段位、估算 MMR、"
        "最近若干场的胜负、总胜率、平均 KDA，以及每一场所用英雄、"
        "该场 KDA、时长、GPM 与英雄伤害。\n"
        "点评角度：先说近期状态（胜负与胜率反映的手感、是否连败/连胜），"
        "再结合平均 KDA 与逐场的 GPM、伤害指出他打得好的场次与打得差的场次，"
        "点名**具体英雄**（如「用灰烬之灵那场 12/3/8」），"
        "最后一句给出可操作的建议（例如某类英雄或某个环节值得加强）。"
        "不要评价并不存在的「队友」或「对局双方」。"
    ),
    "hero": (
        "这份数据是**单个英雄的档案**：属性成长、定位、基础数值，"
        "以及天梯与职业比赛的选取次数和胜率（没有则说明该英雄暂无数据）。\n"
        "点评角度：先一句话说清这个英雄的定位与打法特点（力量/敏捷/智力，"
        "近战/远程，适合什么位置），再结合属性成长说明它的强势期（前期线上、"
        "中期节奏还是后期成型），最后用**数据里给出的**选取率与胜率"
        "说明它当前的表现。数据里没有胜率时就不要提胜率。"
    ),
    "hero_list": (
        "这份数据是**英雄名单**（按主属性或定位筛选后的结果），"
        "只有英雄名，**没有任何数值**。\n"
        "点评角度：概述这份名单的构成（各属性/定位各有多少、偏向什么打法），"
        "如果用户提了筛选条件就说明筛选结果的特点，"
        "如果用户问「推荐」就从中推荐 2-3 个并说明理由。"
        "**绝对不要编造胜率、选取率或版本强度** —— 这份数据里没有这些。"
    ),
    "hero_build": (
        "这份数据是**某个英雄的分阶段出装推荐**：出门装、前期、中期、后期"
        "各有哪些装备及各自的选取率。\n"
        "点评角度：按阶段讲清出装思路 —— 前期先出什么、"
        "核心装备是哪件、为什么要这么出（续航、输出还是先手），"
        "并结合**数据里给出的选取率**指出哪些是主流选择。"
    ),
    "item": (
        "这份数据是**一件物品的信息**：价格、类型、效果说明、合成组件。\n"
        "点评角度：先说明这件装备适合什么类型的英雄、在什么局势下该出，"
        "再结合价格与属性说明它的性价比与合成路径是否平滑。"
        "不要虚构数据里没有的数值。"
    ),
    "live": (
        "这份数据是**正在进行中的比赛列表**：队伍名、当前比分、"
        "已进行时间、平均 MMR、观战人数。\n"
        "点评角度：指出哪几场最值得一看（比分胶着、进行到关键时段、"
        "MMR 较高或观战人数多），并说明理由。"
        "数据里没有选手与英雄，就不要点评具体打法。"
    ),
    "pro": (
        "这份数据是**近期职业比赛列表**：对阵双方、比分、胜者、时长、"
        "所属赛事与开赛时间。\n"
        "点评角度：概括这段时间的职业赛事看点（哪些队伍状态好、"
        "是否有横扫或鏖战、涉及哪些赛事），并点名值得回看的场次。"
        "数据里没有阵容与英雄，就不要点评具体战术。"
    ),
}


def _review_prompt(kind: str) -> str:
    """非对局场景的系统提示词：一段式点评 + 该种类的数据范围说明。"""
    scope = _REVIEW_SCOPES.get(kind)
    if not scope:
        # 认不出种类时的兜底：只要求「与上方数据对应」，
        # 不编造字段。宁可泛泛，也不要写出与列表冲突的内容。
        scope = (
            "这份数据的具体类型未能识别，请只根据数据里**真实出现**的内容"
            "写一段点评，不要引入数据之外的任何信息。"
        )
    return _REVIEW_COMMON + "\n【这份数据是什么】\n" + scope


# 用户点名问某个英雄时的追加指令。用户可能说「XX比赛 我朋友用的火猫怎么样」，
# 这时除三点常规点评外，还要单独针对该英雄写一段。
_FOCUS_PROMPT = (
    "\n\n【额外要求】用户在提问中点名关注「{hero}」这个英雄。"
    "请在上面三点之后，再单独加一段以「4.」开头，专门点评该英雄本局的表现"
    "（结合其经济、经验、KDA、参团率、伤害与出装），"
    "并说明它是否达到了应有水平。"
)

# 非对局场景下用户点名英雄时的追加指令。原 _FOCUS_PROMPT 说的是
# 「在上面三点之后，再加一段以 4. 开头」——那是给三点式赛后总结用的；
# 非对局只有一段点评，套用那句话会让模型去编号、破坏「一整段」的要求。
_REVIEW_FOCUS_PROMPT = (
    "\n\n【额外要求】用户在提问中点名关注「{hero}」这个英雄。"
    "请把点评的重点放在该英雄上，结合这份数据里与它相关的字段"
    "（定位、属性、选取率、胜率、出装等）说明它当前的表现与适用场景，"
    "并把结论自然融进同一段话里，**不要另起编号、不要分小标题**。"
)

# 三个栏目的固定小标题。模型偶尔漏写、改写小标题（或走 Agent 兜底拿不到
# 结构化输出），因此栏目标题一律由插件补齐，卡片上的三个栏目才每次都一致。
_SECTION_TITLES = ("天辉方的表现", "夜魇方的表现", "一句话总结")

# 定位每个栏目起始行的关键词。只认行首这几个词，因此模型写成
# 「### 天辉方的表现」「1. 天辉方胜负关键：」「2、夜魇」都能对上。
_SECTION_KEYS = (("天辉",), ("夜魇",), ("一句话", "小结", "总之", "总结"))

# 可选的第 4 段：用户点名了某个英雄时的追加点评（见 _FOCUS_PROMPT）。
# 它排在「一句话总结」之后，若不单独切出来会被并进一句话总结里 ——
# 定点点评往往是一整段，混进去会读成「总结」的一部分。
# 只认行首的「4.」或「点名」，避免误伤正文里出现的数字。
_FOCUS_HEAD_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(?:4\s*[.、)]|点名|【点名)", re.M)
_FOCUS_TITLE = "点名英雄"
# 第四段开头「4. 点名英雄：」这类标题文字本身要去掉，只留正文
_FOCUS_STRIP_RE = re.compile(
    r"^(?:\d{1,2}\s*[.、)]\s*)?(?:点名(?:英雄)?|【点名[^】]*】)\s*[：:，,。．.、\-—]*\s*"
)

# 行首的装饰性标记：井号标题、数字序号、项目符号、方括号小标题。
_HEADING_PREFIX_RE = re.compile(
    r"^\s*(?:#{1,6}\s*|\d{1,2}\s*[.、)]\s*|[-*+•]\s*|【[^】]{0,12}】\s*)+"
)

# 栏目起始行里的标题文字本身（含常见旧写法），连同其后的分隔标点一起去掉，
# 只留正文。否则模型写成「1. 天辉方胜负关键：水晶室女…」时，整行会被丢掉。
_SECTION_TITLE_RE = re.compile(
    r"^(?:天辉方的表现|夜魇方的表现|一句话总结|"
    r"天辉方胜负关键|夜魇方胜负关键|"
    r"天辉方|夜魇方|天辉|夜魇|一句话|小结|总之|总结)"
    r"\s*[：:，,。．.、\-—]*\s*"
)

_PLACEHOLDER_NAMES = {"匿名玩家", "未知玩家", "anonymous", "-", ""}

# 标题行里的英雄头像语法：``![英雄名](立绘URL)``。英雄名现在写在图片的
# alt 里（渲染成头像），因此提取关键英雄时必须先把 alt 取出来。
_PORTRAIT_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")


def _match_terms(cards: list[str]) -> list[str]:
    """从数据卡片里提取要在总结中加粗的**关键英雄**。

    不能指望模型自己记得加粗：实测同一份提示词下，它有时加粗、有时不加，
    所以「重点显示」必须由代码确定性地完成。这里只需解析卡片标题行：

        ### ![暗夜魔王](https://.../night_stalker_vert.jpg) · Lv.19 · KAKA · **12/6/12**

    只取英雄名（图片 alt），不取玩家 ID：

    * 玩家 ID 常常是乱码昵称或空（``匿名玩家``），写进总结毫无信息量；
    * 用户要的是「哪个英雄打得怎么样」，英雄名才是稳定的标识，
      也因此赛后总结用英雄名而不是选手 ID（见 :data:`_SYSTEM_PROMPT`）。

    返回按长度降序排列的词表，避免短名先匹配、把长名切碎。
    """
    heroes: list[str] = []
    for card in cards or []:
        for line in str(card).splitlines():
            if not line.startswith("### "):
                continue
            body = line[4:].strip()
            # 兼容旧卡片：胜负标记原先在玩家行首（✅/❌），现已移到分队标题。
            # 历史消息与缓存里的旧卡片仍可能带标记，剥掉以免影响英雄名提取。
            body = re.sub(r"^[✅❌✔✖]\s*", "", body)

            # 英雄名：优先取头像图片的 alt（新版卡片），
            # 没有图片时退回标题第一段（旧格式/降级后的卡片）
            portrait = _PORTRAIT_RE.search(body)
            hero = portrait.group(1).strip() if portrait else ""
            if not hero:
                first = body.split("·")[0].strip()
                hero = re.sub(r"\s*Lv\..*$", "", first).strip()
            hero = hero.strip()
            if hero and hero not in _PLACEHOLDER_NAMES:
                heroes.append(hero)

    terms: list[str] = []
    for term in heroes:
        if term and term not in terms:
            terms.append(term)

    # 非对局卡片（近期战绩表、英雄档案、出装等）没有 ``### ![英雄](立绘)``
    # 这样的标题行，从上面取不到任何英雄名 —— 但那些卡片里同样有英雄名
    # （战绩表的「英雄」列、英雄档案的 h1），点评点到时也该加粗。
    # 这时退回按**官方英雄名表**去认。
    if not terms:
        terms = _heroes_mentioned_in(cards)

    # 长词优先，避免「敌法师」被更短的词先吃掉
    terms.sort(key=len, reverse=True)
    return terms


def _heroes_mentioned_in(cards: list[str]) -> list[str]:
    """非对局卡片：按官方英雄名表，找出卡片里出现过的英雄名。

    只认长度 >= 2 的名字（见 :func:`hero_names.all_hero_names`），
    单字名（陈 / 凯 / 獸）在中文里太容易误伤。返回的只是**候选词表**，
    是否真的写进点评仍由 :func:`_bold_terms` 按正文里实际出现的词决定，
    因此多认几个名字不会让卡片上冒出无关的粗体。
    """
    text = "\n".join(str(c) for c in (cards or []) if c)
    if not text:
        return []
    try:
        from .hero_names import all_hero_names
    except Exception:
        return []
    return [name for name in all_hero_names() if name in text]


def _bold_terms(text: str, terms: list[str]) -> str:
    """把 ``text`` 里出现的 ``terms`` 包成 ``**粗体**``。

    已经处于 ``**...**`` 中的片段跳过，避免出现 ``****`` 这类嵌套标记。
    逐个词处理，每次只重复加粗**未加粗**的片段。
    """
    if not text or not terms:
        return text

    for term in terms:
        # 按已有加粗标记切分：奇数下标是已加粗内容，保持原样
        segments = re.split(r"(\*\*.+?\*\*)", text, flags=re.S)
        for i, seg in enumerate(segments):
            if i % 2 == 1 or not seg or term not in seg:
                continue
            segments[i] = seg.replace(term, f"**{term}**")
        text = "".join(segments)
    return text


def _split_sections(text: str) -> list[tuple[str, str]]:
    """把总结正文按「天辉 / 夜魇 / 一句话」切成若干段（可选追加「点名英雄」）。

    返回 ``[(小标题, 正文), ...]``，一段都定位不到时返回空列表，
    由调用方决定怎么退化（不硬套栏目）。

    不依赖模型写的小标题长什么样，只按**行首关键词**定位，因此模型写成
    ``### 天辉方的表现``、``1. 天辉方胜负关键：`` 或 ``2、夜魇`` 都能对上。
    小标题那一行本身会被剥掉标题文字与分隔标点，只保留其后的正文 ——
    否则写成「1. 天辉方胜负关键：水晶室女…」时整行正文都会被丢掉。

    允许只切出前两段：总结比 ``summary_max_chars`` 长时会被截断，
    第三段可能整个没了。这时给前两段补上正确的小标题，
    仍好过退回「1. 天辉方胜负关键：…」这篇没有栏目的原文。
    只剩一段时不补 —— 单靠一个「天辉」就下结论太容易误伤自由行文
    （「天辉前期压制力不足」是正文，不是栏目标题）。
    除非那一段确实**带着标题标记**（`#` 或 `1.` 序号），
    那就说明模型是按栏目在写、只是被截断了，可以放心补标题。
    """
    lines = text.splitlines()
    # 按**栏目标题**认段，而不是按出现顺序编号：模型偶尔会整段漏掉某一栏，
    # 若按顺序配标题，「一句话总结」的文字就会被挂到「夜魇方的表现」下面。
    # 这里记下命中的是哪一栏（group），标题一律取自它。
    hits: list[tuple[int, int]] = []
    cursor = 0
    for group, keys in enumerate(_SECTION_KEYS):
        for idx in range(cursor, len(lines)):
            content = _HEADING_PREFIX_RE.sub("", lines[idx]).strip()
            if content and any(content.startswith(key) for key in keys):
                hits.append((group, idx))
                cursor = idx + 1
                break

    if not hits:
        return []
    # 只有一段时，必须确认它真的带标题标记（# 或 1. 序号）：
    # 「天辉前期压制力不足」是正文，不是栏目标题。
    if len(hits) == 1 and not _has_heading_mark(lines[hits[0][1]]):
        return []

    out: list[tuple[str, str]] = []
    for order, (group, start) in enumerate(hits):
        end = hits[order + 1][1] if order + 1 < len(hits) else len(lines)
        # 起始行去掉标题文字后剩下的正文，可能与后续行同属一段
        first = _SECTION_TITLE_RE.sub(
            "", _HEADING_PREFIX_RE.sub("", lines[start]).strip()
        ).strip()
        rest = [
            line for line in lines[start + 1:end]
            if not line.lstrip().startswith("#")
        ]
        body = "\n".join(([first] if first else []) + rest).strip()
        out.append((_SECTION_TITLES[group], body))

    # 第四段：用户点名英雄时的追加点评。只在「一句话总结」那一段里找，
    # 因为它的编号（4.）排在最后；找到就把它从一句话总结里切出来，
    # 让「一句话总结」真的只是一句话。
    # 先确认末段确实是「一句话总结」那一栏：截断缺段时末段可能是夜魇方，
    # 在它身上找「4.」纯属误伤。
    if hits[-1][0] == len(_SECTION_KEYS) - 1:
        title, body = out[-1]
        match = _FOCUS_HEAD_RE.search(body)
        if match:
            focus = body[match.start():]
            focus = _HEADING_PREFIX_RE.sub("", focus).strip()
            focus = _FOCUS_STRIP_RE.sub("", focus).strip()
            head = body[:match.start()].strip()
            out[-1] = (title, head)
            if focus:
                out.append((_FOCUS_TITLE, focus))

    return out


def format_analysis(text: str, cards: list[str], max_chars: int = 0,
                    kind: str = "") -> str:
    """整理模型输出：对局排成三个固定小栏目，其余场景排成一段式点评。

    ``kind`` 决定版式（见 :func:`templates.detect_kind`）：

    * **对局**（``kind`` 为 ``match`` 或空）：把内容切成
      「天辉方的表现 / 夜魇方的表现 / 一句话总结」三个小栏目，
      栏目标题由插件补齐（见 :data:`_SECTION_TITLES`）。
      定位不到三个栏目时（例如模型返回自由段落、或走了 Agent 回复兜底）
      原样返回，**不硬套**标题 —— 把一句话总结塞进「天辉方的表现」
      比没有小标题更糟。
    * **其余场景**：卡片里没有分队也没有十名玩家，**不设小标题**，
      整段输出。此时强制加三栏只会把「一句话总结」之类的标题
      安到一段本就不分栏的点评上。

    两种情况都只做两件事：把英雄名加粗（代码确定性完成，不依赖模型自觉）、
    以及按 ``max_chars`` 控制篇幅。篇幅控制上，对局按**栏目**分摊压缩 ——
    直接砍尾部的话，被砍掉的总是最后的「一句话总结」，读者看到的就成了
    「内容不完整」；非对局整段只有一段，按句末标点收缩即可。
    """
    body = (text or "").strip()
    if not body:
        return ""

    terms = _match_terms(cards)
    if not _is_match_kind(kind):
        # 非对局：一段式。即使模型自己写了「### 」小标题也去掉 ——
        # 版式上这一块只有一段正文（详见 _review_prompt 的说明）。
        flat = _flatten_headings(body)
        out = _bold_terms(flat, terms)
        return _shrink_text(out, max_chars) if max_chars else out

    sections = _split_sections(body)
    if not sections:
        out = _bold_terms(body, terms)
        return _shrink_text(out, max_chars) if max_chars else out

    if max_chars:
        sections = _fit_sections(sections, max_chars)

    blocks: list[str] = []
    for title, section in sections:
        section = _bold_terms(section, terms)
        if not section:
            continue
        blocks.append(f"### {title}\n\n{section}")
    return "\n\n".join(blocks) if blocks else _bold_terms(body, terms)


def _is_match_kind(kind: str) -> bool:
    """该种类是否按三点式赛后总结处理。

    认不出种类（空串）时按对局处理：沿用原行为，
    历史卡片、缓存卡片与既有测试夹具都不会因此改变表现。
    """
    return kind in ("", KIND_MATCH)


def _flatten_headings(text: str) -> str:
    """去掉行首的 Markdown 标题标记，把各段合并成连续正文。

    非对局点评只该是一段话。模型偶尔仍会写成「### 表现」这种小标题，
    照原样画出来就会在「点评」区块里多出一层级不明的标题，
    与卡片上方的 h1/h2 混在一起。这里只剥标记、不动文字。
    """
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = _HEADING_PREFIX_RE.sub("", raw).strip()
        if line:
            lines.append(line)
    return "\n\n".join(lines)


def _shrink_text(text: str, max_chars: int) -> str:
    """按句末标点把一段文字缩到 ``max_chars`` 内（用于非分栏输出的兜底）。"""
    if not max_chars or len(text) <= max_chars:
        return text
    cut = text[:max_chars].rstrip()
    # 尽量在句末断开，读起来才不像被硬砍
    for mark in ("。", "；", "！", "？", "\n"):
        pos = cut.rfind(mark)
        if pos >= max_chars // 2:
            return cut[:pos + 1]
    return cut.rstrip("，,、") + "…"


def _shrink_section_bodies(bodies: list[str], budget: int) -> list[str]:
    """把各栏正文整体压进 ``budget`` 总字数，返回等长的新正文列表。

    录像复盘的四栏篇幅需受控。策略：总和不超就直接返回；超了先按
    「等比压缩」给每栏一个目标字数，再逐栏按句末收缩到各自目标，
    保证四栏结构都还在（不像 ``_shrink_text`` 那样一刀切掉末尾整栏）。
    """
    total = sum(len(b) for b in bodies)
    if not budget or total <= budget:
        return bodies
    out: list[str] = []
    for b in bodies:
        if not b:
            out.append(b)
            continue
        # 等比分配，但每栏至少留 30 字，避免某栏被压没
        target = max(30, round(budget * len(b) / total))
        out.append(_shrink_text(b, target))
    return out


def _fit_sections(sections: list[tuple[str, str]], max_chars: int,
                  ) -> list[tuple[str, str]]:
    """在 ``max_chars`` 总额度内精简各栏目。

    「一句话总结」与「点名英雄」优先保全：前者本就只要一句、被砍最刺眼，
    后者是用户主动问的、砍了等于没回答。剩余额度由天辉/夜魇两栏均分，
    各自按句末标点收缩。
    """
    total = sum(len(body) for _title, body in sections)
    if total <= max_chars:
        return sections

    keep_titles = {_SECTION_TITLES[-1], _FOCUS_TITLE}
    keep_idx = [i for i, (t, _b) in enumerate(sections) if t in keep_titles]
    keep_len = sum(len(sections[i][1]) for i in keep_idx)
    others = [i for i in range(len(sections)) if i not in keep_idx]
    if not others:
        # 只剩需要保全的栏目：仍给个硬上限，避免完全失控
        return [(t, _shrink_text(b, max(30, max_chars // max(1, len(sections)))))
                for t, b in sections]

    # 每个待压缩栏目至少留 30 字，免得被压成空标题
    budget = max(max_chars - keep_len, len(others) * 30)
    each = max(30, budget // len(others))

    out = list(sections)
    for i in others:
        title, body = out[i]
        out[i] = (title, _shrink_text(body, each))
    return out


def _line_section_key(line: str) -> str:
    """若该行是某个栏目的标题行，返回命中的关键词，否则返回空串。"""
    head = _HEADING_PREFIX_RE.sub("", line).strip()
    for keys in _SECTION_KEYS:
        for key in keys:
            if head.startswith(key):
                return key
    return ""


def _has_heading_mark(line: str) -> bool:
    """该行是否带标题标记（``#`` 或 ``1.`` / ``1、`` 这类序号）。

    用来区分「栏目标题行」与「正文恰好以天辉开头」：
    ``1. 天辉方胜负关键：…`` 带序号，是标题；
    ``天辉前期压制力不足`` 是正文，不能当标题处理。
    """
    stripped = line.lstrip()
    return stripped.startswith("#") or bool(
        re.match(r"^\d{1,2}\s*[.、)]", stripped))


def _clean(text: str) -> str:
    """清掉模型可能自带的标题前缀与多余空白。"""
    out = (text or "").strip()
    if not out:
        return ""

    # 去掉首行的 Markdown 标题 —— 但**小栏目标题要留下**。
    # 模型偶尔会把「赛后总结」当大标题写出来，那一行是多余的；
    # 而「### 天辉方的表现」本身就是我们要的栏目，删掉它会让首段失去标题、
    # 后面的分栏也定位不到（天辉那一段会退化成一篇没标题的正文）。
    #
    # 判据是「首行像栏目 且 后面还有别的栏目标题」：
    # 「# 总结」本身两可 —— 既可能是多余大标题（后面只有一段正文），
    # 也可能是第三个栏目的标题（前面还有天辉/夜魇两栏）。只有后者才保留。
    lines = out.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        is_section = bool(_line_section_key(lines[0]))
        rest_has_sections = any(
            _line_section_key(line) for line in lines[1:]
            if line.lstrip().startswith("#")
        )
        if not (is_section and rest_has_sections):
            lines = lines[1:]
            out = "\n".join(lines).strip()

    for prefix in ("总结：", "总结:", "赛后总结：", "赛后总结:", "综合分析：", "综合分析:"):
        if out.startswith(prefix):
            out = out[len(prefix):].strip()
            break

    # 去掉行内代码标记。提示词里用 ``**英雄名**`` 举例说明加粗写法，
    # 模型有时会把那对反引号一起抄进正文（``**敌法师**``），
    # 于是网络 t2i 上会**原样画出反引号**，而本地 Pillow 会把它当格式标记
    # 清掉 —— 同一条总结在两条渲染路径下长相不同。这里统一清掉反引号，
    # 加粗标记 ``**`` 本身保留。
    out = out.replace("`", "")

    return out.strip()


async def _resolve_provider(host, event):
    """按 配置指定 → 会话默认 → 任意可用 的顺序挑一个对话模型。"""
    context = getattr(host, "context", None)
    if context is None:
        return None

    configured = str(getattr(host, "summary_provider_id", "") or "").strip()
    if configured:
        get_by_id = getattr(context, "get_provider_by_id", None)
        if callable(get_by_id):
            try:
                prov = get_by_id(configured)
            except Exception as exc:
                logger.warning(f"Dota2 总结模型 {configured} 不可用: {exc}")
                prov = None
            if prov is not None:
                return prov
            logger.warning(f"Dota2 总结模型 {configured} 未找到，改用会话默认模型。")

    umo = None
    try:
        umo = getattr(event, "unified_msg_origin", None)
    except Exception:
        umo = None

    get_using = getattr(context, "get_using_provider_async", None)
    if callable(get_using):
        try:
            prov = await get_using(umo)
        except Exception as exc:
            logger.warning(f"Dota2 获取会话默认模型失败: {exc}")
            prov = None
        if prov is not None:
            return prov

    get_all = getattr(context, "get_all_providers", None)
    if callable(get_all):
        try:
            providers = get_all() or []
        except Exception:
            providers = []
        if providers:
            return providers[0]

    return None


async def summarize(host, cards: list[str], event=None, focus_hero: str = "") -> str:
    """调用本机默认大模型，对卡片数据写一段赛后总结。

    Args:
        host: 插件实例（需要 ``context`` 与总结相关配置）。
        cards: 待点评的卡片 Markdown 列表。
        event: 用于解析会话默认模型，可为 None。
        focus_hero: 用户点名的英雄名（如「火猫」）。非空时额外要求单独点评
            该英雄本局的表现。

    Returns:
        总结文本；任何一步失败都返回空串，由调用方回退到 Agent 的回复。
    """
    if not getattr(host, "enable_llm_summary", True):
        return ""

    # 卡片里的装备是 ``![中文物品名](Valve CDN URL)``：URL 对点评毫无价值，
    # 却会把正文撑大约 5 倍，导致 _MAX_INPUT_CHARS 截断后模型只看到前 1/5 的
    # 比赛数据。先降级成纯文本（保留物品名）再限量。
    material = markdown_to_plain("\n\n".join(c.strip() for c in cards if c and c.strip()))
    if not material:
        return ""

    # 只有「具体某一局」才有双方十人的数据，能写三点式赛后总结；
    # 其余查询（近期战绩 / 英雄 / 列表 / 出装 / 物品 / 实时 / 职业）改成
    # 一段式点评，且点评范围由种类决定 —— 否则会写出与上方列表对不上的
    # 「天辉方的表现 / 夜魇方的表现」（用户实际反馈的问题）。
    kind = detect_kind(cards)
    is_match = kind in ("", KIND_MATCH)

    try:
        provider = await _resolve_provider(host, event)
    except Exception as exc:
        logger.warning(f"Dota2 总结模型解析异常: {exc}")
        return ""

    if provider is None:
        logger.warning("Dota2 未找到可用的对话模型，跳过模型总结，改用 Agent 回复。")
        return ""

    if len(material) > _MAX_INPUT_CHARS:
        material = material[:_MAX_INPUT_CHARS].rstrip() + "…"

    if is_match:
        system_prompt = _SYSTEM_PROMPT
        focus = (focus_hero or "").strip()
        if focus:
            system_prompt += _FOCUS_PROMPT.format(hero=focus)
        prompt = f"以下是刚查询到的 Dota2 数据，请按要求写赛后总结：\n\n{material}"
    else:
        # 非对局：一段式点评。用户点名的英雄点评同样适用 ——
        # 例如「查一下我朋友的火猫」问的是英雄数据，也是单段。
        system_prompt = _review_prompt(kind)
        focus = (focus_hero or "").strip()
        if focus:
            system_prompt += _REVIEW_FOCUS_PROMPT.format(hero=focus)
        prompt = f"以下是刚查询到的 Dota2 数据，请针对这份数据写一段点评：\n\n{material}"
    timeout = getattr(host, "summary_timeout", 180)
    max_chars = getattr(host, "summary_max_chars", 500)

    try:
        # 不传 tools：这里只要一段文字，避免再引发一轮工具调用。
        #
        # 注意：**不要**想着用 max_tokens 给推理模型留输出预算 ——
        # AstrBot 的 OpenAI provider 在 ``_prepare_chat_payload`` 里只把
        # ``messages`` 和 ``model`` 放进请求体，多余的 kwargs 会被直接丢弃
        # （实测传入 max_tokens 后最终 payload 里并没有它），
        # 想加预算只能改 AstrBot 全局的 custom_extra_body，插件不该动那个。
        #
        # 因此规避推理模型的正确做法是**选对模型**：
        # 有些模型名字里带 flash，实际却是推理模型（如 deepseek-v4-flash），
        # 会把预算全花在 reasoning_content 上、正文返回空字符串。
        # 详见 README「赛后总结模型」一节。
        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
                request_max_retries=1,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(f"Dota2 模型总结超时（>{timeout}s），改用 Agent 回复。")
        return ""
    except Exception as exc:
        logger.warning(f"Dota2 模型总结失败，改用 Agent 回复: {exc}")
        return ""

    model_hint = getattr(provider, "model_name", "") or type(provider).__name__
    try:
        text = _clean(response.completion_text if response else "")
    except Exception as exc:
        logger.warning(f"Dota2 解析模型总结失败: {exc}")
        return ""

    if not text:
        # 最常见的成因：选到推理模型，输出预算全花在 reasoning_content 上，
        # 正文被截断成空串。把这条线索直接写进日志，避免再次误判成
        # 「插件没跑」而去反复查开关。
        reasoning = getattr(response, "reasoning_content", None) if response else None
        if reasoning:
            logger.warning(
                f"Dota2 模型 {model_hint} 只返回了思考、正文为空"
                f"（思考 {len(str(reasoning))} 字）。"
                "多半是选了推理模型且输出预算不足，"
                "请改选非推理模型（如 GLM/GLM-4-Flash-250414）。"
                "本次总结改用 Agent 回复。"
            )
        else:
            logger.warning("Dota2 模型总结为空，改用 Agent 回复。")
        return ""

    # 三点内容由模型写，小标题与加粗由这里确定性地补齐：
    # 实测模型有时加粗关键英雄、有时不加，小标题也常被改写成「天辉方胜负关键」
    # 这类旧写法，交给代码统一才能保证每张卡片的三个栏目都齐整。
    #
    # 字数上限也交给 format_analysis：按栏目分摊着压，而不是先截断再分栏。
    # 先截断的话，被砍掉的永远是结尾的「一句话总结」，读者看到的就是
    # 「总结显示不完整」——正是要修的现象。
    raw_len = len(text)
    text = format_analysis(text, cards, max_chars=max_chars, kind=kind)
    label = "赛后总结" if is_match else "点评"
    if raw_len > max_chars:
        logger.info(
            f"Dota2 {label}由模型 {model_hint} 生成（{raw_len} 字，"
            f"按 {max_chars} 字上限精简为 {len(text)} 字）。"
        )
    else:
        logger.info(f"Dota2 {label}由模型 {model_hint} 生成（{len(text)} 字）。")
    return text


# ──────────────────────────────────────────────
# 录像解析数据驱动的赛后总结
# ──────────────────────────────────────────────
#
# 与 ``summarize`` 的区别：那个函数把「卡片 Markdown」喂给模型，模型只能
# 复述 KDA/经济这些静态数字；这里改用 OpenDota **录像解析**后的数据
# （团战时间线、关键目标、对线优劣、经济曲线拐点、百分位表现），
# 模型能讲出「这场比赛是怎么赢/输的」，而不只是「谁数据好看」。
#
# 录像数据由 ``OpenDotaClient.wait_for_parse`` 异步取得（解析约需数分钟），
# 因此本函数只在后台任务里被调用，不进同步请求流水线。

# 百分位 benchmark 里挑出来讲的两类：明显carry（>85 分位）与明显拖后腿（<30 分位）。
_BENCH_KEYS = (
    ("gold_per_min", "经济"),
    ("xp_per_min", "经验"),
    ("kills_per_min", "击杀"),
    ("hero_damage_per_min", "输出"),
    ("last_hits_per_min", "补刀"),
)


def _pct(value) -> int:
    """OpenDota benchmark 的 pct 是 0~1 的小数，转成整数百分位。"""
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return 0


# 复盘「关键物品成型时点」时关注的大件（内部名 → 中文名）。
# 只列有战略意义、能改变局势的成型装；消耗品与廉价散件不讲。
_KEY_ITEMS: dict[str, str] = {
    "black_king_bar": "黑皇杖",
    "radiance": "辉耀",
    "battle_fury": "狂战斧",
    "divine_rapier": "圣剑",
    "daedalus": "代达罗斯之殇",
    "monkey_king_bar": "金箍棒",
    "butterfly": "蝴蝶",
    "satanic": "撒旦之邪力",
    "heart": "恐鳌之心",
    "assault": "强袭胸甲",
    "shivas_guard": "希瓦的守护",
    "scythe_of_vyse": "邪恶镰刀",
    "bloodthorn": "血棘",
    "nullifier": "否决坠饰",
    "sphere": "林肯法球",
    "manta": "幻影斧",
    "sange_and_yasha": "散夜对剑",
    "echo_sabre": "回音战刃",
    "desolator": "黯灭",
    "mjollnir": "雷神之锤",
    "silver_edge": "白银之锋",
    "invis_sword": "影刃",
    "blink": "闪烁匕首",
    "force_staff": "原力法杖",
    "cyclone": "风杖",
    "rod_of_atos": "阿托斯之棍",
    "orchid": "紫怨",
    "aether_lens": "以太透镜",
    "octarine_core": "八面玲珑",
    "refresher": "刷新球",
    "arcane_boots": "秘法鞋",
    "guardian_greaves": "卫士胫甲",
    "mekansm": "梅肯斯姆",
    "pipe": "洞察烟斗",
    "crimson_guard": "赤红甲",
    "lotus_orb": "莲花宝珠",
    "solar_crest": "太阳纹章",
    "spirit_vessel": "魂之灵瓮",
    "urn_of_shadows": "影之灵龛",
    "vladmir": "弗拉迪米尔的祭品",
    "helm_of_the_overlord": "主宰头盔",
    "wraith_pact": "怨灵之契",
    "boots_of_bearing": "行进之靴",
    "pavise": "圣盾",
    "aeon_disk": "永恒之盘",
    "wind_waker": "狂风之力",
    "gleipnir": "缚灵索",
    "harpoon": "鱼叉",
    "disperser": "驱散之锤",
    "phylactery": "圣物匣",
    "khanda": "坎达",
}

_ITEM_NAME_CACHE: dict[str, str] | None = None


def _item_zh(internal_key: str) -> str:
    """物品内部名（``black_king_bar``）→ 官方中文名；查不到返回内部名。

    优先用上面的大件白名单；白名单没有时回退查 ``item_ids.json``
    （``id → {n: 内部名, zh: 中文名}``）的反向映射。
    """
    if internal_key in _KEY_ITEMS:
        return _KEY_ITEMS[internal_key]
    global _ITEM_NAME_CACHE
    if _ITEM_NAME_CACHE is None:
        table: dict[str, str] = {}
        try:
            from .item_icons import _load as _load_items

            for entry in _load_items().values():
                if isinstance(entry, dict):
                    n, zh = entry.get("n"), entry.get("zh")
                    if n and zh:
                        table[str(n)] = str(zh)
        except Exception:
            table = {}
        _ITEM_NAME_CACHE = table
    return _ITEM_NAME_CACHE.get(internal_key, internal_key)


def _key_item_timings(purchase_log, limit: int = 4) -> list[tuple[int, str]]:
    """从 purchase_log 提取关键大件的成型时点，返回 [(秒, 中文名), ...]。

    只看白名单/大件表里的物品；同一物品只取最早一次（避免重复购买刷屏）。
    """
    seen: dict[str, int] = {}
    for entry in purchase_log or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "")
        t = entry.get("time")
        if key and isinstance(t, (int, float)) and key not in seen:
            zh = _item_zh(key)
            # 只保留大件（白名单命中，或中文名带「杖/剑/锤/心/甲/球/靴」等除外，
            # 这里简单以白名单 + 价格过滤，白名单已覆盖主流大件）
            if key in _KEY_ITEMS:
                seen[key] = int(t)
    ordered = sorted(seen.items(), key=lambda kv: kv[1])
    return [(t, _KEY_ITEMS[k]) for k, t in ordered[:limit]]


def _replay_material(match, hero_name) -> str:
    """把 ``match.replay`` 格式化成模型能读懂的中文结构化素材。

    ``hero_name``：``hero_id → 中文名`` 的解析函数（避免此处再引英雄表）。
    只挑「能讲故事」的字段；逐秒事件流这类原始数据不喂，太长也没信息量。
    """
    r = getattr(match, "replay", None) or {}
    if not r:
        return ""

    lines: list[str] = []

    def mmss(t) -> str:
        try:
            t = int(t)
        except (TypeError, ValueError):
            return "?"
        sign = "-" if t < 0 else ""
        t = abs(t)
        return f"{sign}{t // 60}:{t % 60:02d}"

    # ---- 一血 ----
    fb = r.get("first_blood_time")
    if fb is not None:
        lines.append(f"一血时间：{mmss(fb)}")

    # ---- 关键目标（推塔 / 兵营 / 肉山 / 不朽盾）----
    # objectives 的类型是 CHAT_MESSAGE_* / building_kill，不是 tower/roshan：
    #   building_kill          → 推塔 / 兵营（key 里是 building 名，含 tower/barracks）
    #   CHAT_MESSAGE_ROSHAN_KILL → 击杀肉山
    #   CHAT_MESSAGE_AEGIS     → 拾取不朽盾
    # 队伍判定：building_kill 的 team 是被推方（2=天辉 3=夜魇），
    # 推塔方 = 对方；肉山/盾的 player_slot/slot <128 为天辉。
    objs = r.get("objectives") or []
    if objs:
        bits = []
        for o in objs:
            otype = str(o.get("type") or "")
            t = mmss(o.get("time", 0))
            if otype == "building_kill":
                # 队伍不由 team 字段给（常为 None），而由 key 里的建筑名给：
                #   goodguys_* ＝天辉的建筑被推 → 推塔方是夜魇
                #   badguys_*  ＝夜魇的建筑被推 → 推塔方是天辉
                key = str(o.get("key") or "")
                if "goodguys" in key:
                    pusher = "夜魇"
                elif "badguys" in key:
                    pusher = "天辉"
                else:
                    pusher = ""
                label = "兵营" if "barracks" in key else "塔"
                bits.append(f"{t} {pusher}推{label}".replace("  ", " "))
            elif otype == "CHAT_MESSAGE_ROSHAN_KILL":
                slot = o.get("slot", o.get("player_slot", 128))
                team = "天辉" if (isinstance(slot, int) and slot < 128) else "夜魇"
                bits.append(f"{t} {team}击杀肉山")
            elif otype == "CHAT_MESSAGE_AEGIS":
                slot = o.get("slot", o.get("player_slot", 128))
                team = "天辉" if (isinstance(slot, int) and slot < 128) else "夜魇"
                bits.append(f"{t} {team}拾取不朽盾")
        if bits:
            lines.append("关键目标时间线：" + "；".join(bits[:16]))

    # ---- 团战（按总死亡数挑规模最大的几场，并给出哪方占优）----
    # teamfight.deaths 是该场总死亡数（int）；各玩家的 gold_delta/xp_delta
    # 之和能反映这场团战哪方赚了。players 顺序与 match.players 一致，
    # 前 5 个是天辉、后 5 个是夜魇。
    fights = r.get("teamfights") or []
    if fights:
        scored = []
        for f in fights:
            try:
                deaths = int(f.get("deaths") or 0)
            except (TypeError, ValueError):
                deaths = 0
            players = f.get("players") or []
            r_delta = sum(int((p or {}).get("gold_delta") or 0) for p in players[:5])
            d_delta = sum(int((p or {}).get("gold_delta") or 0) for p in players[5:])
            scored.append((deaths, f.get("start", 0), f.get("end", 0), r_delta, d_delta))
        scored.sort(reverse=True)
        bits = []
        for deaths, s, e, r_delta, d_delta in scored[:3]:
            if r_delta > d_delta:
                winner = "天辉占优"
            elif d_delta > r_delta:
                winner = "夜魇占优"
            else:
                winner = "势均力敌"
            bits.append(f"{mmss(s)}-{mmss(e)}（{deaths} 人阵亡，{winner}）")
        if bits:
            lines.append("主要团战：" + "；".join(bits))

    # ---- 经济曲线拐点（最大领先/落后）----
    adv = [x for x in (r.get("radiant_gold_adv") or []) if isinstance(x, (int, float))]
    if adv:
        peak = max(adv)
        trough = min(adv)
        peak_i = adv.index(peak)
        trough_i = adv.index(trough)
        lines.append(
            f"天辉经济领先峰值 {int(peak)}（约第 {peak_i} 分钟），"
            f"最深落后 {int(trough)}（约第 {trough_i} 分钟）"
        )

    # ---- 各玩家录像级数据（对线 / 百分位表现）----
    # 按天辉 / 夜魇**分组**列出，而不是十人连排 —— 连排时模型容易把某边的
    # 英雄记到另一边（实测把夜魇的大地之灵写进了「天辉方的表现」）。
    # 分组标题与卡片正文「## 天辉 / ## 夜魇」呼应，强化分边。
    def player_line(p) -> str:
        name = hero_name(p.get("hero_id", 0))
        seg = [name]
        le = p.get("lane_efficiency_pct")
        if le is not None:
            seg.append(f"对线效率 {le}%")
        if p.get("is_roaming"):
            seg.append("游走")
        # 百分位 benchmark：挑明显高于/低于同位置平均的指标
        bm = p.get("benchmarks") or {}
        hi, lo = [], []
        for key, label in _BENCH_KEYS:
            entry = bm.get(key) or {}
            pct = _pct(entry.get("pct"))
            if pct >= 85:
                hi.append(f"{label}{pct}分位")
            elif pct and pct <= 30:
                lo.append(f"{label}{pct}分位")
        if hi:
            seg.append("亮眼：" + "/".join(hi[:3]))
        if lo:
            seg.append("落后：" + "/".join(lo[:3]))
        return "（" + "，".join(seg) + "）"

    all_players = r.get("players", []) or []
    radiant = [player_line(p) for p in all_players if p.get("is_radiant")]
    dire = [player_line(p) for p in all_players if not p.get("is_radiant")]
    if radiant or dire:
        lines.append("各玩家录像级表现：")
        if radiant:
            lines.append("天辉方：\n" + "\n".join(radiant))
        if dire:
            lines.append("夜魇方：\n" + "\n".join(dire))

    # ---- 视野控制（双方插眼 / 排眼总量）----
    def vision(ps):
        obs = sum(int(p.get("obs_placed") or 0) for p in ps)
        sen = sum(int(p.get("sen_placed") or 0) for p in ps)
        ok = sum(int(p.get("observer_kills") or 0) for p in ps)
        return obs, sen, ok

    r_ps = [p for p in all_players if p.get("is_radiant")]
    d_ps = [p for p in all_players if not p.get("is_radiant")]
    if r_ps or d_ps:
        ro, rs, rk = vision(r_ps)
        do, ds, dk = vision(d_ps)
        lines.append(
            f"视野控制：天辉插眼 {ro} 观察 + {rs} 真视、排眼 {rk}；"
            f"夜魇插眼 {do} 观察 + {ds} 真视、排眼 {dk}"
        )

    # ---- 关键物品成型时点（双方核心的大件）----
    # 只挑每方最早做出关键大件的 1-2 人，讲「谁在什么时间拿出了什么」。
    item_bits = []
    for p in all_players:
        timings = _key_item_timings(p.get("purchase_log"))
        if not timings:
            continue
        name = hero_name(p.get("hero_id", 0))
        side = "天辉" if p.get("is_radiant") else "夜魇"
        first_t, first_item = timings[0]
        # 只报最早一件最有代表性的大件，避免刷屏
        item_bits.append((first_t, f"{mmss(first_t)} {side}·{name} {first_item}"))
    if item_bits:
        item_bits.sort()
        lines.append("关键物品成型：" + "；".join(b for _, b in item_bits[:6]))

    return "\n".join(lines)


def _fix_cross_side(text: str, match, hero_name) -> str:
    """纠正总结里「跨边取人」的错误（代码级确定性兜底）。

    模型偶发会把某方的英雄写进另一方的栏目（实测：夜魇的大地之灵被写进
    「天辉方的表现」）。提示词约束只能降低概率、不能杜绝，因此这里在生成后
    再校验一遍：把文本按三个栏目切开，检查每个栏目段里出现的**对方**英雄名。

    检测到跨边时，把**写错的那个栏目**整段替换为基于真实数据的兜底描述
    （该方数据正确的概述），而不是把错误内容发出去。另两方不动。
    """
    if not text or not getattr(match, "players", None):
        return text

    radiant = {hero_name(p.hero_id) for p in match.players if p.is_radiant}
    dire = {hero_name(p.hero_id) for p in match.players if not p.is_radiant}
    radiant.discard("")
    dire.discard("")

    sections = _split_sections(text)
    if len(sections) < 2:
        return text  # 切不出栏目，无从校验，原样返回

    def fallback(win: bool, names: set[str]) -> str:
        top = "、".join(sorted(names)[:2]) if names else "核心英雄"
        result = "获胜" if win else "失利"
        return (
            f"本方本场{result}。{top}是队内数据最突出的选手，"
            f"详细数据见上方对战卡片。"
        )

    out: list[tuple[str, str]] = []
    fixed = False
    for title, body in sections:
        if title == _SECTION_TITLES[0]:          # 天辉方的表现
            wrong = {n for n in dire if n and n in body}
            if wrong:
                logger.warning(
                    f"Dota2 录像总结跨边错误：天辉栏目出现夜魇英雄 {sorted(wrong)}，已用兜底文本替换。"
                )
                body = fallback(match.radiant_win, radiant)
                fixed = True
        elif title == _SECTION_TITLES[1]:        # 夜魇方的表现
            wrong = {n for n in radiant if n and n in body}
            if wrong:
                logger.warning(
                    f"Dota2 录像总结跨边错误：夜魇栏目出现天辉英雄 {sorted(wrong)}，已用兜底文本替换。"
                )
                body = fallback(not match.radiant_win, dire)
                fixed = True
        out.append((title, body))

    if not fixed:
        return text
    # 重新拼成「### 标题\n\n正文」的形式，交给后续 format_analysis 统一处理
    return "\n\n".join(f"### {t}\n\n{b}" for t, b in out if b.strip())


_REPLAY_SYSTEM_PROMPT = (
    "你是资深 Dota2 数据分析师，正在用**录像解析数据**复盘一整场比赛。"
    "请按下面四个小标题写一篇复盘，先写小标题、再写正文，"
    "小标题固定为以下四行（原样照抄，不要改写、不要加序号）：\n"
    "### 比赛进程\n"
    "### 团战与关键目标\n"
    "### 优势与失误\n"
    "### 亮眼与糟糕表现\n"
    "四个栏目各写一段、顺序固定。\n"
    "**内容要求（每栏覆盖的维度）**：\n"
    "1. **比赛进程**：按**前期 / 中期 / 后期**的时间线讲局势变化——"
    "前期谁打开局面（一血、对线优劣），中期经济经验曲线何时拉开或胶着，"
    "后期如何定胜负；穿插**关键物品成型时点**（谁在几分钟拿出了什么大件）"
    "与**视野控制**（哪方插眼/排眼更主动）对局势的影响。\n"
    "2. **团战与关键目标**：讲**团战得失**——哪波团战哪方占优、规模多大、"
    "奠定了什么；以及推塔/兵营/肉山/不朽盾这些关键目标的归属与节奏。\n"
    "3. **优势与失误**：复盘双方各自的**优势**（做对了什么：节奏、出装、"
    "控图、抓机会）与**失误**（哪里没做好：避战、对线崩盘、关键团战输掉、"
    "装备成型太慢、视野被压制）。\n"
    "4. **亮眼与糟糕表现**：点名本场**最亮眼的 1-2 人**（结合其百分位表现/"
    "KDA/关键作用，说清为何亮眼）与**最糟糕的 1-2 人**（说清拖后腿在哪）。\n"
    "**分边规则（最高优先级，违反即为错误）**：每个英雄**只属于天辉或夜魇其中一方**。\n"
    "材料里「各玩家录像级表现」已按「天辉方：」「夜魇方：」**分组**列出，"
    "卡片正文也以「## 天辉」「## 夜魇」分段、每段各 5 人。"
    "提到某英雄时，先核对他属于哪一方，**绝不把一方的英雄当成另一方**。\n"
    "**称呼规则（必须遵守）**：一律用**英雄名**称呼玩家，"
    "不要写玩家昵称、账号 ID 或「匿名玩家」。\n"
    "**篇幅（硬约束）**：全文（四个栏目正文相加）**不得超过 1200 字**，"
    "宁精勿滥；每栏 2-4 句即可，不必面面俱到，挑最有信息量的讲。\n"
    "格式要求：\n"
    "- 只用中文；\n"
    "- 提到英雄时用两个星号包住英雄名（**英雄名**），小标题行不要加星号；\n"
    "- 引用材料里真实出现的数据（时间点、经济差、百分位、插眼数等），不要编造；\n"
    "- 不要输出除四个小标题以外的标题、表格或代码块；\n"
    "- 某项数据缺失时据实略过，不要硬凑。"
)


async def summarize_match_replay(host, match, cards: list[str], event=None,
                                 focus_hero: str = "") -> str:
    """用录像解析数据为**具体某一局**写赛后总结；失败返回空串。

    与 ``summarize`` 的关键差异是输入素材：这里把 ``match.replay`` 的
    录像分析数据（经 ``_replay_material`` 格式化）与卡片文字一起喂给模型，
    让总结能讲清比赛的节奏与转折，而不只是复述静态数字。

    本函数假定 ``match.parsed`` 为真（已有录像数据）；调用方负责在解析
    完成后再调用。返回的文本经 ``format_analysis`` 排成三个栏目并按
    ``summary_max_chars`` 控制篇幅。
    """
    if not getattr(host, "enable_llm_summary", True):
        return ""
    if not getattr(match, "parsed", False):
        return ""

    from .hero_names import hero_name_by_id

    def hero_name(hero_id: int) -> str:
        try:
            return hero_name_by_id(hero_id) or f"英雄#{hero_id}"
        except Exception:
            return f"英雄#{hero_id}"

    replay_part = _replay_material(match, hero_name)
    card_part = markdown_to_plain(
        "\n\n".join(c.strip() for c in cards if c and c.strip())
    )
    material = card_part
    if replay_part:
        material = f"{card_part}\n\n【录像解析数据】\n{replay_part}"
    if not material.strip():
        return ""
    if len(material) > _MAX_INPUT_CHARS:
        material = material[:_MAX_INPUT_CHARS].rstrip() + "…"

    try:
        provider = await _resolve_provider(host, event)
    except Exception as exc:
        logger.warning(f"Dota2 总结模型解析异常: {exc}")
        return ""
    if provider is None:
        logger.warning("Dota2 未找到可用对话模型，跳过录像总结。")
        return ""

    system_prompt = _REPLAY_SYSTEM_PROMPT
    focus = (focus_hero or "").strip()
    if focus:
        system_prompt += _FOCUS_PROMPT.format(hero=focus)
    prompt = f"以下是一场 Dota2 比赛的数据与录像解析结果，请按要求写赛后总结：\n\n{material}"

    timeout = getattr(host, "summary_timeout", 180)
    max_chars = getattr(host, "summary_max_chars", 500)
    try:
        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
                request_max_retries=1,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(f"Dota2 录像总结超时（>{timeout}s）。")
        return ""
    except Exception as exc:
        logger.warning(f"Dota2 录像总结失败: {exc}")
        return ""

    try:
        text = _clean(response.completion_text if response else "")
    except Exception as exc:
        logger.warning(f"Dota2 解析录像总结失败: {exc}")
        return ""
    if not text:
        logger.warning("Dota2 录像总结为空。")
        return ""

    # 代码级兜底：剔除「点名了本场不存在的英雄」这类幻觉（复盘按时间线写，
    # 不再有「天辉方的表现/夜魇方的表现」分栏，跨边校验改为「存在性校验」）。
    text = _drop_phantom_heroes(text, match, hero_name)

    return _format_replay(text, cards, max_chars=_REPLAY_MAX_CHARS)


_REPLAY_MAX_CHARS = 1200


def _format_replay(text: str, cards: list[str], max_chars: int) -> str:
    """把录像复盘文本排成卡片可用的形式：保留模型的四个小标题 + 英雄加粗。

    与三点式 ``format_analysis`` 不同，这里**不重新分栏** —— 复盘已由模型按
    「比赛进程 / 团战与关键目标 / 优势与失误 / 亮眼与糟糕表现」四个小标题写好，
    直接保留结构、只对正文加粗英雄名。总超长时按「分栏」从后往前收缩，
    而不是按句子硬砍（避免砍掉后面整栏、留下残缺结构）。
    """
    body = (text or "").strip().replace("`", "")
    if not body:
        return ""
    terms = _match_terms(cards)
    out = _bold_terms(body, terms)
    if not max_chars or len(out) <= max_chars:
        return out

    # 超限时按 ### 分栏整体收缩：逐栏精简，先压最长栏。
    blocks = _split_hash_sections(out)
    if not blocks:
        return _shrink_text(out, max_chars)
    bodies = [b for _, b in blocks]
    budget = max_chars
    # 标题行的开销按每栏「### 标题\\n\\n」≈ 12 字预留
    per_block_overhead = 14 * len(blocks)
    budget = max(max_chars - per_block_overhead, max_chars // 2)
    shrunk = _shrink_section_bodies(bodies, budget)
    return "\n\n".join(
        f"### {t}\n\n{b}" for (t, _), b in zip(blocks, shrunk) if b.strip()
    )


def _split_hash_sections(text: str) -> list[tuple[str, str]]:
    """按行首 ``### xxx`` 把文本切成 [(标题, 正文), ...]；切不出返回空列表。"""
    lines = text.splitlines()
    heads = [i for i, ln in enumerate(lines)
             if ln.lstrip().startswith("###")]
    if not heads:
        return []
    out: list[tuple[str, str]] = []
    for order, start in enumerate(heads):
        end = heads[order + 1] if order + 1 < len(heads) else len(lines)
        title = lines[start].lstrip("#").strip()
        body = "\n".join(lines[start + 1:end]).strip()
        out.append((title, body))
    return out


def _drop_phantom_heroes(text: str, match, hero_name) -> str:
    """剔除复盘里点名了**本场不存在**的英雄的句子（代码级兜底）。

    时间线复盘不再按边分栏，原来的「跨边取人」校验不再适用；但模型仍可能
    编造一个根本没上场的英雄（幻觉）。这里把含「幻影英雄」的**整句**删掉，
    并记日志。本场真实英雄名集合来自 ``match.players``。
    """
    if not text or not getattr(match, "players", None):
        return text
    real = {hero_name(p.hero_id) for p in match.players}
    real.discard("")

    # 已知英雄全集：从英雄表里拿所有中文名，用来识别「文本里提到的是不是英雄」
    try:
        from .hero_names import all_hero_names
        universe = set(all_hero_names())
    except Exception:
        universe = set()

    phantom = {n for n in universe if n and n not in real and n in text}
    if not phantom:
        return text
    logger.warning(f"Dota2 录像复盘出现本场不存在的英雄 {sorted(phantom)}，已剔除相关句子。")

    kept: list[str] = []
    for para in text.split("\n"):
        # 小标题行永远保留
        if para.lstrip().startswith("#"):
            kept.append(para)
            continue
        # 按句切分，删掉含幻影英雄的句子
        sentences = re.split(r"(?<=[。！？；])", para)
        kept_s = [s for s in sentences if not any(p in s for p in phantom)]
        line = "".join(kept_s).strip()
        if line:
            kept.append(line)
    return "\n".join(kept)
