# -*- coding: utf-8 -*-
"""桌面预览的选号、密钥、运行目录和服务生命周期。"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import support
from support import check, section

sys.path.insert(0, str(Path(support.ROOT) / "GUI"))
import runtime
import apikeys
import config
import wx_identity
import gui_facilities

section("桌面启动只接受确认的小号")
account = "wxid_preview_test0001"
profile = {
    "mode": "custom", "base_url": "https://fixture.invalid/v1/chat/completions",
    "model": "fixture-chat", "api_key": "fixture-key-do-not-store",
}
root = Path(support.TMP) / "project"
root.mkdir()
original = {"account": account, "token": "original-token",
            "llm": {"api_key": "original-key", "model": "original-model"},
            "agent": {"auto_reply": True, "reply_blacklist": ["wxid_blocked"]},
            "proactive": {"enabled": True, "reminders": {"enabled": True}}}
(root / "config.json").write_text(json.dumps(original), encoding="utf-8")
state = root / "preview"
demo = runtime.OrangeRuntime()


class FakeChild:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 0

    def wait(self, timeout):
        return self.code


launches = []


def launch(entry, args, env, log_path):
    child = FakeChild()
    demo._children.append(child)
    launches.append((entry, args, env, child))
    if entry == runtime.WORKER:
        Path(env["WXBOT_READY_FILE"]).write_text("ready", encoding="utf-8")
    return child


def reject(fn):
    try:
        fn()
    except runtime.RuntimeError:
        return True
    return False


section("保存的密钥只用于匹配的 API 地址")
provider_root = root / "providers"
provider_root.mkdir()
provider_config = provider_root / "config.json"
configured_url = "https://first.fixture.invalid/v1"
configured = {"llm": {"base_url": configured_url, "api_key": "legacy-key"}}
provider_config.write_text(json.dumps(configured), encoding="utf-8")
with patch.object(runtime, "ROOT", provider_root), patch.dict(os.environ, {
    "WXBOT_API_KEYS": str(provider_root / "api_keys.json"), "DSHWX_API_KEY": "",
}):
    apikeys.add("Other provider", "https://second.fixture.invalid/v1", "model", "other-key")
    apikeys.add("Other endpoint", "https://first.fixture.invalid/other", "model", "other-path-key")
    check("无匹配记录时继续使用旧文件里的 Key", demo._original_model()["api_key"] == "legacy-key"
          and config._apply_env(config._merge_defaults(configured))["llm"]["api_key"] == "legacy-key")
    provider_config.write_text(json.dumps({"llm": {"base_url": configured_url}}), encoding="utf-8")
    check("没有匹配的 Key 时拒绝启动", not demo.configured_model()["key_available"]
          and reject(lambda: demo._model({"mode": "configured"})))
    apikeys.add("Project endpoint", "https://FIRST.fixture.invalid:443/v1/chat/completions/",
                "model", "matching-key", make_default=False)
    check("优先使用匹配地址的记录，忽略其他服务的默认 Key",
          demo._model({"mode": "configured"})["api_key"] == "matching-key"
          and config._apply_env(config._merge_defaults(configured))["llm"]["api_key"] == "matching-key")
    with patch.dict(os.environ, {"DSHWX_API_KEY": "explicit-key"}):
        check("环境变量仍可显式覆盖", demo._original_model()["api_key"] == "explicit-key"
              and config._apply_env(config._merge_defaults(configured))["llm"]["api_key"] == "explicit-key")


section("桌面会话生命周期")
with patch.multiple(runtime, ROOT=root, STATE=state), \
     patch.object(demo, "discover_accounts", return_value=[
         {"account": account, "wxid": account, "path": str(root / account)}]), \
     patch.object(demo, "_reserve_session", side_effect=lambda: state.mkdir(exist_ok=True)), \
     patch.object(demo, "_launch", side_effect=launch), \
     patch.object(demo, "_wait_service", return_value={"account": account, "account_bound": True}):
    check("未确认不能启动", reject(lambda: demo.start(account, profile, False)))
    check("未在线账号不能启动", reject(lambda: demo.start("wxid_other", profile, True)))
    check("拒绝在非本机 HTTP 地址使用密钥", reject(lambda: demo.start(
        account, dict(profile, base_url="http://fixture.invalid/v1"), True)))
    check("现有模型摘要不包含密钥", "api_key" not in demo.configured_model())

    session = demo.start(account, profile, True)
    check("明确选定账号和模型", session["account"] == account
          and session["model"] == "fixture-chat"
          and session["base_url"] == "https://fixture.invalid/v1")
    check("两个进程使用相同选定账号", demo.running and len(launches) == 2
          and all(call[2]["WXBOT_ACCOUNT"] == account for call in launches))
    env = launches[0][2]
    check("服务使用独立端口和令牌",
          env["WXBOT_PORT"] in launches[0][1] and len(env["DSHWX_TOKEN"]) >= 32)
    check("队列和记忆隔离", all(Path(env[key]).is_relative_to(state)
                                for key in ("WXBOT_QUEUE_DB", "WXBOT_MEMORY_DB", "WXBOT_KB_DB")))
    stored = json.loads(Path(env["WXBOT_CONFIG"]).read_text(encoding="utf-8"))
    check("预览配置不落密钥", not stored["token"] and not stored["llm"]["api_key"]
          and env["DSHWX_API_KEY"] == profile["api_key"])
    check("启动默认关闭三个发送开关", not stored["agent"]["auto_reply"]
          and not stored["proactive"]["enabled"]
          and not stored["proactive"]["reminders"]["enabled"])
    check("同账号沿用原名单", stored["agent"]["reply_blacklist"] == ["wxid_blocked"])
    check("旧版配置保持原值", runtime.read_config(root / "config.json") == original)

    previous_state = dict(config._state)
    try:
        with patch.object(config, "CONFIG_PATH", env["WXBOT_CONFIG"]), \
             patch.dict(os.environ, {"DSHWX_API_KEY": env["DSHWX_API_KEY"],
                                     "DSHWX_TOKEN": env["DSHWX_TOKEN"]}):
            current = config.load(force=True)
            current["agent"]["auto_reply"] = True
            check("GUI 更新配置成功", config.save(current))
            after = runtime.read_config(Path(env["WXBOT_CONFIG"]))
            check("更新继续隔离环境密钥", not after["token"] and not after["llm"]["api_key"])
            check("服务仍可读取当前模型密钥", config.load()["llm"]["api_key"] == profile["api_key"])
    finally:
        config._state.update(previous_state)
    check("运行时不能切换账号", reject(lambda: demo.start(account, profile, True)))
    demo.stop()
    check("停止只终止本次两个进程", not demo.running
          and all(call[3].poll() == 0 for call in launches))
    demo.start(account, profile, True)
    children = tuple(demo._children)
    children[0].code = 1
    demo._watch_children(children, root / "process.log")
    check("服务崩溃后同时停止工作进程", children[1].poll() == 0
          and bool(demo.exit_reason) and not demo.running)

section("账号验证不启用客户端回退")
account = "wxid_fixture_cd12"
directory = root / account
(directory / "db_storage").mkdir(parents=True)
rows = [{"account": account, "path": str(directory), "wxid": "wxid_fixture"}]
opened = []


class FakeDB:
    def __init__(self, db_dir, account):
        self.account = account
        self.account_dir = str(Path(db_dir) / account)
        self.wxid = "wxid_fixture"
        self.keys_file = str(Path(db_dir) / "selected" / "keys.json")
        opened.append((self._find_weixin_pids(), self._try_other_accounts(),
                       self._select_account_by_keys(None)))

    def get_self_info(self):
        return {"username": self.wxid}


with patch.dict(os.environ, {"WXBOT_ACCOUNT": account,
                             "WXBOT_ACCOUNT_DIR": str(directory),
                             "WXBOT_ACCOUNT_WXID": "wxid_fixture"}), \
     patch.object(wx_identity, "_dependencies",
                  return_value=(FakeDB, lambda: rows, lambda *_: (21, 22))):
    check("发现在线账号不打开数据库", len(wx_identity.discover_accounts()) == 1 and not opened)
    check("数据库仅允许已核验 PID 和选定账号",
          wx_identity.open_bound_db(account).account == account
          and opened == [([21], False, False)])
    try:
        wx_identity.open_bound_db(account, expected_wxid="wxid_other")
    except RuntimeError:
        check("拒绝其他微信号", True)
    else:
        check("拒绝其他微信号", False)

section("桌面操作使用选定小号的知识库和暂停队列")
fixture_cfg = {"account": account, "kb": {"enabled": True, "inbox": str(root)}}
article = root / "fixture.txt"
article.write_text("接口验收资料。聊天回复须等待微信回读确认，回读失败时应在队列中标示。",
                   encoding="utf-8")
with patch.dict(os.environ, {"WXBOT_ACCOUNT": account, "WXBOT_CONFIG": "fixture.json",
                             "DSHWX_TOKEN": "test-token"}), \
     patch.object(gui_facilities.kb, "load_cfg", return_value=fixture_cfg):
    result = gui_facilities.kb_request({"action": "ingest", "path": str(article),
                                       "chat": "wxid_fixture"})
    check("导入资料", result.get("ok"), str(result))
    check("目录和资料状态返回给 GUI", gui_facilities.kb_request()["inbox"] == str(root)
          and gui_facilities.kb_request()["stats"]["documents"] == 1)
    query = {"action": "search", "query": "回读确认"}
    check("私聊资料保持隔离", not gui_facilities.kb_request(query)["items"]
          and gui_facilities.kb_request(dict(query, chat="wxid_fixture"))["items"])
    check("撤回后资料不再可见",
          gui_facilities.kb_request({"action": "forget", "id": result["doc_id"]})["ok"]
          and not gui_facilities.kb_request(dict(query, chat="wxid_fixture"))["items"])
    check("暂停联系人立即落库",
          gui_facilities.pause_chat(account, {"chat": "wxid_fixture", "minutes": 30})["ok"]
          and gui_facilities.Queue().is_paused("wxid_fixture"))

sys.exit(support.finish())

