"""Generate GERA presentation PPTX for AI-assisted coding exchange meeting."""

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE
import os

# ── Color palette ──────────────────────────────────────────────────────────
BG_DARK = RGBColor(0x1A, 0x1A, 0x2E)
BG_CARD = RGBColor(0x22, 0x22, 0x3A)
ACCENT_BLUE = RGBColor(0x4E, 0xC9, 0xF0)
ACCENT_GREEN = RGBColor(0x2E, 0xCC, 0x71)
ACCENT_ORANGE = RGBColor(0xF3, 0x9C, 0x12)
ACCENT_RED = RGBColor(0xE7, 0x4C, 0x3C)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT_GRAY = RGBColor(0xBB, 0xBB, 0xCC)
DIM_GRAY = RGBColor(0x88, 0x88, 0x99)


def set_slide_bg(slide, color):
    """Set solid background color for a slide."""
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = color


def add_title_box(slide, text, top=Inches(0.3), height=Inches(0.7),
                  font_size=Pt(28), color=ACCENT_BLUE):
    """Add a styled title textbox to the slide."""
    txBox = slide.shapes.add_textbox(Inches(0.6), top, Inches(8.8), height)
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = text
    p.font.size = font_size
    p.font.color.rgb = color
    p.font.bold = True
    p.font.name = "Microsoft YaHei"
    return txBox


def add_body_text(slide, lines, top=Inches(1.2), left=Inches(0.8),
                  width=Inches(8.4), font_size=Pt(16), line_spacing=1.5,
                  bullet=True, font_name="Microsoft YaHei"):
    """Add bullet-point body text to the slide."""
    txBox = slide.shapes.add_textbox(left, top, width, Inches(5.5))
    tf = txBox.text_frame
    tf.word_wrap = True
    for i, line in enumerate(lines):
        if i == 0:
            p = tf.paragraphs[0]
        else:
            p = tf.add_paragraph()
        if bullet and not line.startswith("  "):
            p.text = line
        else:
            p.text = line
        p.font.size = font_size
        p.font.color.rgb = WHITE
        p.font.name = font_name
        p.space_after = Pt(font_size.pt * (line_spacing - 1))
    return txBox


def add_code_block(slide, code_text, top=Inches(1.5), left=Inches(0.6),
                   width=Inches(8.8), height=Inches(4.8),
                   font_size=Pt(11)):
    """Add a monospace code block with dark card background."""
    # Card background
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor(0x16, 0x16, 0x28)
    shape.line.fill.background()

    # Code text
    txBox = slide.shapes.add_textbox(
        left + Inches(0.3), top + Inches(0.2),
        width - Inches(0.6), height - Inches(0.4)
    )
    tf = txBox.text_frame
    tf.word_wrap = True
    for i, line in enumerate(code_text.split("\n")):
        if i == 0:
            p = tf.paragraphs[0]
        else:
            p = tf.add_paragraph()
        p.text = line
        p.font.size = font_size
        p.font.color.rgb = ACCENT_GREEN
        p.font.name = "Consolas"
        p.space_after = Pt(2)
    return txBox


def add_highlight_box(slide, text, top, left=Inches(0.8),
                      width=Inches(8.4), height=Inches(0.6),
                      font_size=Pt(18), color=ACCENT_ORANGE):
    """Add a highlighted key point box."""
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor(0x2A, 0x2A, 0x1A)
    shape.line.color.rgb = color
    shape.line.width = Pt(1.5)

    tf = shape.text_frame
    tf.word_wrap = True
    tf.paragraphs[0].alignment = PP_ALIGN.CENTER
    p = tf.paragraphs[0]
    p.text = text
    p.font.size = font_size
    p.font.color.rgb = color
    p.font.bold = True
    p.font.name = "Microsoft YaHei"
    return shape


def add_stat_card(slide, value, label, left, top,
                  width=Inches(2.2), height=Inches(1.5),
                  value_color=ACCENT_BLUE):
    """Add a stat card with big number and label."""
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = BG_CARD
    shape.line.fill.background()

    tf = shape.text_frame
    tf.word_wrap = True
    tf.paragraphs[0].alignment = PP_ALIGN.CENTER
    # Value
    p = tf.paragraphs[0]
    p.text = value
    p.font.size = Pt(32)
    p.font.color.rgb = value_color
    p.font.bold = True
    p.font.name = "Consolas"
    p.space_after = Pt(4)
    # Label
    p2 = tf.add_paragraph()
    p2.alignment = PP_ALIGN.CENTER
    p2.text = label
    p2.font.size = Pt(12)
    p2.font.color.rgb = LIGHT_GRAY
    p2.font.name = "Microsoft YaHei"
    return shape


