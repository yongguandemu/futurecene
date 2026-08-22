"""test_worldbook_metadata.py — 世界书重要记事字段（任务五 5.4）"""
import json
import tempfile
from pathlib import Path

from src.shared.world_book import WorldBook
from src.web.app_factory import create_app


def _make():
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    p.write_text(json.dumps({"version": 2, "entry_count": 0, "entries": [],
                             "categories": {}}, ensure_ascii=False), encoding="utf-8")
    wb = WorldBook(p)
    wb.add_entry("note_1", "重要记事", "观众喜欢推理", category="character",
                 metadata={"role": "yuki"})
    return create_app({"world_book": wb}), wb


def test_priority_note_via_put():
    """PUT /api/worldbook/<id> 带 metadata → 合并 priority_note（重要记事标记）。"""
    app, wb = _make()
    r = app.test_client().put("/api/worldbook/note_1",
                              json={"metadata": {"priority_note": True}})
    assert r.status_code == 200
    entry = r.get_json()["data"]
    assert entry["metadata"]["priority_note"] is True
    assert entry["metadata"]["role"] == "yuki"  # 原有 metadata 保留（按键合并）


def test_priority_note_persisted():
    app, wb = _make()
    app.test_client().put("/api/worldbook/note_1",
                          json={"metadata": {"priority_note": True}})
    assert wb.get_entry("note_1")["metadata"]["priority_note"] is True
    # 落盘后重载验证
    wb.save_to_disk()
    wb2 = WorldBook(wb._path)
    assert wb2.get_entry("note_1")["metadata"]["priority_note"] is True


def test_update_without_metadata_keeps_existing():
    app, wb = _make()
    app.test_client().put("/api/worldbook/note_1", json={"content": "新内容"})
    entry = wb.get_entry("note_1")
    assert entry["content"] == "新内容"
    assert entry["metadata"] == {"role": "yuki"}  # 原 metadata 不变
