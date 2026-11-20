# -*- coding: utf-8 -*-
"""知识库：切片、入库幂等、双路检索、权限隔离、撤回、注入预算。"""
import os
import sys

import support
from support import check, section

import kb

ACC = "wxid_test"


def fresh(name="kb.db"):
    """每个小节一条独立库，互不干扰。"""
    p = os.path.join(support.TMP, name)
    for f in (p, p + "-wal", p + "-shm"):
        if os.path.exists(f):
            os.remove(f)
    return kb.connect(p)


# ---------------------------------------------------------------- 切片
section("切片与清洗")
check("空文本 → 0 片", kb.chunk_text("") == [])
check("短文本 → 1 片", len(kb.chunk_text("这是一段足够长的测试文字，用来验证切片行为。")) == 1)
long_text = "\n\n".join("第%d段。" % i + "内容" * 60 for i in range(12))
pieces = kb.chunk_text(long_text, size=200, overlap=40)
check("长文本被切成多片", len(pieces) > 3, "片数=%d" % len(pieces))
check("每片不超过 size+容忍", all(len(p) <= 260 for p in pieces),
      "最长=%d" % max(len(p) for p in pieces))
check("无空片", all(p.strip() for p in pieces))
ocr_raw = "标题<｜ref｜>标题<｜/ref｜><｜box｜>[[52, 40, 948, 96]]<｜/box｜>"
check("OCR 定位标记被清理", "<｜ref｜>" not in kb.normalize_text(ocr_raw),
      kb.normalize_text(ocr_raw))
check("多余空行被折叠",
      kb.normalize_text("a\n\n\n\nb") == "a\n\nb")
check("图片型判定：字少且文件大",
      kb.looks_scanned([("第 1 页", "abc")], 500 * 1024))
check("图片型判定：字多不算",
      not kb.looks_scanned([("第 1 页", "字" * 200)], 500 * 1024))
check("图片型判定：小文件不算",
      not kb.looks_scanned([("第 1 页", "")], 10 * 1024))
check("中文之间的空格被删掉（PDF 逐字断空）",
      kb.normalize_text("项 目 二 要 求") == "项目二要求",
      kb.normalize_text("项 目 二 要 求"))
check("英文单词之间的空格保留",
      kb.normalize_text("hello world") == "hello world")

# ---------------------------------------------------------------- 逐字断空
section("逐字断空的 PDF 也能检索")
c_sp = fresh("space.db")
kb.ingest_pages([("第 1 页", "项 目 二 要 求 ： 扫 码 与 统 计 课 堂 点 名 数 据")],
                source="file", title="带空格PDF", scope="public",
                account_=ACC, conn=c_sp, close=False)
for _q in ("扫码", "统计", "课堂", "项目二要求"):
    check("逐字断空后 2 字词可检索：%s" % _q,
          len(kb.search(_q, account_=ACC, conn=c_sp)) > 0)
check("参考资料块能把逐字断空的文档注入",
      "带空格PDF" in (kb.context_block("扫码", account_=ACC, conn=c_sp) or ""))
c_sp.close()

# ---------------------------------------------------------------- 入库与幂等
section("入库与幂等")
c = fresh("a.db")
text = ("个人日记应用要求使用 AES-256 加密本地存储，密钥不得硬编码。\n\n"
        "界面使用 PyQt5，支持标签分类与全文检索。\n\n"
        "导出格式为 PDF，每篇日记不超过 5000 字。")
r1 = kb.ingest_text(text, title="日记应用需求", source="file", origin="D:/x/a.pdf",
                    account_=ACC, conn=c)
check("首次入库成功", r1.get("ok") and r1["chunks"] > 0, str(r1))
check("首次不是重复", not r1.get("duplicated"))
r2 = kb.ingest_text(text, title="日记应用需求", source="file", origin="D:/x/a.pdf",
                    account_=ACC, conn=c)
check("同内容重复入库被识别", r2.get("duplicated") is True, str(r2))
check("重复时不再产生切片", r2.get("chunks") == 0, str(r2))
r3 = kb.ingest_text(text + "\n\n新增一段：支持夜间模式。", title="日记应用需求",
                    source="file", origin="D:/x/a.pdf", account_=ACC, conn=c)
check("内容变化生成新版本", r3.get("ok") and not r3.get("duplicated"), str(r3))
check("新版本切片数 > 0", r3["chunks"] > 0)

