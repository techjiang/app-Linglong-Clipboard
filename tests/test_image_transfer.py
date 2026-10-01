"""图片按需传输：内容寻址、校验、原子落盘。

这个模块承担「对端给的东西凭什么信」这件事，所以用例的重点不在
happy path，而在**拒绝**：篡改的字节、路径穿越的摘要、超限的载荷。
"""

from __future__ import annotations

import os

import pytest

from linglong_clip.core import image_transfer
from linglong_clip.core.models import IMAGE_DIGEST_KEY, ClipboardItem


def _png(payload: bytes = b"body") -> bytes:
    return b"\x89PNG\r\n\x1a\n" + payload


# ---------- 摘要与格式 ----------


def test_digest_matches_collector_naming():
    """摘要算法必须与采集侧一致，否则每张图都会被判成「对端没有」。

    采集侧（`ui/clipboard_qt.py`）用 blake2b、16 字节摘要做文件名。
    """
    import hashlib

    data = _png(b"x")
    assert image_transfer.digest_of(data) == hashlib.blake2b(
        data, digest_size=16
    ).hexdigest()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("a" * 32, "a" * 32),
        ("A" * 32, "a" * 32),
        ("images/" + "b" * 32 + ".png", "b" * 32),
        ("c" * 32 + ".png", "c" * 32),
        ("images\\" + "d" * 32 + ".png", "d" * 32),
        ("", ""),
        (None, ""),
        ("not-a-digest", ""),
        ("a" * 31, ""),
        ("a" * 33, ""),
        ("g" * 32, ""),  # 非十六进制
        ("data:image/png;base64,AAAA", ""),  # 内联图不需要跨设备取
    ],
)
def test_normalize_digest(value, expected):
    assert image_transfer.normalize_digest(value) == expected


@pytest.mark.parametrize(
    "value",
    ["../" + "a" * 32, "../../etc/passwd", "/etc/passwd", "a/../b", "..%2f" + "a" * 32],
)
def test_path_traversal_values_are_not_valid_digests(value):
    """摘要会参与拼路径，放行 `../` 就是一个读写任意路径的原语。

    这道闸门由 `is_valid_digest` 把守 —— 它是**拿到线上数据的第一站**
    （HTTP 路径参数、记录的 `image_path`），必须先过它。
    """
    assert image_transfer.is_valid_digest(value) is False


def test_normalize_digest_only_extracts_the_basename():
    """`normalize_digest` 只取文件名，**不负责**判合法性之外的路径安全。

    这不是漏洞：它取出的摘要从不直接拼路径 —— 路径一律由
    `digest` 重新拼（`images/<digest>.png`），而 `digest` 已过
    `is_valid_digest`。所以 `../aaa.png` 在这里被读成 `aaa` 是安全的，
    且比直接拒绝更宽容：老库里确实有带目录前缀的写法。
    """
    assert image_transfer.normalize_digest("../" + "a" * 32 + ".png") == "a" * 32
    # 但真正危险的形状（含非法字符、长度不对）仍然拿不到东西
    assert image_transfer.normalize_digest("../../etc/passwd") == ""


# ---------- digest_of_item：两条路都要看 ----------


def test_digest_of_item_prefers_path():
    digest = "a" * 32
    item = ClipboardItem(kind="image", image_path=f"images/{digest}.png")
    assert image_transfer.digest_of_item(item) == digest


def test_digest_of_item_falls_back_to_metadata():
    """同步来的记录没有路径、只有摘要 —— 这是按需补图的唯一线索。

    只看 `image_path` 会让同步来的记录被当成「本来就没有图」，
    于是补图永远找不到要补的东西，而且不报错。
    """
    digest = "b" * 32
    item = ClipboardItem(kind="image", metadata={IMAGE_DIGEST_KEY: digest})
    assert image_transfer.digest_of_item(item) == digest


def test_digest_of_item_ignores_junk():
    assert image_transfer.digest_of_item(ClipboardItem(kind="text")) == ""
    assert (
        image_transfer.digest_of_item(
            ClipboardItem(kind="image", metadata={IMAGE_DIGEST_KEY: "junk"})
        )
        == ""
    )
    assert image_transfer.digest_of_item(ClipboardItem(kind="image", metadata=None)) == ""


# ---------- 校验 ----------


def test_verify_accepts_matching_bytes():
    data = _png(b"ok")
    assert image_transfer.verify(data, image_transfer.digest_of(data)) is True


def test_verify_rejects_tampered_bytes():
    data = _png(b"ok")
    assert image_transfer.verify(_png(b"not-ok"), image_transfer.digest_of(data)) is False


def test_verify_rejects_bad_digest_format():
    data = _png(b"ok")
    assert image_transfer.verify(data, "junk") is False
    assert image_transfer.verify(data, "") is False


# ---------- 落盘 ----------


def test_store_writes_and_returns_relative_path(tmp_path):
    data = _png(b"store-me")
    digest = image_transfer.digest_of(data)
    result = image_transfer.store(tmp_path, digest, data)
    assert result.stored is True
    assert result.path == f"{tmp_path.name}/{digest}.png"
    assert (tmp_path / f"{digest}.png").read_bytes() == data


def test_store_is_idempotent(tmp_path):
    data = _png(b"twice")
    digest = image_transfer.digest_of(data)
    assert image_transfer.store(tmp_path, digest, data).stored
    second = image_transfer.store(tmp_path, digest, data)
    assert second.stored is True
    assert second.reason == "已存在"


