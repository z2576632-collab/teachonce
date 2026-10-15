"""Demo scenario data — "personal course-material organizing".

What this file is: the INPUT data of the demo. Three structured
demonstrations of how a human completed the task, one natural-language
correction, and four held-out evaluation cases.

What this file is NOT: skill content is never written here. The skill
(trigger conditions, decision rules, exceptions) is produced by the real
LLM at demo runtime from these demonstrations. Held-out cases are kept
OUT of the learning input — they are only used for v1-vs-v2 evaluation.
"""

from __future__ import annotations

from typing import Any

SKILL_NAME = "课程资料整理"

# ---------------------------------------------------------------------------
# Demonstration 1 — end-of-semester big cleanup
# ---------------------------------------------------------------------------

DEMO_1: dict[str, Any] = {
    "task_goal": "把下载文件夹里一学期积累的课程资料整理到 文档/课程/ 下",
    "context": (
        "学期末整理。下载文件夹里混着 6 门课的资料：课件 ppt/pdf、作业 document、"
        "参考论文、考试相关材料，文件名混乱且中英文混杂。"
    ),
    "steps": [
        {
            "action": "通读全部文件，先列出这学期有哪些课程",
            "target": "下载文件夹",
            "decision": "先按课程名建顶层文件夹，而不是按文件类型（pdf/ppt/docx）分",
            "rationale": "以后找资料都是按『哪门课』找，从不按格式找",
        },
        {
            "action": "为每门课建立 课件/ 作业/ 参考/ 三个子文件夹",
            "target": "各课程文件夹",
            "decision": "固定三类子目录，不发明更多分类",
            "rationale": "作业要交、课件要复习、参考资料很少翻，三类够用",
        },
        {
            "action": "把『选课指南.pdf』『校历.pdf』等跨课程文件放进 _公共/",
            "target": "_公共/",
            "decision": "不属于任何单门课程的文件集中放 _公共/，不硬塞进某一门课",
            "rationale": "这些文件明年还会用到，混进某门课里以后就找不到了",
        },
        {
            "action": "作业出现 v1/v2/final 多个版本时，只把终稿留在 作业/，旧版移入 作业/历史版本/",
            "target": "各课程 作业/",
            "decision": "旧版本不删除，放入 历史版本/ 子目录",
            "rationale": "担心以后要对比修改痕迹；删了找不回来",
        },
        {
            "action": "『未命名1.pdf』『扫描件_0830.pdf』等看不出归属的文件统一放 _待整理/",
            "target": "_待整理/",
            "decision": "不猜测归属，集中放 _待整理/ 并记下文件名清单",
            "rationale": "猜错比不整理更麻烦，之后可以逐个确认",
        },
    ],
    "key_judgments": [
        {
            "situation": "文件属于多门课或不属于任何一门课",
            "decision": "放 _公共/",
            "why": "跨课程复用，硬归到某一门以后找不到",
        },
        {
            "situation": "作业存在多个历史版本",
            "decision": "终稿留在 作业/，旧版进 作业/历史版本/，不删除",
            "why": "用户明确要求保留旧版本",
        },
        {
            "situation": "无法判断文件归属",
            "decision": "放 _待整理/，不猜",
            "why": "错误归档的代价高于暂缓归档",
        },
    ],
    "outcome": "全部资料按课程归位；7 个无法归属的文件在 _待整理/ 等待确认",
    "tags": ["课程资料", "文件整理", "归档规则"],
}

# ---------------------------------------------------------------------------
# Demonstration 2 — a new course reusing the existing structure
# ---------------------------------------------------------------------------

DEMO_2: dict[str, Any] = {
    "task_goal": "整理《数据结构》新课的资料，放入 文档/课程/数据结构/",
    "context": "新课资料：课件 ppt、作业 pdf、leetcode 练习代码文件、课程群通知截图。",
    "steps": [
        {
            "action": "为《数据结构》建立 课件/ 作业/ 参考/ 子文件夹",
            "target": "文档/课程/数据结构/",
            "decision": "沿用和旧课程完全一致的三类结构",
            "rationale": "所有课程结构一致，找东西的手感才一样",
        },
        {
            "action": "leetcode 练习代码按周次归入 作业/练习/",
            "target": "作业/练习/",
            "decision": "代码练习算作业的一部分",
            "rationale": "练习和作业一起提交，放一起方便对照",
        },
        {
            "action": "课程群通知截图按日期排进 课件/",
            "target": "课件/",
            "decision": "通知截图归课件，不单独建文件夹",
            "rationale": "通知属于课程进度信息，和课件一起看正合适",
        },
        {
            "action": "把一份《考研数据结构真题》挑出来放 参考/",
            "target": "参考/",
            "decision": "不是课程作业、只是参考材料的一律放 参考/",
            "rationale": "避免把非作业内容混进 作业/",
        },
    ],
    "key_judgments": [
        {
            "situation": "新课程的资料类型与已有课程相似",
            "decision": "沿用同一套目录结构，不重新设计",
            "why": "保持全部课程一致，降低以后查找的心智负担",
        }
    ],
    "outcome": "数据结构资料归位；没有发明新的目录结构",
    "tags": ["课程资料", "新课", "复用结构"],
}

