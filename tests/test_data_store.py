"""test_data_store.py — 键值持久化存储（规格书 6.4）"""
from src.shared.data_store import DataStore


def test_set_get(tmp_path):
    ds = DataStore(path=str(tmp_path / "store.json"))
    ds.set("role", "yuki")
    assert ds.get("role") == "yuki"
    assert ds.get("missing", "fallback") == "fallback"


def test_delete(tmp_path):
    ds = DataStore(path=str(tmp_path / "store.json"))
    ds.set("a", 1)
    assert ds.delete("a") is True
    assert ds.get("a") is None
    assert ds.delete("a") is False  # 不存在返回 False


def test_snapshot(tmp_path):
    ds = DataStore(path=str(tmp_path / "store.json"))
    ds.set("a", 1)
    ds.set("b", {"k": "v"})
    assert ds.snapshot() == {"a": 1, "b": {"k": "v"}}


def test_persistence_across_instances(tmp_path):
    path = str(tmp_path / "store.json")
    ds1 = DataStore(path=path)
    ds1.set("mode", "live")
    ds2 = DataStore(path=path)  # 重新加载，验证持久化
    assert ds2.get("mode") == "live"
