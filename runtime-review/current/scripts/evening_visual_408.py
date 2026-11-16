"""Validate and render source-bound 408 reading cards."""
from collections import defaultdict
from html import escape
from pathlib import Path
import re

from public_scene_408 import SCHEMA as SCENE_SCHEMA, public_scene

SCHEMA = 'cs408-evening-reading-v2'
DAILY_READING_SCHEMA = 'cs408-daily-reading-v1'


def validate(plan, manifest, handoff):
    def check(ok, message):
        if not ok:
            raise ValueError(message)
    check(plan.get('subject') == '408', '复盘科目错误')
    check(plan.get('status') == 'READY', '网页尚未完成')
    check(set(plan) == {'schema', 'subject', 'status', 'study_date', 'source_commit',
          'handoff_sha256', 'duration_seconds', 'cards', 'coverage'}, '阅读版字段错误；不接受题目或教师材料')
    for key in ('study_date', 'source_commit', 'handoff_sha256'):
        check(plan.get(key) == manifest[key], '晚间plan绑定错误：' + key)
    check(plan.get('duration_seconds') == 900, '阅读目标须为900秒')
    cards = plan.get('cards')
    check(isinstance(cards, list) and 1 <= len(cards) <= 30, '复盘卡片数量无效')
    captures = {r['capture_id'] for r in handoff['captures']}
    points = set(handoff['knowledge_points'])
    sources, seconds = {}, 0
    for card in cards:
        check(isinstance(card, dict) and set(card) == {'card_id', 'concept_key', 'source_capture_ids',
              'title', 'breakpoint', 'explanation', 'takeaway', 'visual', 'budget_seconds'}, '卡片字段错误')
        cid = card['card_id']
        check(isinstance(cid, str) and cid.startswith('R408-') and cid not in sources, '卡片ID重复或无效')
        check(card['concept_key'] in points, '卡片知识点不在当天范围')
        refs = card['source_capture_ids']
        check(isinstance(refs, list) and refs and all(isinstance(x, str) for x in refs)
              and len(refs) == len(set(refs)) and set(refs) <= captures, '卡片引用了范围外Capture')
        sources[cid] = set(refs)
        for key, limit in [('title', 50), ('breakpoint', 160), ('explanation', 500), ('takeaway', 100)]:
            value = card[key]
            check(isinstance(value, str) and 0 < len(value.strip()) <= limit, '卡片文本为空或超出极简预算：' + key)
        visual = card['visual']
        check(isinstance(visual, dict) and set(visual) == {'kind', 'lines'}, '视觉字段错误')
        check(visual['kind'] in {'flow', 'compare', 'tree'}, '视觉类型无效')
        lines = visual['lines']
        check(isinstance(lines, list) and 2 <= len(lines) <= 10 and all(isinstance(x, str) and 0 < len(x.strip()) <= 160 for x in lines), '视觉关系行无效')
        budget = card['budget_seconds']
        check(type(budget) is int and 10 <= budget <= 300, '阅读预算无效')
        seconds += budget
    check(600 <= seconds <= 900, '阅读预算须为10至15分钟')
    coverage = plan.get('coverage')
    check(isinstance(coverage, list) and len(coverage) == len(captures), '全天coverage数量不一致')
    seen = set()
    for row in coverage:
        check(isinstance(row, dict) and set(row) == {'capture_id', 'disposition', 'card_ids', 'reason'}, 'coverage字段错误')
        cid = row['capture_id']
        check(cid in captures and cid not in seen, 'coverage重复或范围错误')
        seen.add(cid)
        refs = row['card_ids']
        check(isinstance(refs, list) and all(isinstance(x, str) for x in refs) and len(refs) == len(set(refs)), 'coverage卡片引用错误')
        check(set(refs) == {key for key, values in sources.items() if cid in values}, 'coverage与卡片未双向绑定')
        check(row['disposition'] in {'included', 'already_clear'} and bool(refs) == (row['disposition'] == 'included'), '薄弱点须纳入卡片；已清楚项可不展示')
        check(isinstance(row['reason'], str) and row['reason'].strip(), 'coverage缺少依据')
    return seconds


