#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5r 日志排查一次性信息收集脚本（SKILL.md §0-§4 + §5.2 的机械化部分）。

用法（Win，任意 Python≥3.8，纯 stdlib，不需要 maa）：
    python 5r_collect.py 20260831            # 全量：拉日志+三份报告数据+归档截图+maafw核实
    python 5r_collect.py 20260831 --fast     # 跳过 maafw 扫描（只要三份报告数据）
    python 5r_collect.py 20260831 --no-scp   # 只读分析，不往 debug/mac log/<date>/ 拷文件

输出分 [0]..[9] 节，[2]-[5] 的表格可直接贴进 report.md（三份报告），
[9] 列出必须人工完成的部分（门4 逐题校验 / 读截图 / 异常 trace / 写 report.md）。

依赖：ssh/scp 免密到 imac@100.116.176.34（remote 模式常态）。
中文经 Mac 的坑全部绕开：远端脚本纯 ASCII（json ensure_ascii 传参 + unicode_escape 回传文件名）。
"""
import argparse
import collections
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import urllib.request

# ---------------------------------------------------------------- 常量（与 SKILL.md 保持一致）
MAC = "imac@100.116.176.34"
MAC_REPO = "/Users/imac/dev/Maa_MHXY_MG"
CLI = "http://100.116.176.34:5090"

# §5.2 ROUTINE：设计内出口/瞬态自恢复/已知问题截图 —— 计数进报告但不归档
ROUTINE = {
    '活动-运镖-开始-点击参加', '宝图完成判断-再次检查', '藏宝图-背包使用',
    '出售阵法', '师门任务-任务分支-装备提交确认-输入验证码',
    '点击打工', '队长踢人-选人', '点击副本-开始战斗',
}

# §4 报告一「典型基线留意线」：超过标 *
LIM = {'shuangbei': 60, 'fuli_qiandao': 120, 'shimen_renwu': 900, 'shimen_renwu_new': 1000,
       'yunbiao_renwu2': 1000, 'baotu_renwu': 1000, '打开大地图_69副本': 30, 'mijing_renwu': 2100,
       'sanjieqiyuan': 200, 'zhengli_baibao': 200, 'jiayuan_zhengli': 150, 'huoli': 120,
       'zhanghao_xinxi': 60, 'jialan': 150, 'baitanchushou': 240, 'kejuxiangshi': 300,
       'huoli_linshifu': 120, 'tuoyinjiance': 900}

# 异常短=疑似假成功（§5 ③/⑨/⑰/⑲）：低于标 ⚠
SHORT = {'kejuxiangshi': 60, 'mijing_renwu': 200, 'wabaotu_qingli': 30,
         'shimen_renwu': 60, 'shimen_renwu_new': 60, 'wabaotu': 30}

# team 阶段 + solo 真干核实要数的 maafw 节点（name, 是否前缀匹配）
NODES = [
    ('fuben69new-副本完成-退出', False),      # 0  副本真完成（5 本应 5 hit）
    ('抓鬼一轮完成', False),                    # 1  捉鬼轮次（N 轮应 N hit）
    ('捉鬼-结束', False),                       # 2
    ('点击副本-开始战斗', False),               # 3  战斗佐证
    ('战斗中-等待', True),                      # 4  战斗佐证（前缀）
    ('点击押送普通镖_确定', False),            # 5  运镖真 3 镖（每号应 3）
    ('海底秘境-指定关卡结束任务', False),      # 6  秘境第25关停
    ('mijing_shibai', False),                   # 7
    ('mijing_tongguan', False),                 # 8
    ('藏宝图-主界面使用', False),               # 9  挖宝图真挖（>0）
    ('活动-科举乡试-开始', False),             # 10 科举真开门（每号 ≥1）
    ('fuben69new-进入', True),                  # 11 进本佐证（前缀）
]
NODE_IDX = {name: i for i, (name, _) in enumerate(NODES)}

VERIFY_TASKS = ['yunbiao_renwu2', 'mijing_renwu', 'wabaotu_qingli', 'kejuxiangshi']

RE_LINE_TS = re.compile(r'^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\] ?(.*)$')
KEYWORD = re.compile(r'!!!|Traceback|Error|异常|持续死亡|画面未变|未就绪|L3 实例级|cancel')


def t2s(t):
    h, m, s = t.split(':')
    return int(h) * 3600 + int(m) * 60 + int(s)


def hr(title):
    print('\n' + '=' * 16 + ' ' + title + ' ' + '=' * 16)


# ---------------------------------------------------------------- 远端执行
def ssh_py(script: str, timeout=600):
    """ssh 到 Mac 跑一段 python（script 必须纯 ASCII），返回 stdout str。"""
    p = subprocess.run(['ssh', '-o', 'ConnectTimeout=10', MAC, 'python3'],
                       input=script.encode('ascii'), capture_output=True, timeout=timeout)
    if p.returncode != 0:
        print(f'!! ssh python3 失败 rc={p.returncode}: {p.stderr.decode("utf-8", "replace")[:300]}')
        return ''
    return p.stdout.decode('utf-8', 'replace')


def remote_available():
    p = subprocess.run(['ssh', '-o', 'ConnectTimeout=8', MAC, 'true'], capture_output=True, timeout=20)
    return p.returncode == 0


# ---------------------------------------------------------------- 编排日志解析
def parse_orch(path):
    """解析一份编排日志 → dict（矩阵/窗口/异常/tag表）。"""
    job = {'path': path, 'file': os.path.basename(path),
           'config': 'team1(默认)', 'done': False, 'tags': [], 'connects': [],
           'roles': [], 'solo_lists': {}, 'matrix': collections.defaultdict(dict),
           'order': [], 'timeouts': [], 'keywords': [], 'fuben': [], 'zhuogui': [],
           'solo_win': collections.defaultdict(list), 'start_ts': None, 'end_ts': None}
    cur_team = {}          # label -> start time（在跑未完的 team 步骤）
    cur_solo = {}          # (role,task) -> start time
    last_ts = None
    for line in open(path, encoding='utf-8', errors='replace'):
        m = RE_LINE_TS.match(line)
        if not m:
            continue
        ts = m.group(2)
        last_ts = ts
        if job['start_ts'] is None:
            job['start_ts'] = ts
        body = m.group(3)
        if '--config' in body:
            cm = re.search(r'--config (\S+?)（', body) or re.search(r'--config (\S+)', body)
            if cm:
                job['config'] = cm.group(1)
        if '已连接 127.0.0.1' in body:
            cm = re.search(r'\[(\d+)\] (\S+) 已连接 (127\.0\.0\.1:\d+)', body)
            if cm:
                job['connects'].append((int(cm.group(1)), cm.group(2), cm.group(3)))
        tm = re.search(r'\[tag\] (\S+?)\((\S+?)\) uuid=(\w+)', body)
        if tm:
            job['tags'].append((tm.group(1), tm.group(2), tm.group(3)))
        if '=== 全部完成 ===' in body:
            job['done'] = True
        if '对象:' in body or '对象：' in body:
            seg = body.split('对象:')[-1] if '对象:' in body else body.split('对象：')[-1]
            for _, n in re.findall(r'(\d+):([^\s,，;：]+)', seg):
                if n not in job['roles']:
                    job['roles'].append(n)
        lm = re.search(r'\[(\S+)\] 任务列表: (\[.*\])', body)
        if lm:
            job['solo_lists'][lm.group(1)] = lm.group(2)
        # solo 起止
        sm = re.search(r'>>> \[([^\]]+)\] 单人 (\S+)（≤([\d.]+)s?）', body)
        if sm:
            cur_solo[(sm.group(1), sm.group(2))] = ts
        dm = re.search(r'<<< \[([^\]]+)\] 单人 (\S+) 完成（用时 ([\d.]+)s）', body)
        if dm:
            role, task = dm.group(1), dm.group(2)
            st = cur_solo.pop((role, task), None)
            if st:
                job['solo_win'][(role, task)].append((st, ts))
            if task not in job['matrix']:
                job['order'].append(task)
            job['matrix'][task][role] = int(float(dm.group(3)))
            continue
        xm = re.search(r'!!! \[([^\]]+)\] 单人 (\S+) 超时 ([\d.]+)s', body)
        if xm:
            role, task = xm.group(1), xm.group(2)
            st = cur_solo.pop((role, task), None)
            if st:
                job['solo_win'][(role, task)].append((st, ts))
            if task not in job['matrix']:
                job['order'].append(task)
            job['matrix'][task][role] = -int(float(xm.group(3)))
            job['timeouts'].append((ts, role, task))
        # team 步骤（fuben69new 子本 / zhuoguirenwu）
        zm = re.search(r'>>> 队长 (fuben69new) (\S+)（≤', body)
        if zm:
            cur_team[f'{zm.group(1)} {zm.group(2)}'] = ts
        gm = re.search(r'>>> 队长 (zhuoguirenwu)（(\d+)轮鬼）', body)
        if gm:
            cur_team[f'{gm.group(1)}({gm.group(2)}轮)'] = ts
            job['zhuogui_expected'] = int(gm.group(2))
        for pat in (r'<<< 队长 (fuben69new) ((?!zhuoguirenwu)\S+) 完成（用时 ([\d.]+)s）',
                    r'<<< 队长 (zhuoguirenwu)(（\d+轮鬼）?) ?完成（用时 ([\d.]+)s）'):
            fm = re.search(pat, body)
            if fm:
                label = fm.group(2) if fm.group(1) == 'fuben69new' else 'zhuogui'
                key = f'{fm.group(1)} {fm.group(2)}' if fm.group(1) == 'fuben69new' else \
                    f"zhuoguirenwu({job.get('zhuogui_expected', '?')}轮)"
                st = cur_team.pop(key, None)
                job['fuben'].append({'label': label, 'kind': fm.group(1),
                                     'start': st or ts, 'end': ts,
                                     'dur': int(float(fm.group(3)))})
                break
        if KEYWORD.search(body):
            job['keywords'].append((ts, body.strip()))
    job['end_ts'] = last_ts
    # 未收口的窗口（cancelled job）：end 兜底为文件末时间
    for (role, task), st in cur_solo.items():
        job['solo_win'][(role, task)].append((st, last_ts))
        job['keywords'].append((st, f'!! 窗口未收口: [{role}] {task}（job 中断？）'))
    if not job['roles']:
        for t in job['matrix'].values():
            for r in t:
                if r not in job['roles']:
                    job['roles'].append(r)
        job['roles'] = job['roles'][:5]
    if not job['roles'] and job['tags']:        # launch 类无单人行的：用 [tag] 的角色名顺序兜底
        job['roles'] = [r for r, _a, _u in job['tags']][:5]
    return job


def print_matrix(job):
    print(f"\n### job {job['file']}（config={job['config']}，{'✅ 全部完成' if job['done'] else '❌ 未到全部完成'}）")
    if job['tags']:
        print('uuid 归号表：' + '，'.join(f'{r}({a})={u}' for r, a, u in job['tags']))
    if job['solo_lists']:
        lists = set(job['solo_lists'].values())
        print(f"SOLO 列表：{sorted(lists)[0] if len(lists) == 1 else str(job['solo_lists'])[:400]}")
    roles = job['roles'] or ['?1', '?2', '?3', '?4', '?5']
    print('| 任务 | ' + ' | '.join(roles) + ' |')
    print('|---' * (len(roles) + 1) + '|')
    flagged = []
    for t in job['order']:
        cells = []
        for r in roles:
            v = job['matrix'][t].get(r)
            if v is None:
                cells.append('—')
            elif v < 0:
                cells.append(f'X{-v}')
                flagged.append(f'{r} {t} X{-v}(墙钟超时)')
            else:
                mark = '*' if (LIM.get(t) and v > LIM[t]) else ''
                if SHORT.get(t) and v < SHORT[t]:
                    mark += '⚠'
                if mark:
                    flagged.append(f'{r} {t} {v}s{mark}')
                cells.append(f'{v}{mark}')
        print(f'| {t} | ' + ' | '.join(cells) + ' |')
    print('（X=墙钟超时，*=超典型基线留意线，⚠=异常短疑似假成功，—=未跑到）')
    if flagged:
        print('**异常标记点名**：' + '；'.join(flagged))
    if job['timeouts']:
        print(f"超时行：{job['timeouts']}")
    if job['keywords']:
        kw = job['keywords']
        # 画面未变×N 这类重复行去重计数
        cnt = collections.Counter(k[1] for k in kw)
        shown = [f'{t} {c}' for c, t in
                 [(v, k[:110]) for k, v in cnt.most_common(12)]]
        print(f'关键词命中（去重后 top12，共 {len(kw)} 行）：')
        for s in shown:
            print('  - ' + s)


# ---------------------------------------------------------------- 远端 maafw/loguru/on_error 扫描
REMOTE_SCAN = r'''
import json,os,re,glob,sys
CFG=json.loads("""@@CFG@@""")
D=CFG['debug_dir']
pref='['+CFG['date']+' '
def first_ts(f):
    try:
        with open(f,encoding='utf-8',errors='replace') as fh:
            for line in fh:
                m=re.match(r'^\[(\d{4}-\d{2}-\d{2}) ([0-9:.]+)\]',line)
                if m: return m.group(1)+' '+m.group(2)[:8]
    except Exception: pass
    return '?'
print('###CUST')
try:
    hitre=re.compile(CFG['re_hit']); missre=re.compile(CFG['re_miss'])
    hits=[];miss=[];fuzzy=0
    for line in open(CFG['custom_log'],encoding='utf-8',errors='replace'):
        m=hitre.search(line)
        if m: hits.append(m.group(1)); continue
        m=missre.search(line)
        if m: miss.append(m.group(1)); continue
        if CFG['kw_fuzzy'] in line: fuzzy+=1
    print('HIT\t%d\t%d'%(len(hits),len(set(hits))))
    print('MISS\t%d\t%d'%(len(miss),len(set(miss))))
    print('FUZZY\t%d'%fuzzy)
    for q in dict.fromkeys(miss): print('NEWQ\t'+q)
except FileNotFoundError:
    print('NOFILE')
print('###OE')
oe=D+'/on_error'; td=D+'/timeout'
for sub in (oe,td):
    try: fs=sorted(f for f in os.listdir(sub) if f.startswith(CFG['date_dot']))
    except Exception: continue
    for f in fs:
        ts,_,node=f.partition('_')
        node=node.rsplit('.',1)[0]
        print('%s\t%s\t%s'%( 'OE' if sub==oe else 'TO', ts, node.encode('unicode_escape').decode('ascii')))
print('###MAAFW')
date_dot=CFG['date_dot']
files=sorted(glob.glob(D+'/maafw.bak.'+date_dot+'*.log'))
files.append(D+'/maafw.log')
allb=sorted(glob.glob(D+'/maafw.bak.*.log'))
prev=[f for f in allb if os.path.basename(f) < 'maafw.bak.'+date_dot]
if prev: files=[prev[-1]]+files
pats=[]
for i,nd in enumerate(CFG['nodes']):
    esc=re.escape(nd['name'])
    pats.append((i, re.compile(r'reco hit \[result\.name='+esc+(r'[^\]]*\]' if nd['prefix'] else r'\]'))))
entry_re=re.compile(r'"entry":"([^"]+)"'); uuid_re=re.compile(r'"uuid":"(\w+)"')
tx_re=re.compile(r'\[Tx(\d+)\]')
want_entries=set(CFG['want_entries'])
for f in files:
    print('C\t%s\t%s'%(os.path.basename(f), first_ts(f)))
    try: fh=open(f,encoding='utf-8',errors='replace')
    except Exception: continue
    for line in fh:
        if not line.startswith(pref): continue
        t=line[len(pref):len(pref)+8]
        if 'reco hit [result.name=' in line:
            for i,p in pats:
                if p.search(line):
                    tx=tx_re.search(line)
                    print('H\t%s\t%s\t%d'%(t, tx.group(1) if tx else '?', i)); break
        elif 'task start: ' in line:
            e=entry_re.search(line)
            if e and e.group(1) in want_entries:
                tx=tx_re.search(line); u=uuid_re.search(line)
                print('S\t%s\t%s\t%s\t%s'%(t, tx.group(1) if tx else '?', e.group(1), u.group(1) if u else ''))
    fh.close()
print('###END')
'''


def remote_scan(date_hyphen, want_entries):
    cfg = {
        'debug_dir': MAC_REPO + '/debug/debug',
        'date': date_hyphen,
        'date_dot': date_hyphen.replace('-', '.'),
        'custom_log': f'{MAC_REPO}/debug/custom/{date_hyphen}.log',
        're_hit': r'缓存精确命中：《(.+?)》',
        're_miss': r'缓存未命中，调用AI：《(.+?)》',
        'kw_fuzzy': '缓存模糊命中',
        'nodes': [{'name': n, 'prefix': pfx} for n, pfx in NODES],
        'want_entries': sorted(set(want_entries) | set(VERIFY_TASKS)),
    }
    script = REMOTE_SCAN.replace('@@CFG@@', json.dumps(cfg, ensure_ascii=True))
    out = ssh_py(script)
    if not out:
        return {}
    res = {'cust': [], 'newq': [], 'oe': [], 'maafw': [], 'cov': []}
    sec = None
    for line in out.splitlines():
        if line.startswith('###'):
            sec = line[3:]
            continue
        if sec == 'CUST':
            res['cust'].append(line.split('\t'))
        elif sec == 'OE':
            parts = line.split('\t')
            if len(parts) >= 3:
                # 远端按 unicode_escape 传节点名（ASCII 通道），本地反转义回中文
                parts[2] = parts[2].encode('ascii').decode('unicode_escape')
                res['oe'].append(parts)
        elif sec == 'MAAFW':
            if line.startswith('C\t'):
                res['cov'].append(line.split('\t')[1:3])
            else:
                res['maafw'].append(line.split('\t'))
    return res


# ---------------------------------------------------------------- 报告二 / 报告三
def report_account(path, date_hyphen):
    rows = []
    for line in open(path, encoding='utf-8', errors='replace'):
        m = re.search(r'\[([^\]]+)\]\s*账号:\s*(127\.0\.0\.1:\d+)\s*\|\s*金币:\s*(-?\d+)\s*\|\s*银币:\s*(-?\d+)', line)
        if m:
            rows.append((m.group(1), m.group(2), int(m.group(3)), int(m.group(4))))
    by = collections.defaultdict(list)
    for ts, port, g, s in rows:
        by[port].append((ts, g, s))
    print('| 账号(port) | 上次金币 | 本次金币 | Δ金币 | 上次银币 | 本次银币 | Δ银币 | 本次时间 |')
    print('|---' * 8 + '|')
    warn = []
    for port in sorted(by, key=lambda p: int(p.split(':')[1])):
        recs = by[port]
        day = [r for r in recs if r[0].startswith(date_hyphen)]
        if not day:
            continue
        last = day[-1]
        prev = recs[recs.index(last) - 1] if recs.index(last) > 0 else None
        if prev is None:
            print(f'| {port} | — | {last[1]} | — | — | {last[2]} | — | {last[0][11:19]}（无上次基线） |')
            continue
        dg, ds = last[1] - prev[1], last[2] - prev[2]
        d = lambda n: ('+' + str(n)) if n >= 0 else str(n)
        print(f'| {port} | {prev[1]} | {last[1]} | {d(dg)} | {prev[2]} | {last[2]} | {d(ds)} | {last[0][11:19]} |')
        if ds < 0:
            warn.append(f'{port} 银币净负 {ds}（归因排除法：可能是玩家自行消费，如实报告）')
    print('（Δ = 本次 − 上次；上次基线时间若无标注即前一日同任务落账）')
    if warn:
        print('负值点名：' + '；'.join(warn))


def report_keju(cache_path, tiku_path, date_hyphen, cust):
    print('**门0（cache 命中统计，loguru）**：')
    if cust == [['NOFILE']]:
        print('- 当天 loguru 日志不存在（没跑 keju？）')
    else:
        hit = miss = uniq_h = uniq_m = fuzzy = 0
        newq = []
        for parts in cust:
            if parts[0] == 'HIT':
                hit, uniq_h = int(parts[1]), int(parts[2])
            elif parts[0] == 'MISS':
                miss, uniq_m = int(parts[1]), int(parts[2])
            elif parts[0] == 'FUZZY':
                fuzzy = int(parts[1])
            elif parts[0] == 'NEWQ':
                newq.append(parts[1] if len(parts) > 1 else '')
        tot = hit + miss
        print(f'```')
        print(f'答题事件总数: {tot}  |  cache 命中: {hit}（独立题 {uniq_h}）  |  AI 新答: {miss}（独立题 {uniq_m}）')
        print(f'命中率(按事件): {hit / tot * 100:.0f}%' if tot else '命中率: N/A')
        print(f'```')
        if fuzzy:
            print(f'!! 模糊命中 {fuzzy} 次（已禁用功能不应出现，按异常上报）')

    d = json.load(open(cache_path, encoding='utf-8'))
    norm = lambda q: re.sub(r'[\s　，。、,.?!:;（）()【】]+', '', q or '')
    new = {k: v for k, v in d.items() if v.get('ts', '')[:10] == date_hyphen}
    print(f'\nkeju_ai_cache 总条数: {len(d)} | 当日新入库(ts=={date_hyphen}): {len(new)}')
    hard = [(k, v) for k, v in new.items()
            if v.get('answer') not in list(v.get('options', {}).values())]
    print(f'**门1（answer∉options 硬错误）: {len(hard)}**')
    for k, v in hard[:10]:
        print(f'   ! answer={v.get("answer")!r} opts={list(v.get("options",{}).values())} Q: {(v.get("question") or k)[:50]}')
    qn = collections.defaultdict(set)
    for k, v in d.items():
        a = v.get('answer')
        if a:
            qn[norm(v.get('question', k))].add(a)
    conf = {q: a for q, a in qn.items() if len([x for x in a if x]) > 1}
    print(f'**门2（同题多答案冲突，全量 {len(d)} 题）: {len(conf)}**')
    for q, a in list(conf.items())[:10]:
        print(f'   ? {list(a)} Q: {q[:50]}')
    import ast
    tk = {}
    if os.path.exists(tiku_path):
        for line in open(tiku_path, encoding='utf-8'):
            mm = re.match(r'\x22(.+?)\x22:\s*(\[.+\])', line.strip())
            if mm:
                try:
                    tk[norm(mm.group(1))] = set(ast.literal_eval(mm.group(2)))
                except Exception:
                    pass
    diff = [(k, v.get('answer'), tk[norm(v.get('question', k))]) for k, v in new.items()
            if norm(v.get('question', k)) in tk and v.get('answer') not in tk[norm(v.get('question', k))]]
    print(f'**门3（tiku 已有该题但 AI 答案不符）: {len(diff)}**（tiku 解析 {len(tk)} 题）')
    for k, a, t in diff[:10]:
        print(f'   != AI:{a!r} tiku:{t} Q:{k[:40]}')
    print(f'\n**门4（知识性逐题人工校验对象，{len(new)} 题）——必须人工判 ✅/❌/⚠，禁止问用户**：')
    for k, v in sorted(new.items(), key=lambda kv: kv[1].get('ts', '')):
        opts = list(v.get('options', {}).values())
        print(f'- Q: {(v.get("question") or k)[:60]}')
        print(f'  AI答: {v.get("answer")!r}   选项: {opts}')
    print('（错的给正解并改 cache —— cache 存答案文本；权威文件在 Mac 侧，改完须同步。学术争议题保留 AI 答案+备注。）')


# ---------------------------------------------------------------- team/solo 真干核实
def verify(jobs, scan):
    hits = [p for p in scan['maafw'] if p[0] == 'H']       # H, t, Tx, nodeIdx
    starts = [p for p in scan['maafw'] if p[0] == 'S']     # S, t, Tx, entry, uuid
    by_node_time = collections.defaultdict(list)           # idx -> [(t,tx)]
    for _, t, tx, i in hits:
        by_node_time[int(i)].append((t, tx))

    def count_window(idx, s, e, txs=None):
        return [1 for t, tx in by_node_time.get(idx, []) if s <= t <= e and (txs is None or tx in txs)]

    def tx_of_start(entry, wstart, tol=10):
        """编排窗口起点 ±tol 秒内该 entry 的 task start → 其 Tx（L3 重连换 uuid 后 tag 失效的兜底）。"""
        best, bd = None, None
        for _, t, tx, en, _u in starts:
            if en != entry:
                continue
            dd = abs(t2s(t) - t2s(wstart))
            if dd <= tol and (bd is None or dd < bd):
                best, bd = tx, dd
        return {best} if best else None

    for job in jobs:
        if not job['fuben'] and not job['solo_win']:
            continue
        print(f"\n### job {job['file']}（{job['config']}）")
        if job['fuben']:
            print('| team 步骤 | 耗时 | 完成退出hit | 战斗等待 | 开始战斗 | 判定 |')
            print('|---' * 5 + '|')
            for w in job['fuben']:
                s, e = w['start'], w['end']
                if w['kind'] == 'fuben69new':
                    n0 = len(count_window(0, s, e))
                    ok = '✅' if n0 == 1 else '❌ 假成功?'
                    print(f"| fuben69new {w['label']} | {w['dur']}s | {n0}/1 "
                          f"| {len(count_window(4, s, e))} | {len(count_window(3, s, e))} | {ok} |")
            zg = [w for w in job['fuben'] if w['kind'] == 'zhuoguirenwu']
            for w in zg:
                exp = job.get('zhuogui_expected', 0)
                n1 = len(count_window(1, w['start'], w['end']))
                n2 = len(count_window(2, w['start'], w['end']))
                ok = '✅' if (exp and n1 == exp and n2 >= 1) else '❌'
                print(f"| zhuoguirenwu({exp}轮) | {w['dur']}s | 轮hit {n1}/{exp} + 结束 {n2} "
                      f"| {len(count_window(4, w['start'], w['end']))} | — | {ok} |")
        # solo 真干核实（运镖3镖/秘境结局/挖图/科举开门），Tx 用窗口起点对齐 task start 归号
        rows = []
        for role in job['roles']:
            yb = job['solo_win'].get((role, 'yunbiao_renwu2'))
            if yb:
                s, e = yb[-1]
                txs = tx_of_start('yunbiao_renwu2', s)
                n = len(count_window(5, s, e, txs))
                rows.append((role, '运镖确定', f'{n}/3', '✅' if n == 3 else f'⚠ {"Tx归号失败,窗内合计" if txs is None else ""}'))
            mj = job['solo_win'].get((role, 'mijing_renwu'))
            if mj:
                s, e = mj[-1]
                txs = tx_of_start('mijing_renwu', s)
                o = {name: len(count_window(idx, s, e, txs)) for name, idx in
                     [('25关停', 6), ('shibai', 7), ('tongguan', 8)]}
                tag = next((k for k, v in o.items() if v), '无结局节点')
                rows.append((role, '秘境结局', tag + ' ' + str(o), '✅' if o['25关停'] or o['tongguan'] else '⚠'))
            wb = job['solo_win'].get((role, 'wabaotu_qingli'))
            if wb:
                s, e = wb[-1]
                txs = tx_of_start('wabaotu_qingli', s)
                n = len(count_window(9, s, e, txs))
                rows.append((role, '挖图使用', f'>0? {n}', '✅' if n > 0 else '❌ 0=假成功'))
            kj = job['solo_win'].get((role, 'kejuxiangshi'))
            if kj:
                s, e = kj[-1]
                txs = tx_of_start('kejuxiangshi', s)
                n = len(count_window(10, s, e, txs))
                rows.append((role, '科举开门', f'≥1? {n}', '✅' if n >= 1 else '❌ 0=没答(⑨)'))
        if rows:
            print('| 号 | 核实项 | 证据 | 判定 |')
            print('|---' * 3 + '|')
            for r in rows:
                print('| ' + ' | '.join(r) + ' |')


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('date', help='YYYYMMDD')
    ap.add_argument('--fast', action='store_true', help='跳过 maafw 扫描/核实（只要报告一二三数据）')
    ap.add_argument('--no-scp', action='store_true', help='不往 debug/mac log/<date>/ 拷文件')
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    d = args.date
    date_hyphen = f'{d[:4]}-{d[4:6]}-{d[6:]}'
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    arch = os.path.join(repo, 'debug', 'mac log', d)
    remote = remote_available()
    if not remote:
        print('!! Mac ssh 不通：remote-only 部分将跳过（本地 debug/run_5r 若有当日日志仍可出报告一）')

    # [0] job 列表
    hr(f'[0] job 列表（cli_server） {d}')
    jobs_api = []
    try:
        with urllib.request.urlopen(CLI + '/jobs', timeout=15) as r:
            jobs_api = json.load(r).get('jobs', [])
        with urllib.request.urlopen(CLI + '/health', timeout=15) as r:
            print('health:', r.read().decode())
    except Exception as e:
        print('cli_server 不可达：', e)
    for j in jobs_api:
        st = datetime.datetime.fromtimestamp(j['started'])
        if st.strftime('%Y%m%d') == d:
            print(f"  {j['job_id']}  {j['mode']:8s} {j['state']:9s} exit={j.get('exit')}  "
                  f"{st.strftime('%H:%M:%S')}→{datetime.datetime.fromtimestamp(j['ended']).strftime('%H:%M:%S')}")

    # [1] 拉编排日志
    hr(f'[1] 编排日志归档 → debug/mac log/{d}/')
    logs = []
    if remote:
        if not args.no_scp:
            os.makedirs(arch, exist_ok=True)
            p = subprocess.run(['scp', '-q',
                                f'{MAC}:{MAC_REPO}/debug/run_5r/run_5r_{d}_*.log', arch],
                               capture_output=True, timeout=120)
            if p.returncode:
                print('!! scp 编排日志失败:', p.stderr.decode('utf-8', 'replace')[:200])
        logs = sorted(glob.glob(os.path.join(arch, f'run_5r_{d}_*.log')))
        if not logs and args.no_scp:
            print('（--no-scp：跳过拉取，本地无当日日志）')
    if not logs:
        logs = sorted(glob.glob(os.path.join(repo, 'debug', 'run_5r', f'run_5r_{d}_*.log')))
        print(f'本地 debug/run_5r 模式：{len(logs)} 份' if logs else '!! 无当日编排日志（本地/归档都没有）')
    jobs = [parse_orch(p) for p in logs]
    for j in jobs:
        print(f"  {j['file']}  config={j['config']}  {'全部完成' if j['done'] else '未完成'}  "
              f"roles={','.join(j['roles'])}")

    # [2] 报告一
    hr('[2] 报告一：耗时矩阵（逐 job）')
    for j in jobs:
        print_matrix(j)
    print('\n（⚠ 单元格 = 异常短疑似假成功，须按 §4.1 三步定位法去 maafw trace；X = 墙钟超时同样必查根因）')

    # [3] 报告二
    hr('[3] 报告二：账号金币·银币变化量')
    acct_path = os.path.join(arch, 'account_info.log')
    if remote:
        if not args.no_scp:
            os.makedirs(arch, exist_ok=True)
            subprocess.run(['scp', '-q', f'{MAC}:{MAC_REPO}/agent/data/account_info.log', acct_path],
                           capture_output=True, timeout=60)
        elif os.path.exists(os.path.join(repo, 'agent', 'data', 'account_info.log')):
            acct_path = os.path.join(repo, 'agent', 'data', 'account_info.log')
    if os.path.exists(acct_path):
        report_account(acct_path, date_hyphen)
        print(f'（数据源：{acct_path}）')
    else:
        print('!! account_info.log 拉取失败/不存在，报告二无法出')

    # [4][5] 报告三
    scan = {}
    if remote:
        scan = remote_scan(date_hyphen, want_entries=[e for j in jobs for e in
                                                      (t for (r, t) in j['solo_win'])] + ['start'])
    hr('[4] 报告三·门0：keju cache 命中统计')
    cache_path = os.path.join(arch, 'keju_ai_cache.json')
    if remote and not args.no_scp:
        subprocess.run(['scp', '-q', f'{MAC}:{MAC_REPO}/agent/data/keju_ai_cache.json', cache_path],
                       capture_output=True, timeout=60)
    if scan:
        report_keju(cache_path if os.path.exists(cache_path)
                    else os.path.join(repo, 'agent', 'data', 'keju_ai_cache.json'),
                    os.path.join(repo, 'agent', 'custom', 'recognition', 'tiku.txt'),
                    date_hyphen, scan.get('cust', []))
    else:
        print('!! 远端扫描不可用，报告三数据缺')

    # [6] on_error / timeout
    hr('[6] on_error / timeout 分布 + 归档（ROUTINE 过滤）')
    if scan:
        oe = [p for p in scan['oe'] if p[0] == 'OE']
        to = [p for p in scan['oe'] if p[0] == 'TO']
        cnt = collections.Counter(p[2] for p in oe)
        print(f'on_error 当日 {len(oe)} 张，按节点：')
        for name, n in cnt.most_common():
            print(f'  {n:3d}× {name}' + ('（ROUTINE，设计内/已知）' if name in ROUTINE else ''))
        print(f'timeout 当日 {len(to)} 张（全部属异常取证，应读画面）：')
        keep = []
        for parts in oe:
            node = parts[2]
            if node not in ROUTINE:
                keep.append(f"{parts[1]}_{node}.png")
        if not args.no_scp and (keep or to):
            os.makedirs(os.path.join(arch, 'on_error'), exist_ok=True)
            ok = fail = 0
            for fname in keep:
                r = subprocess.run(['scp', '-q',
                                    f"{MAC}:'{MAC_REPO}/debug/debug/on_error/{fname}'",
                                    os.path.join(arch, 'on_error', fname)], capture_output=True)
                ok += r.returncode == 0
                fail += r.returncode != 0
            for parts in to:
                fname = f"{parts[1]}_{parts[2]}.png"
                r = subprocess.run(['scp', '-q',
                                    f"{MAC}:'{MAC_REPO}/debug/debug/timeout/{fname}'",
                                    os.path.join(arch, 'on_error', fname)], capture_output=True)
                ok += r.returncode == 0
                fail += r.returncode != 0
            print(f'已归档非 ROUTINE on_error {len(keep)} 张 + timeout {len(to)} 张'
                  f'（成功 {ok} 失败 {fail}）→ {arch}\\on_error\\')
            for f in keep + [f"{p[1]}_{p[2]}.png" for p in to]:
                print('  ' + os.path.join(arch, 'on_error', f))
    else:
        print('（跳过：远端不可用）')

    # [7][8] maafw 覆盖 + 核实
    if not args.fast and scan:
        hr('[7] maafw bak 覆盖表（trace 异常时选 bak 用）')
        for name, first in scan['cov']:
            print(f'  {name:50s} first={first}')
        hr('[8] team 阶段 + solo 关键任务真干核实（节点级硬证据）')
        verify(jobs, scan)
    else:
        hr('[7]/[8] maafw 覆盖 + 真干核实')
        print('（--fast 或远端不可用：跳过。异常单元格须人工按 §4.1 trace）')

    # [9] 人工部分
    hr('[9] ✋ 以下必须人工完成（脚本无法替代）')
    manual = []
    new_cnt = 0
    if scan and scan.get('cust') != [['NOFILE']]:
        for parts in scan['cust']:
            if parts[0] == 'MISS':
                pass
        try:
            cj = json.load(open(cache_path if os.path.exists(cache_path)
                                else os.path.join(repo, 'agent', 'data', 'keju_ai_cache.json'),
                                encoding='utf-8'))
            new_cnt = sum(1 for v in cj.values() if v.get('ts', '')[:10] == date_hyphen)
        except Exception:
            pass
    if new_cnt:
        manual.append(f'门4：逐题知识性校验上面列出的 {new_cnt} 道新题（直接判，别问用户）；'
                      f'错题改 Mac 侧 keju_ai_cache.json 并同步，报告贴错题清单表（无错也写"0 错"）')
    if scan:
        to = [p for p in scan['oe'] if p[0] == 'TO']
        nonroutine = [p for p in scan['oe'] if p[0] == 'OE' and p[2] not in ROUTINE]
        if to or nonroutine:
            shots = [os.path.join(arch, 'on_error', f"{p[1]}_{p[2]}.png") for p in nonroutine + to]
            shots = [s for s in shots if os.path.exists(s)] or [os.path.join(arch, 'on_error')]
            manual.append(f'读归档截图（timeout {len(to)} + 非 ROUTINE on_error {len(nonroutine)} 张）：'
                          f'用图像分析工具逐张读"卡在哪一帧"——\n    ' + '\n    '.join(shots))
    anom = []
    for j in jobs:
        for t, roles_d in j['matrix'].items():
            for r, v in roles_d.items():
                if v is not None and (v < 0 or (SHORT.get(t) and v < SHORT[t])):
                    anom.append(f"{j['file']} {r} {t} {'X超时' if v < 0 else str(v) + 's⚠异常短'}")
        if not j['done']:
            anom.append(f"{j['file']} 未到「全部完成」（cancelled/中断，去 maafw 看最后状态）")
    if anom:
        manual.append('对以下异常逐个定位根因到节点级（按 §4.1：编排窗口→task start 拿 Tx→筛 Tx 命中序列）：\n    - '
                      + '\n    - '.join(anom))
    manual.append(f'写 report.md → {os.path.join(arch, "report.md")}（当天多 job 一份分章节：'
                  f'每 job 三份报告表格 + team 核实表 + 根因（节点级 trace + 截图画面结论）'
                  f'+ on_error 分布表 + 结论与待办）')
    manual.append('新故障模式 → 更新 SKILL.md 已知问题表 §5 + 写 memory')
    for i, m in enumerate(manual, 1):
        print(f'{i}. {m}')
    print('\n（硬性规则回顾：三份报告全表格 / 门4不问用户 / 多 job 全覆盖 / 异常到节点级 / team 真完成核实 / 归档才算完）')


if __name__ == '__main__':
    main()