# ════════════════════════════════════════════════════════════════════════════
#  SLIDE BUILDERS
# ════════════════════════════════════════════════════════════════════════════

def slide_cover(prs):
    """Slide 1: Cover page."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # Blank layout
    set_slide_bg(slide, BG_DARK)

    # Main title
    txBox = slide.shapes.add_textbox(Inches(0.8), Inches(1.5), Inches(8.4), Inches(1.2))
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = "当 Workflow 遇见 Agent"
    p.font.size = Pt(40)
    p.font.color.rgb = ACCENT_BLUE
    p.font.bold = True
    p.font.name = "Microsoft YaHei"
    p.alignment = PP_ALIGN.CENTER

    # Subtitle
    p2 = tf.add_paragraph()
    p2.alignment = PP_ALIGN.CENTER
    p2.text = "GERA 自动代码评审系统的设计与优化"
    p2.font.size = Pt(24)
    p2.font.color.rgb = WHITE
    p2.font.name = "Microsoft YaHei"

    # Divider line
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(3), Inches(3.2), Inches(4), Pt(2)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = ACCENT_BLUE
    shape.line.fill.background()

    # Event info
    txBox2 = slide.shapes.add_textbox(Inches(0.8), Inches(3.6), Inches(8.4), Inches(1.0))
    tf2 = txBox2.text_frame
    tf2.word_wrap = True
    p3 = tf2.paragraphs[0]
    p3.text = "AI辅助编码、代码审查应用经验交流会"
    p3.font.size = Pt(16)
    p3.font.color.rgb = LIGHT_GRAY
    p3.font.name = "Microsoft YaHei"
    p3.alignment = PP_ALIGN.CENTER
    p4 = tf2.add_paragraph()
    p4.alignment = PP_ALIGN.CENTER
    p4.text = "2026-05-28"
    p4.font.size = Pt(14)
    p4.font.color.rgb = DIM_GRAY
    p4.font.name = "Microsoft YaHei"


def slide_problem(prs):
    """Slide 2: Problem and motivation."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG_DARK)

    add_title_box(slide, "问题与动机")

    add_body_text(slide, [
        "人工代码评审的三大痛点：",
        "",
        "  1. 耗时 — 大量变更堆积，评审响应慢",
        "  2. 遗漏 — 疲劳导致深层问题被忽略",
        "  3. 标准不一致 — 不同评审者关注点不同",
        "",
        "",
        "一个想法：",
        "  能不能让 AI 自动帮我看代码？",
        "",
        "",
        "但关键问题来了：",
        "  AI 应该在流程中扮演什么角色？",
    ], font_size=Pt(16))

    add_highlight_box(slide,
        "全交给 AI？全不用 AI？还是各取所长？",
        top=Inches(5.6), color=ACCENT_ORANGE)


def slide_design_philosophy(prs):
    """Slide 3: Core design philosophy — Workflow + Agent."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG_DARK)

    add_title_box(slide, "核心设计思想 — Workflow + Agent")

    add_highlight_box(slide,
        "原则：能确定的就确定，需要判断的才用 AI",
        top=Inches(1.1), color=ACCENT_GREEN)

    # Architecture diagram as code block
    code = (
        "┌─────────────────────────────────────────────────────────────┐\n"
        "│                    GERA 单次评审流程                         │\n"
        "├─────────────────────────────────────────────────────────────┤\n"
        "│                                                             │\n"
        "│  [确定性 Workflow]              [AI Agent]                   │\n"
        "│                                                             │\n"
        "│  1. Gerrit API 轮询             9. Claude Code 评审          │\n"
        "│  2. 过滤: 自己/in_flight/已审      - 理解代码语义            │\n"
        "│  3. git fetch + diff               - 发现深层 bug            │\n"
        "│  4. 创建 worktree                   - 评估问题是否解决        │\n"
        "│  5. SVN 合规检查                                                │\n"
        "│  6. 写入 diff 文件               ← 唯一的 AI 环节 →          │\n"
        "│  7. 复制 Skill 文件                                            │\n"
        "│  8. 生成 CLAUDE.md             10. 解析 JSON 输出            │\n"
        "│                                11. 格式化评审消息             │\n"
        "│                                12. POST 到 Gerrit            │\n"
        "│                                13. 清理 worktree             │\n"
        "│                                                             │\n"
        "│  ~90% 确定性代码                ~10% AI Agent                 │\n"
        "└─────────────────────────────────────────────────────────────┘"
    )
    add_code_block(slide, code, top=Inches(1.9), font_size=Pt(10.5))


def slide_no_agent(prs):
    """Slide 4: What doesn't need Agent."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG_DARK)

    add_title_box(slide, "哪些不需要 Agent？")

    add_body_text(slide, [
        "轮询调度 & 过滤去重",
        "  while True + time.sleep(600) + 三层幂等过滤",
        "  确定性逻辑，AI 做这个是浪费钱",
        "",
        "Git 操作",
        "  shallow clone → 按需 fetch → 命名 ref → worktree 隔离",
        "  纯工程问题，不需要 AI 判断",
        "",
        "Gerrit REST API 交互",
        "  GET /changes → POST /review，标准 HTTP 客户端",
        "",
        "SVN 合规检查",
        "  检查设计文档/自测报告/需求归档 — 纯数据校验",
        "  下载 Excel 读列 J，匹配报告名称",
        "",
    ], font_size=Pt(14))

    add_highlight_box(slide,
        "结论：确定性的事交给确定性代码，快、准、省",
        top=Inches(5.6), color=ACCENT_GREEN)