def test_store_rejects_mismatched_bytes(tmp_path):
    """落盘处再校验一次是有意的：这是所有路径的收口。

    一张来源不明的图进到 `images/` 之后**再也无法察觉** ——
    文件名已经是摘要了，看上去完全正常。
    """
    data = _png(b"real")
    digest = image_transfer.digest_of(data)
    result = image_transfer.store(tmp_path, digest, _png(b"fake"))
    assert result.stored is False
    assert "摘要不符" in result.reason
    assert not (tmp_path / f"{digest}.png").exists()


def test_store_rejects_bad_digest(tmp_path):
    assert image_transfer.store(tmp_path, "../../etc/passwd", _png()).stored is False
    assert image_transfer.store(tmp_path, "", _png()).stored is False


def test_store_rejects_empty_and_oversized(tmp_path):
    data = _png(b"x")
    digest = image_transfer.digest_of(data)
    assert image_transfer.store(tmp_path, digest, b"").stored is False

    big = b"x" * (image_transfer.MAX_IMAGE_BYTES + 1)
    assert image_transfer.store(
        tmp_path, image_transfer.digest_of(big), big
    ).stored is False


def test_store_leaves_no_partial_file_on_failure(tmp_path, monkeypatch):
    """写一半失败时不能留下半个文件。

    半个 PNG 会被后续的引用集统计和界面当成一张真图 ——
    文件名就是合法摘要，看不出区别。
    """
    data = _png(b"atomic")
    digest = image_transfer.digest_of(data)

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("模拟写入失败")

    monkeypatch.setattr(os, "replace", boom)
    result = image_transfer.store(tmp_path, digest, data)
    assert result.stored is False

    monkeypatch.setattr(os, "replace", real_replace)
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == [], f"留下了半截文件：{leftovers}"


def test_store_creates_directory(tmp_path):
    nested = tmp_path / "images"
    data = _png(b"mkdir")
    result = image_transfer.store(nested, image_transfer.digest_of(data), data)
    assert result.stored is True
    assert nested.is_dir()


# ---------- 待补清单 ----------


def test_pending_digests_finds_synced_records_without_bytes(tmp_path):
    digest = "c" * 32
    items = [
        ClipboardItem(kind="image", metadata={IMAGE_DIGEST_KEY: digest}),
        ClipboardItem(kind="text", content="无关"),
    ]
    assert image_transfer.pending_digests(items, tmp_path) == [digest]


def test_pending_digests_skips_present_files(tmp_path):
    data = _png(b"present")
    digest = image_transfer.digest_of(data)
    (tmp_path / f"{digest}.png").write_bytes(data)
    items = [ClipboardItem(kind="image", image_path=f"images/{digest}.png")]
    assert image_transfer.pending_digests(items, tmp_path) == []


def test_pending_digests_deduplicates(tmp_path):
    """同一张图可能被多条记录引用（合并关闭时的重复行）。"""
    digest = "d" * 32
    items = [
        ClipboardItem(kind="image", metadata={IMAGE_DIGEST_KEY: digest}),
        ClipboardItem(kind="image", metadata={IMAGE_DIGEST_KEY: digest}),
    ]
    assert image_transfer.pending_digests(items, tmp_path) == [digest]


# ---------- 「这张图有没有被真正看过」 ----------


def test_privacy_inspectable_requires_ocr_entry():
    """只有 OCR 结果存在时，才算「看过这张图」。

    区分两种「OCR 没给出文字」：
    - `metadata["ocr"]` 存在、`text` 为空 → 看过，图里确实没字
    - `metadata["ocr"]` 不存在 → 没看过，这张图对我们是不透明的
    """
    assert image_transfer.privacy_inspectable(
        ClipboardItem(kind="image", metadata={"ocr": {"text": "", "backend": "t"}})
    )
    assert image_transfer.privacy_inspectable(
        ClipboardItem(kind="image", metadata={"ocr": {"text": "hello", "backend": "t"}})
    )
    assert not image_transfer.privacy_inspectable(
        ClipboardItem(kind="image", metadata={"other": 1})
    )
    assert not image_transfer.privacy_inspectable(ClipboardItem(kind="image"))


def test_privacy_inspectable_tolerates_broken_metadata():
    """老库/手改过的 JSON 里 `ocr` 可能是字符串或 None，不能抛异常。"""
    for bad in [None, "", "text", 42, []]:
        item = ClipboardItem(kind="image", metadata={"ocr": bad})
        assert image_transfer.privacy_inspectable(item) is False
    assert image_transfer.privacy_inspectable(ClipboardItem(kind="image", metadata=None)) is False


def test_store_survives_unlink_failure_during_cleanup(tmp_path, monkeypatch):
    """清理临时文件本身失败时，不能把原始异常盖掉、也不能假装成功。

    这一段只有在「写入失败 + 临时文件又删不掉」时才走到（磁盘满、
    同步盘上文件被占用）。它必须把**原因如实报出来**，因为用户
    真正需要知道的是「图片没存下来」，而不是「清理也失败了」。
    """
    data = _png(b"cleanup")
    digest = image_transfer.digest_of(data)

    def boom_replace(src, dst):
        raise OSError("模拟 replace 失败")

    def boom_unlink(path):
        raise OSError("模拟删除也失败")

    monkeypatch.setattr(os, "replace", boom_replace)
    monkeypatch.setattr(os, "unlink", boom_unlink)

    result = image_transfer.store(tmp_path, digest, data)
    assert result.stored is False
    assert "写入失败" in result.reason