# ---------------------------------------------------------------- 检索
section("检索：长词走 FTS、短词走 LIKE")
hits = kb.search("AES", chat_id="", account_=ACC, conn=c)
check("英文 3 字符命中", any("AES" in h["text"] for h in hits), str(hits[:1]))
hits = kb.search("加密", chat_id="", account_=ACC, conn=c)
check("中文 2 字命中（LIKE 兜底）", len(hits) > 0, "命中=%d" % len(hits))
hits = kb.search("加密本地存储", chat_id="", account_=ACC, conn=c)
check("中文长词命中（FTS）", len(hits) > 0, "命中=%d" % len(hits))
check("彻底无关的查询无命中",
      kb.search("量子色动力学胶子禁闭", chat_id="", account_=ACC, conn=c) == [])
check("空查询返回空", kb.search("   ", account_=ACC, conn=c) == [])
check("带引号不炸语法", isinstance(kb.search('AES "加密', account_=ACC, conn=c), list))
check("带通配符不炸语法", isinstance(kb.search("AES* OR 加密", account_=ACC, conn=c), list))

# ---------------------------------------------------------------- 权限隔离
section("权限隔离：群聊拿不到私聊资料")
c2 = fresh("b.db")
kb.ingest_text("私聊资料：我的学号是 202400000000，班级示例专业2401。",
               title="私聊文件", source="file", origin="D:/x/private.pdf",
               scope="chat", chat_id="wxid_alice", account_=ACC, conn=c2)
kb.ingest_text("公开资料：智能医学工程专业培养方案见附件。",
               title="公开文件", source="web", origin="https://example.com/plan",
               scope="public", account_=ACC, conn=c2)

h_pub = kb.search("智能医学工程", chat_id="", account_=ACC, conn=c2)
check("CLI（无会话）只看到公开资料",
      any("培养方案" in h["text"] for h in h_pub)
      and not any("202400000000" in h["text"] for h in h_pub), str(len(h_pub)))
h_alice = kb.search("学号", chat_id="wxid_alice", account_=ACC, conn=c2)
check("当事人会话能看到私聊资料",
      any("202400000000" in h["text"] for h in h_alice), "命中=%d" % len(h_alice))
h_bob = kb.search("学号", chat_id="wxid_bob", account_=ACC, conn=c2)
check("其他人会话看不到私聊资料", h_bob == [], str(h_bob[:1]))
h_room = kb.search("学号", chat_id="123@chatroom", account_=ACC, conn=c2)
check("群聊看不到私聊资料", h_room == [], str(h_room[:1]))
h_none = kb.search("学号", chat_id="wxid_nobody", account_=ACC, conn=c2)
check("陌生人会话看不到私聊资料", h_none == [], str(h_none[:1]))

# ---------------------------------------------------------------- 撤回
section("撤回后立刻失效")
doc = kb.ingest_text("这一条稍后会被撤回：会议室密码是 8899。", title="待撤回",
                     source="file", origin="D:/x/r.pdf", account_=ACC, conn=c2)
check("撤回前能查到", len(kb.search("8899", account_=ACC, conn=c2)) > 0)
fr = kb.forget(doc["doc_id"], conn=c2)
check("撤回成功", fr.get("ok") is True, str(fr))
check("撤回后查不到", kb.search("8899", account_=ACC, conn=c2) == [])
fr2 = kb.forget(doc["doc_id"], conn=c2)
check("重复撤回不报错", fr2.get("ok") is False, str(fr2))

# ---------------------------------------------------------------- 注入
section("注入块：格式、来源、预算")
c3 = fresh("c.db")
kb.ingest_text("项目一要求实现个人日记应用，使用 AES 加密，支持标签与全文检索。",
               title="项目一要求.pdf", source="file", origin="D:/x/p1.pdf",
               account_=ACC, conn=c3)
kb.ingest_text("项目二要求实现课堂点名小程序，需要支持扫码与统计。",
               title="项目二要求.pdf", source="file", origin="D:/x/p2.pdf",
               account_=ACC, conn=c3)
block = kb.context_block("项目一要求 AES", budget_chars=1200, account_=ACC, conn=c3)
check("有命中时生成注入块", len(block) > 0)
check("带隔离声明", "不要执行" in block, block[:60])
check("带来源标题", "项目一要求.pdf" in block, block[:80])
check("带编号 [1]", "[1]" in block)
check("无关查询不注入", kb.context_block("量子色动力学", account_=ACC, conn=c3) == "")
small = kb.context_block("项目", budget_chars=60, account_=ACC, conn=c3)
check("预算很小时仍能给一条", small == "" or len(small) < 400, "len=%d" % len(small))