def slide_agent_where(prs):
    """Slide 5: Where Agent is used."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG_DARK)

    add_title_box(slide, "Agent 用在哪里？")

    add_body_text(slide, [
        "唯一的 AI 环节：理解代码语义，发现深层问题",
        "",
        "为什么用 Claude Code CLI 而非 API？",
        "  - Claude Code 能直接 cat 源文件，自主浏览代码",
        "  - 原生加载 CLAUDE.md 中的 Skill，无需嵌入 prompt",
        "  - API 做不到这些 — 你得把所有文件塞进 prompt",
        "",
        "5 个维度并行评审，各司其职：",
        "  1. 问题解决评估 — 变更是否真正修复了 bug？",
        "  2. 安全 — 缓冲区溢出、注入、不安全函数",
        "  3. 内存管理 — 泄漏、use-after-free、空指针",
        "  4. 编码规范 — Allman 大括号、行长度、变量初始化",
        "  5. 逻辑正确性 — 越界、返回值、循环终止",
        "  6. 业务逻辑 — 领域特定问题",
    ], font_size=Pt(14))

    add_highlight_box(slide,
        "每个维度 = 一个独立的 Claude 子进程 + 专属 Skill 文件",
        top=Inches(5.6), color=ACCENT_BLUE)


def slide_cost_optimization(prs):
    """Slide 6: Cost optimization — Prompt diet."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG_DARK)

    add_title_box(slide, "成本优化 — Prompt 瘦身之路")

    # Evolution timeline as code block
    code = (
        "v1  diff 全文嵌入 prompt\n"
        "    └─ 问题：上下文爆炸，内网模型处理慢\n"
        "\n"
        "v4  diff 写文件，Claude 自己 cat 读取\n"
        "    但 Skill 全文仍嵌入 prompt\n"
        "    └─ 问题：每个维度 ~30KB Skill 文本\n"
        "\n"
        "v5  Skill 文件夹化 + CLAUDE.md 原生加载\n"
        "    Skill .md 文件复制到 worktree\n"
        "    CLAUDE.md 用 @引用，Claude Code 自动发现\n"
        "    └─ prompt 仅 ~400 字节\n"
        "\n"
        "    ┌──────────────────────────────────────┐\n"
        "    │  30,000 bytes  →  400 bytes           │\n"
        "    │  ██████████████  ▏                    │\n"
        "    │        99% 缩减                       │\n"
        "    └──────────────────────────────────────┘"
    )
    add_code_block(slide, code, top=Inches(1.2), font_size=Pt(12))

    add_highlight_box(slide,
        "核心思路：prompt 每个字都是钱，能省则省",
        top=Inches(5.8), color=ACCENT_ORANGE)