def render(plan):
    cards = []
    for card in plan['cards']:
        visual = card['visual']
        lines = [escape(x) for x in visual['lines']]
        if visual['kind'] == 'tree':
            diagram = '<pre>' + '\n'.join(lines) + '</pre>'
        else:
            separator = '<span class="arrow">↓</span>' if visual['kind'] == 'flow' else ''
            diagram = '<div class="' + visual['kind'] + '">' + separator.join('<div class="node">' + x + '</div>' for x in lines) + '</div>'
        cards.append('<section><h2>' + escape(card['title']) + '</h2><p class="muted">' + escape(card['breakpoint']) + '</p>' + diagram + '<p>' + escape(card['explanation']) + '</p><p class="takeaway">' + escape(card['takeaway']) + '</p></section>')
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>408晚间复盘</title><style>body{margin:0;background:#f4f5f2;color:#202d29;font:18px/1.7 system-ui,sans-serif}main{max-width:840px;margin:auto;padding:36px 20px}h1{font-size:28px}h2{font-size:22px;margin-top:0}section{background:white;border:1px solid #dce2dc;border-radius:16px;padding:24px;margin:24px 0}.muted{color:#58665f;font-size:16px}.node,pre{background:#eef3ef;padding:12px 16px;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere}.flow{display:grid;gap:4px}.arrow{text-align:center;color:#50755e}.compare{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.takeaway{border-left:4px solid #50755e;padding-left:14px;font-weight:600}pre{font-size:17px;overflow:auto}</style>'
            '<main><h1>408晚间复盘 · ' + escape(plan['study_date']) + '</h1><p class="muted">约15分钟 · 直接阅读 · 阅读不等于独立掌握</p>' + ''.join(cards) + '</main></html>').encode()


def daily_scene(card, source_commit):
    mechanism = card['mechanism']
    if not isinstance(mechanism, dict) or set(mechanism) - {'title', 'nodes', 'edges', 'states', 'rows', 'initial_state'}:
        raise ValueError('日终卡片只接收公开机制图的对象、关系和状态：' + card['card_id'])
    return {**mechanism, 'schema': SCENE_SCHEMA,
        'identity': card['card_id'], 'source_version': source_commit,
        'view': 'protected', 'hint_level': 0}