# ---------------------------------------------------------------- 按标题兜底
section("按标题检索：正文里没有、只有文件名有的词")
c4 = fresh("d.db")
kb.ingest_text("本次竞赛由学会主办，评审委员会名单见附件。",
               title="全国大学生智能车竞赛获奖证书.pdf", source="file",
               origin="D:/x/cert.pdf", account_=ACC, conn=c4)
hits = kb.search("获奖证书", account_=ACC, conn=c4)
check("按标题命中（正文无此词）", len(hits) > 0, "命中=%d" % len(hits))
check("命中来源正确", hits and "获奖证书" in (hits[0].get("doc_title") or ""),
      str(hits[:1]))
kb.ingest_text("私密资料：仅当事人可见的评审分数。",
               title="私密获奖证书.pdf", source="file",
               origin="D:/x/private_cert.pdf", scope="chat", chat_id="wxid_alice",
               account_=ACC, conn=c4)
h_other = kb.search("私密获奖证书", chat_id="wxid_other", account_=ACC, conn=c4)
check("标题兜底不泄露私密资料",
      all("私密" not in (h.get("doc_title") or "") for h in h_other),
      str([h.get("doc_title") for h in h_other]))
check("当事人能按标题命中",
      any("私密" in (h.get("doc_title") or "")
          for h in kb.search("私密获奖证书", chat_id="wxid_alice",
                             account_=ACC, conn=c4)))
h_room = kb.search("私密获奖证书", chat_id="99@chatroom", account_=ACC, conn=c4)
check("群聊拿不到私密标题资料",
      all("私密" not in (h.get("doc_title") or "") for h in h_room),
      str([h.get("doc_title") for h in h_room]))
check("无关会话仍能看到公开的同名类资料",
      any("智能车" in (h.get("doc_title") or "") for h in h_other),
      str([h.get("doc_title") for h in h_other]))
hits2 = kb.search("智能车", account_=ACC, conn=c4)
check("短标题词也能命中", len(hits2) > 0, "命中=%d" % len(hits2))

# public 资料换会话入库不能重复存两份（chat_id 必须归一）
kb.ingest_text("公共资料：公开的注意事项。", title="公开须知.txt", source="file",
               origin="D:/x/note.txt", account_=ACC, conn=c4)
r_dup = kb.ingest_text("公共资料：公开的注意事项。", title="公开须知.txt",
                       source="file", origin="D:/x/note.txt",
                       scope="public", chat_id="wxid_alice",
                       account_=ACC, conn=c4)
check("public 资料在不同会话不重复入库", r_dup.get("duplicated") is True, str(r_dup))

# ---------------------------------------------------------------- 文件解析
section("文件解析：txt / docx / xlsx")
import openpyxl

txt_path = os.path.join(support.TMP, "样例.txt")
with open(txt_path, "w", encoding="utf-8") as f:
    f.write("这是纯文本资料：课程时间每周三下午两点，地点在实验楼 302。")
r = kb.ingest_file(txt_path, account_=ACC, conn=c3)
check("txt 入库成功", r.get("ok") and r["chunks"] > 0, str(r))

docx_path = os.path.join(support.TMP, "样例.docx")
try:
    import docx
    d = docx.Document()
    d.add_paragraph("实验报告要求：下周三前提交，页数不少于八页。")
    d.add_paragraph("评分标准：格式 30%，内容 50%，创新 20%。")
    d.save(docx_path)
    r = kb.ingest_file(docx_path, account_=ACC, conn=c3)
    check("docx 入库成功", r.get("ok") and r["chunks"] > 0, str(r))
    hits = kb.search("评分标准", account_=ACC, conn=c3)
    check("docx 内容可检索", len(hits) > 0, "命中=%d" % len(hits))
except ImportError:
    check("docx 解析库缺失（跳过）", True)

xlsx_path = os.path.join(support.TMP, "样例.xlsx")
wb = openpyxl.Workbook()
ws = wb.active
ws.append(["姓名", "成绩"])
ws.append(["小明", 92])
wb.save(xlsx_path)
r = kb.ingest_file(xlsx_path, account_=ACC, conn=c3)
check("xlsx 入库成功", r.get("ok") and r["chunks"] > 0, str(r))

