"""数据模型的序列化与派生字段。"""

from linglong_clip.core.models import ClipboardItem, content_hash


def test_content_hash_stable_and_kind_sensitive():
    assert content_hash("text", "abc") == content_hash("text", "abc")
    assert content_hash("text", "abc") != content_hash("link", "abc")


def test_is_empty():
    assert ClipboardItem(kind="text", content="").is_empty is True
    assert ClipboardItem(kind="text", content="   \n\t ").is_empty is True
    assert ClipboardItem(kind="text", content="x").is_empty is False
    # 只要有图片路径或文件就算有内容
    assert ClipboardItem(kind="image", image_path="a.png").is_empty is False
    assert ClipboardItem(kind="file", files=["/a"]).is_empty is False


def test_roundtrip_through_row(storage):
    item = ClipboardItem(
        kind="link",
        content="https://cnb.cool/x",
        preview="https://cnb.cool/x",
        files=[],
        metadata={"host": "cnb.cool"},
        tags=["a", "b"],
        hash="h1",
    )
    saved = storage.add(item)
    loaded = storage.get(saved.id)
    assert loaded.metadata == {"host": "cnb.cool"}
    assert loaded.tags == ["a", "b"]
    assert loaded.kind == "link"


def test_files_json_roundtrip(storage):
    item = ClipboardItem(kind="file", content="/a\n/b", files=["/a", "/b"], hash="h2")
    saved = storage.add(item)
    assert storage.get(saved.id).files == ["/a", "/b"]


def test_size_label():
    assert ClipboardItem(size=512).size_label == "512 B"
    assert "KB" in ClipboardItem(size=2048).size_label
    assert "MB" in ClipboardItem(size=3 * 1024 * 1024).size_label


def test_corrupt_json_fields_fall_back():
    class Row(dict):
        def __getitem__(self, key):
            return self.get(key)

    row = Row(
        id=1, kind="text", content="x", preview="x", html="", rtf="",
        image_path="", image_width=0, image_height=0,
        files="{not json", metadata="[]", source_app="", source_title="",
        hash="h", size=0, pinned=0, favorite=0, sensitive=0,
        tags="bad", device="", created_at=0.0, used_at=0.0, use_count=0,
    )
    item = ClipboardItem.from_row(row)
    assert item.files == []
    assert item.metadata == {}
    assert item.tags == []