def validate_daily(plan, manifest, handoff):
    def check(ok, message):
        if not ok:
            raise ValueError(message)

    def has_text(value):
        return isinstance(value, str) and bool(value.strip())

    fields = {'schema', 'subject', 'status', 'study_date', 'source_commit', 'handoff_sha256', 'cards', 'coverage'}
    check(set(plan) == fields and (plan.get('schema'), plan.get('subject'), plan.get('status')) ==
          (DAILY_READING_SCHEMA, '408', 'READY'), '日终阅读计划字段或状态无效')
    check(handoff.get('authoring_mode') == 'local', '日终阅读需要当日已完成的本地学习材料')
    for key in ('study_date', 'source_commit', 'handoff_sha256'):
        check(plan[key] == manifest[key], '日终阅读计划范围或版本不符：' + key)
    captures = {r['capture_id']: r['quality_status'] for r in handoff['captures']}
    points = set(handoff['knowledge_points'])
    check(isinstance(plan['cards'], list), '日终知识卡须为数组')
    covered, ids = defaultdict(set), set()
    for card in plan['cards']:
        check(isinstance(card, dict) and set(card) == {'card_id', 'concept_key', 'source_capture_ids', 'title',
              'definition', 'conditions', 'conclusion', 'secondary_conclusions', 'mechanism'}, '日终知识卡字段无效')
        cid = card['card_id']
        check(isinstance(cid, str) and re.fullmatch(r'R408-[a-zA-Z0-9-]+', cid) and cid not in ids, '日终知识卡ID无效')
        ids.add(cid)
        code = card['concept_key']
        check(isinstance(code, str) and re.fullmatch(r'(DS|CO|OS|CN)\d{2}-\d{2}', code) and code in points,
              '知识卡超出当天考点：' + cid)
        refs = card['source_capture_ids']
        check(isinstance(refs, list) and refs and all(isinstance(ref, str) and ref in captures
              and captures[ref] != 'user_removed' for ref in refs) and len(set(refs)) == len(refs), '知识卡缺少当日依据：' + cid)
        for ref in refs:
            covered[ref].add(cid)
        check(all(has_text(card[k]) for k in ('title', 'definition', 'conclusion')), '知识卡缺定义或核心结论：' + cid)
        check(isinstance(card['conditions'], list) and all(map(has_text, card['conditions'])), '知识卡条件无效：' + cid)
        secondary = card['secondary_conclusions']
        check(isinstance(secondary, list) and all(isinstance(row, dict) and set(row) == {'conclusion', 'condition'}
              and all(map(has_text, row.values())) for row in secondary), '二级结论缺少适用条件：' + cid)
        if card['mechanism'] is not None:
            scene = public_scene(daily_scene(card, plan['source_commit']))
            check(any(node['detail'] for node in scene['nodes']) or len(scene['states']) > 1,
                  '机制图需有可点说明或实际状态变化：' + cid)
    coverage = plan['coverage']
    check(isinstance(coverage, list) and len(coverage) == len(captures), '日终复盘未覆盖全部当日快存')
    seen = set()
    for row in coverage:
        check(isinstance(row, dict) and set(row) == {'capture_id', 'card_ids', 'reason'}, '日终复盘覆盖字段无效')
        ref = row['capture_id']
        check(ref in captures and ref not in seen and has_text(row['reason']), '日终复盘覆盖重复、越界或缺依据')
        seen.add(ref)
        cards = row['card_ids']
        check(isinstance(cards, list) and all(isinstance(cid, str) for cid in cards)
              and len(cards) == len(set(cards)) and set(cards) == covered[ref], '知识卡和当天快存未双向绑定')
    return None  # Reading has no question session or scheduled duration.


def render_daily(plan):
    from render_interactive_408 import compose_fragment

    cards, groups = [], defaultdict(list)
    names = {'DS': '数据结构', 'CO': '计算机组成原理', 'OS': '操作系统', 'CN': '计算机网络'}
    for card in sorted(plan['cards'], key=lambda card: list(names).index(card['concept_key'][:2])):
        cid = escape(card['card_id'])
        title = escape(card['title'])
        sections = ['<h3>定义</h3><p>' + escape(card['definition']) + '</p>']
        if card['conditions']:
            sections.append('<h3>适用条件</h3><ul>' + ''.join('<li>' + escape(v) + '</li>' for v in card['conditions']) + '</ul>')
        sections.append('<h3>核心结论</h3><p>' + escape(card['conclusion']) + '</p>')
        if card['secondary_conclusions']:
            sections.append('<h3>二级结论</h3><dl>' + ''.join('<dt>' + escape(r['conclusion']) + '</dt><dd>' +
                            escape(r['condition']) + '</dd>' for r in card['secondary_conclusions']) + '</dl>')
        diagram = compose_fragment(daily_scene(card, plan['source_commit']))[0] if card['mechanism'] is not None else ''
        groups[card['concept_key'][:2]].append(f'<a href="#{cid}">{title}</a>')
        cards.append(f'<article id="{cid}" tabindex="-1"><div class="card-text"><h2>{title}</h2>{"".join(sections)}</div>'
                     f'{diagram}<div class="tools"><button type="button" data-copy>复制本卡</button>'
                     f'<label>本卡备注<textarea rows="2" data-note aria-label="{title}的备注"></textarea></label></div></article>')
    directory = ''.join(f'<section><h2>{names[key]}</h2>{"".join(links)}</section>' for key, links in groups.items())
    template = (Path(__file__).resolve().parents[1] / 'support/interactive-408/daily-reading.html').read_text(encoding='utf-8')
    parts = {'__DAY__': escape(plan['study_date']), '__DIRECTORY__': directory,
             '__CARDS__': ''.join(cards) or '<p>当天记录没有新增可编成知识卡的内容。</p>'}
    return re.sub('|'.join(parts), lambda match: parts[match[0]], template).encode()