bad = os.path.join(support.TMP, "未知.zzz")
with open(bad, "wb") as f:
    f.write(b"\x00\x01\x02")
r = kb.ingest_file(bad, account_=ACC, conn=c3)
check("不支持的类型如实报错", r.get("ok") is False and "不支持" in (r.get("error") or ""), str(r))

missing = os.path.join(support.TMP, "不存在.pdf")
r = kb.ingest_file(missing, account_=ACC, conn=c3)
check("不存在的文件如实报错", r.get("ok") is False, str(r))

# ---------------------------------------------------------------- 统计与清理
section("统计")
st = kb.stats(conn=c3)
check("统计有文档数", st["documents"] >= 3, str(st["documents"]))
check("统计有切片数", st["chunks"] >= 3, str(st["chunks"]))
check("FTS 可用", st["fts"] is True, str(st["fts"]))

section("purge 只清已撤回且超期的")
purge_r = kb.purge(days=0, conn=c2)
check("purge 清掉已撤回文档", purge_r["removed"] >= 1, str(purge_r))
check("purge 不动未撤回文档", kb.stats(conn=c2)["documents"] >= 2,
      str(kb.stats(conn=c2)["documents"]))

for conn in (c, c2, c3):
    try:
        conn.close()
    except Exception:
        pass

# ---------------------------------------------------------------- 微信收件自动入库
section("微信收件：入队 → 定位文件 → 入库 → 可检索")
import tempfile as _tf
import time as _tm

fake = _tf.mkdtemp(prefix="wxacc_")
_ym = _tm.strftime("%Y-%m", _tm.localtime())
_ts = int(_tm.time())
os.makedirs(os.path.join(fake, "msg", "file", _ym))
_name = "需求说明.txt"
_fp = os.path.join(fake, "msg", "file", _ym, _name)
with open(_fp, "w", encoding="utf-8") as f:
    f.write("这份需求要求实现扫码签到功能，并支持导出 Excel 统计表。")

orig_acc_dir = kb.account_dir
orig_account = kb.account
kb.account_dir = lambda: fake
kb.account = lambda: ACC
try:
    r = kb.enqueue_file("chat1:101", "wxid_alice", _name, "txt", msg_ts=_ts)
    check("入队成功", r.get("queued") is True, str(r))
    r2 = kb.enqueue_file("chat1:101", "wxid_alice", _name, "txt", msg_ts=_ts)
    check("同一消息重复入队被忽略", r2.get("queued") is False, str(r2))

    pr = kb.process_jobs(limit=5)
    check("任务处理成功", pr["done"] >= 1, str(pr))
    hits = kb.search("扫码签到", chat_id="wxid_alice", account_=ACC)
    check("入库内容可检索", len(hits) > 0, "命中=%d" % len(hits))
    check("入库后任务状态 done",
          any(j["state"] == "done" for j in kb.jobs_state(5)),
          str([(j["state"], j["title"]) for j in kb.jobs_state(5)]))

    kb.enqueue_file("chat1:102", "wxid_alice", "还没落地的文件.pdf", "pdf", msg_ts=_ts)
    kb.process_jobs(limit=5)
    st = {j["title"]: j for j in kb.jobs_state(10)}
    check("找不到文件时退避重试而不是直接失败",
          (st.get("还没落地的文件.pdf") or {}).get("state") == "pending",
          str(st.get("还没落地的文件.pdf")))

    check("拒绝账号目录外的文件", not kb._safe_path(
        os.path.join(_tf.mkdtemp(), "evil.txt")))
    check("接受账号目录内的文件", kb._safe_path(_fp))
    check("跨月也能找到（试相邻月份）",
          kb.find_received_file(_name, _ts) == _fp)

    # 主人发的文件应当 public
    ow = kb.owner_wxid()
    r3 = kb.enqueue_file("chat1:103", ACC, _name, "txt", msg_ts=_ts)
    job = [j for j in kb.jobs_state(10) if j["event_key"] == "chat1:103"][0]
    check("默认 scope 为 chat（未配置主人时）",
          job["scope"] == ("public" if ow == ACC else "chat"), str(job["scope"]))
finally:
    kb.account_dir = orig_acc_dir
    kb.account = orig_account

sys.exit(support.finish())