# ---------------------------------------------------------------------------
# Demonstration 3 — exam-related materials
# ---------------------------------------------------------------------------

DEMO_3: dict[str, Any] = {
    "task_goal": "整理《英语》课的资料，并把考试周要用的材料归到一起",
    "context": "英语课资料：课件、作文作业、口语考试录音、期末复习提纲、往年真题。",
    "steps": [
        {
            "action": "英语课常规资料归入 文档/课程/英语/ 的 课件/ 作业/ 参考/",
            "target": "文档/课程/英语/",
            "decision": "沿用课程三件套结构",
            "rationale": "与其它课程保持一致",
        },
        {
            "action": "把期末复习提纲、往年真题从 参考/ 里拿出来，单独建 考试资料/",
            "target": "考试资料/",
            "decision": "考试直接相关的资料独立放 考试资料/，不和 参考/ 混放",
            "rationale": "复习周只翻这个文件夹；平时不用在一堆参考里翻找",
        },
        {
            "action": "口语考试练习录音放进 考试资料/口语练习/",
            "target": "考试资料/口语练习/",
            "decision": "备考用的录音跟随考试资料分类",
            "rationale": "它是为了考试准备的",
        },
    ],
    "key_judgments": [
        {
            "situation": "资料与考试直接相关",
            "decision": "从 参考/ 移出，放入独立的 考试资料/",
            "why": "考试周集中查找，效率最高",
        }
    ],
    "outcome": "英语资料归位；考试资料集中，考前可直接进入复习",
    "tags": ["课程资料", "考试资料", "录音"],
}

SEED_DEMONSTRATIONS: list[dict[str, Any]] = [DEMO_1, DEMO_2, DEMO_3]

# Independent initial-version review cases, separate from later v1/v2 comparison.
BASELINE_CASES: list[dict[str, Any]] = [
    {
        "situation": "《天文学》资料包包含本课程课件，需要建立课程目录。",
        "expected_decision": "按课程名称建立目录，课件放入课程下的课件目录。",
        "keywords": ["课程"],
    },
    {
        "situation": "一份校历适用于本学期全部课程。",
        "expected_decision": "跨课程资料放入 _公共/。",
        "keywords": ["_公共"],
    },
]

# ---------------------------------------------------------------------------
# The new task the AI meets AFTER learning (cross-task transfer)
# ---------------------------------------------------------------------------

NEW_TASK = (
    "整理《学术写作》课的讲座资料包，放进 文档/课程/学术写作/："
    "3 个讲座录音 mp3、3 份对应的转写稿 docx、以及讲座用的 slides。"
)

# ---------------------------------------------------------------------------
# User correction after reviewing how the AI would handle the new task
# ---------------------------------------------------------------------------

CORRECTION_FEEDBACK = (
    "录音文件不要随便放进 参考/ 或 考试资料/。"
    "以后所有课程录音都集中放到该课程自己的 录音/ 文件夹里；"
    "如果有对应的转写稿，要跟录音放在同一个 录音/ 文件夹里，"
    "并且两份文件名用一样的序号开头，方便播放时对照着看。"
)

# ---------------------------------------------------------------------------
# Held-out cases — NEVER used during learning; only for v1-vs-v2 evaluation
# ---------------------------------------------------------------------------

HELD_OUT_CASES: list[dict[str, Any]] = [
    {
        "situation": "《线性代数》期末考试前，手上有《期末复习提纲》和两份往年真题，需要归入课程文件夹。",
        "expected_decision": "放入该课程的 考试资料/ 文件夹，与课件、作业分开。",
        "keywords": ["考试资料"],
    },
    {
        "situation": "《人际沟通》课的期末论文有两个版本：初稿.docx 和 终稿_final.docx。",
        "expected_decision": "终稿留在 作业/，初稿移入 作业/历史版本/，不删除。",
        "keywords": ["历史版本"],
    },
    {
        "situation": "整理《学术写作》课资料时，发现 3 个讲座录音 mp3 和 3 份对应的转写稿 docx。",
        "expected_decision": "录音与转写稿一起放入该课程的 录音/ 文件夹，按相同序号配对。",
        "keywords": ["录音", "转写稿"],
    },
    {
        "situation": "下载文件夹里有一个『扫描件_20260901.pdf』，通读后仍看不出属于哪门课、是什么资料。",
        "expected_decision": "放入 _待整理/ 并记录文件名，不猜测归属。",
        "keywords": ["_待整理"],
    },
]