def slide_cost_combo(prs):
    """Slide 7: Cost optimization — combination tactics."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG_DARK)

    add_title_box(slide, "成本优化 — 组合拳")

    add_body_text(slide, [
        "规则引擎前置过滤（不消耗 token）",
        "  17 条正则规则：strcpy、sprintf、Allman 大括号...",
        "  LLM 只需关注规则引擎抓不到的深层问题",
        "",
        "结构化 JSON 输出",
        "  强制输出 JSON，不输出废话",
        "  减少 thinking 块，_strip_thinking() 过滤分析文本",
        "",
        "评审结果缓存（TTL 24h）",
        "  change_id:revision 为 key，避免重复评审",
        "",
        "幂等过滤",
        "  REVIEW_TAG + revision 号双重检查",
        "  _in_flight 集合防止并发重复提交",
        "",
        "多维度降级策略",
        "  部分维度失败 → 标注\"未完成\"，不丢弃已有结果",
        "  全部失败 → 跳过提交，等下轮重试",
    ], font_size=Pt(13.5))


def slide_performance(prs):
    """Slide 8: Performance optimization — 3-tier concurrency."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG_DARK)

    add_title_box(slide, "性能优化 — 三层并发模型")

    code = (
        "self._executor (ThreadPoolExecutor, max_workers=2)\n"
        "  │\n"
        "  ├─ _review_task → _review_change\n"
        "  │    └─ _invoke_claude_dimensions\n"
        "  │         └─ ThreadPoolExecutor(max_workers=6)\n"
        "  │              ├─ _run(问题评估)   ┐\n"
        "  │              ├─ _run(安全)       │\n"
        "  │              ├─ _run(内存管理)   ├─ 6 个 Claude 子进程\n"
        "  │              ├─ _run(编码规范)   │   Semaphore(4) 限流\n"
        "  │              ├─ _run(逻辑)       │\n"
        "  │              └─ _run(业务逻辑)   ┘\n"
        "  │\n"
        "  └─ _review_task (第 2 个 review)\n"
        "       └─ ...同上...\n"
        "\n"
        "┌────────┬──────────┬──────────────────────────┐\n"
        "│  层级   │   限制   │         机制              │\n"
        "├────────┼──────────┼──────────────────────────┤\n"
        "│ Review 任务│    2   │ ThreadPoolExecutor(2)    │\n"
        "│ 维度任务│    6   │ ThreadPoolExecutor(6)    │\n"
        "│ Claude │    4   │ threading.Semaphore(4)   │\n"
        "└────────┴──────────┴──────────────────────────┘\n"
        "\n"
        "Git 优化：shallow clone(depth=10) + 按需 fetch"
    )
    add_code_block(slide, code, top=Inches(1.2), font_size=Pt(10.5))


def slide_pitfalls(prs):
    """Slide 9: Architecture pitfalls and fixes."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG_DARK)

    add_title_box(slide, "架构优化 — 踩过的坑")

    add_body_text(slide, [
        "坑 1：FETCH_HEAD 竞态",
        "  多线程 git fetch 互相覆盖 FETCH_HEAD → diff 错乱",
        "  解决：fetch 到命名 ref refs/review/{id}/{rev}",
        "",
        "坑 2：共享工作区冲突",
        "  多个 Claude 进程 cat 同一文件 → 读到错误版本",
        "  解决：每个 change 创建独立 worktree",
        "",
        "坑 3：Skill 嵌入 prompt 膨胀",
        "  13 个 Skill 全文嵌入 → 30KB/维度，上下文不够用",
        "  解决：Skill 文件夹化 + CLAUDE.md @引用原生加载",
        "",
        "坑 4：executor.shutdown 永久阻塞",
        "  KeyboardInterrupt 后 shutdown(wait=True) 挂死",
        "  解决：wait=False + 日志 RotatingFileHandler 防膨胀",
    ], font_size=Pt(13))


def slide_summary(prs):
    """Slide 10: Summary + Q&A."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, BG_DARK)

    add_title_box(slide, "经验总结")

    add_body_text(slide, [
        "经验 1：Agent 是调味料，不是主食",
        "  放在真正需要判断的地方（代码语义理解）",
        "  确定性的事（轮询/Git/API）用确定性代码做",
        "",
        "经验 2：Prompt 是成本",
        "  每个字都是钱，能省则省",
        "  Skill 文件夹化 → 99% 缩减，质量反而提升",
        "",
        "经验 3：并发是工程问题",
        "  不是 AI 问题，用传统方法（Semaphore/命名ref/worktree）解决",
        "  AI 解决的是\"理解\"，不是\"调度\"",
    ], font_size=Pt(15))

    # Q&A section
    txBox = slide.shapes.add_textbox(Inches(0.8), Inches(5.0), Inches(8.4), Inches(1.2))
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = "Q & A"
    p.font.size = Pt(36)
    p.font.color.rgb = ACCENT_BLUE
    p.font.bold = True
    p.font.name = "Microsoft YaHei"
    p.alignment = PP_ALIGN.CENTER


# ════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    prs = Presentation()
    prs.slide_width = Inches(10)
    prs.slide_height = Inches(7.5)

    slide_cover(prs)
    slide_problem(prs)
    slide_design_philosophy(prs)
    slide_no_agent(prs)
    slide_agent_where(prs)
    slide_cost_optimization(prs)
    slide_cost_combo(prs)
    slide_performance(prs)
    slide_pitfalls(prs)
    slide_summary(prs)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "gera_presentation.pptx")
    prs.save(out_path)
    print(f"PPT saved to: {out_path}")
    print(f"Total slides: {len(prs.slides)}")


if __name__ == "__main__":
    main()
