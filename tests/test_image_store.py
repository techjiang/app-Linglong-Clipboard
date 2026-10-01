"""图片附件的回收与对账。

`images/` 曾经只有入口没有出口：记录消失之后那份 PNG 没人来收。
三条路径都会漏 —— 淘汰、敏感过期、以及采集期就被拦下的截图；
JSON 后端的 `delete()` / `clear()` 连顺手删也没有。这些漏法有个共同点：
**不报错、界面照常**，只有磁盘在涨，而 `--stats` 里连图片占用都不显示。

本文件分两部分：

- 纯函数部分（`image_store`）：路径归一、回收、占用统计；
- **跨后端部分**：同一组用例对 SQLite 与 JSON 各跑一遍。两个后端
  各写一份实现，分叉的后果是「换个存储方式，磁盘表现就不一样」——
  那正是本轮修掉的缺陷之一，所以要用同一组断言把它们钉在一起。
"""

import pytest

from linglong_clip.core import image_store
from linglong_clip.core.models import ClipboardItem

PNG = b"\x89PNG\r\n\x1a\nfake"


def _add_image(repo, name, *, hash_=None, **kwargs):
    """落一份图片文件 + 一条引用它的记录。"""
    directory = repo.images_dir
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(PNG + name.encode())
    return repo.add(
        ClipboardItem(
            kind="image",
            content=name,
            image_path=f"images/{name}",
            hash=hash_ or f"h-{name}",
            preview=f"[图片] {name}",
            # 真机上采集管线会给图片补上 OCR 结果（哪怕是「图里没字」）。
            # 没有这个标记的图被视为「没被读过」，不会外发 ——
            # 见 `image_transfer.privacy_inspectable`。
            metadata={"ocr": {"text": "", "backend": "stub"}},
            **kwargs,
        )
    )


# ---- 路径归一 ----


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("images/a.png", "images/a.png"),
        ("./images/a.png", "images/a.png"),
        ("images\\a.png", "images/a.png"),
        ("  images/a.png  ", "images/a.png"),
        ("a.png", "a.png"),
        ("", ""),
        (None, ""),
        ("data:image/png;base64,AAAA", ""),
    ],
)
def test_normalize_relative(raw, expected):
    assert image_store.normalize_relative(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("images/a.png", "a.png"),
        ("a.png", "a.png"),
        ("/abs/deep/a.png", "a.png"),
        ("", ""),
        ("data:image/png;base64,AAAA", ""),
    ],
)
def test_basename_of(raw, expected):
    assert image_store.basename_of(raw) == expected


def test_referenced_names_drops_blanks_and_uris():
    names = image_store.referenced_names(
        ["images/a.png", "b.png", "", None, "data:image/png;base64,AAAA"]
    )
    assert names == {"a.png", "b.png"}


# ---- 回收：只会动我们自己的文件 ----


def test_sweep_removes_unreferenced(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / "keep.png").write_bytes(PNG)
    (images / "junk.png").write_bytes(PNG * 10)

    result = image_store.sweep(images, {"keep.png"}, dry_run=False)
    assert result.removed == 1
    assert result.kept == 1
    assert result.bytes == len(PNG * 10)
    assert (images / "keep.png").exists()
    assert not (images / "junk.png").exists()


def test_sweep_dry_run_deletes_nothing(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / "junk.png").write_bytes(PNG)

    result = image_store.sweep(images, set(), dry_run=True)
    assert result.dry_run is True
    assert result.removed == 1
    assert result.names == ["junk.png"]
    assert (images / "junk.png").exists(), "预演绝不能真删"


def test_sweep_skips_directories_and_symlinks(tmp_path):
    """只动自己写下的普通 PNG 文件。

    `images/` 可能被用户指向同步盘 —— 里面混着别的东西时误删的代价
    远大于多留几 MB，所以子目录与符号链接一律不碰。
    """
    images = tmp_path / "images"
    images.mkdir()
    (images / "sub").mkdir()
    (images / "sub" / "deep.png").write_bytes(PNG)
    (images / "notes.txt").write_bytes(b"not ours")
    target = tmp_path / "outside.png"
    target.write_bytes(PNG)
    try:
        (images / "link.png").symlink_to(target)
    except OSError:
        pass  # 某些环境不允许建符号链接，跳过这一项

    result = image_store.sweep(images, set(), dry_run=False)
    assert result.removed == 0
    assert (images / "sub" / "deep.png").exists()
    assert (images / "notes.txt").exists()
    assert target.exists(), "符号链接指向的文件绝不能被删"


def test_sweep_respects_limit_and_flags_truncation(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    for index in range(5):
        (images / f"j{index}.png").write_bytes(PNG)

    result = image_store.sweep(images, set(), dry_run=False, limit=2)
    assert result.removed == 2
    assert result.truncated is True
    assert len(list(images.iterdir())) == 3


def test_sweep_missing_directory_is_not_an_error(tmp_path):
    result = image_store.sweep(tmp_path / "nope", set(), dry_run=False)
    assert result.removed == 0
    assert result.errors == 0


# ---- 占用统计 ----


def test_usage_counts_files_bytes_and_missing(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / "a.png").write_bytes(PNG)
    (images / "b.png").write_bytes(PNG)

    usage = image_store.usage(images, {"a.png", "b.png", "gone.png"})
    assert usage.files == 2
    assert usage.bytes == len(PNG) * 2
    assert usage.missing == 1, "记录引用了但文件不在，要单独报出来"


# ---- 跨后端：同一组语义 ----


@pytest.fixture(params=["sqlite", "json"])
def repo(request, tmp_path):
    """两个后端同一组用例，断言逐条对齐。"""
    if request.param == "sqlite":
        from linglong_clip.core.storage import Storage

        instance = Storage(tmp_path / "history.db")
    else:
        from linglong_clip.core.repository import JsonRepository

        instance = JsonRepository(tmp_path / ".clipboard" / "clipboard.json")
    yield instance
    instance.close()


def test_delete_removes_image(repo):
    item = _add_image(repo, "a.png")
    path = repo.images_dir / "a.png"
    assert path.exists()

    repo.delete(item.id)
    assert not path.exists(), "删记录就该把它的图一起收走"


def test_delete_keeps_image_still_shared(repo):
    """同一张图被两条记录共用时，删一条不能删文件。

    图片按内容哈希命名，同一张截图被复制两次只落一份文件、两条记录共用它
    （`merge_duplicates` 关掉时就会这样）。无条件 `unlink` 会让另一条记录的图失联。
    """
    first = _add_image(repo, "shared.png", hash_="h1")
    second = _add_image(repo, "shared.png", hash_="h2")
    path = repo.images_dir / "shared.png"

    repo.delete(first.id)
    assert path.exists(), "还有记录在用这张图"
    repo.delete(second.id)
    assert not path.exists(), "没人用了才该删"


def test_clear_removes_images(repo):
    _add_image(repo, "a.png")
    _add_image(repo, "b.png")
    images = repo.images_dir

    repo.clear(keep_pinned=False)
    assert repo.count() == 0
    assert list(images.iterdir()) == [], "清空历史不该留下图片垃圾"


def test_clear_keeps_pinned_image(repo):
    _add_image(repo, "keep.png", pinned=True)
    _add_image(repo, "drop.png")

    repo.clear(keep_pinned=True)
    assert (repo.images_dir / "keep.png").exists()
    assert not (repo.images_dir / "drop.png").exists()


def test_enforce_limits_removes_evicted_images(repo):
    """淘汰是要删记录的三条路径之一，此前只有它和敏感过期漏了删图。"""
    for index in range(5):
        _add_image(repo, f"a{index}.png", created_at=1000.0 + index)

    removed = repo.enforce_limits(2)
    assert removed == 3
    survivors = {item.content for item in repo.list(limit=10)}
    assert survivors == {"a3.png", "a4.png"}
    remaining = {path.name for path in repo.images_dir.iterdir()}
    assert remaining == {"a3.png", "a4.png"}, "被淘汰的图必须一起收走"


def test_enforce_limits_by_days_removes_images(repo):
    import time as _time

    _add_image(repo, "old.png", created_at=_time.time() - 10 * 86400)
    _add_image(repo, "new.png", created_at=_time.time())

    assert repo.enforce_limits(0, max_days=1) == 1
    assert (repo.images_dir / "new.png").exists()
    assert not (repo.images_dir / "old.png").exists()


def test_purge_expired_sensitive_removes_images(repo):
    """敏感记录里最要紧的一类就是截图 —— 到期不删图等于没删。"""
    import time as _time

    _add_image(repo, "secret.png", sensitive=True, created_at=_time.time() - 100)
    _add_image(repo, "normal.png", created_at=_time.time() - 100)

    purged = repo.purge_expired_sensitive(1)
    assert purged == 1
    assert not (repo.images_dir / "secret.png").exists()
    assert (repo.images_dir / "normal.png").exists(), "非敏感记录不该被动"


def test_sweep_images_finds_orphans(repo):
    _add_image(repo, "used.png")
    (repo.images_dir / "orphan.png").write_bytes(PNG)

    result = repo.sweep_images(dry_run=True)
    assert result.removed == 1
    assert result.names == ["orphan.png"]
    assert result.kept == 1

    assert repo.sweep_images(dry_run=False).removed == 1
    assert {path.name for path in repo.images_dir.iterdir()} == {"used.png"}


def test_image_usage_reports_missing(repo):
    _add_image(repo, "here.png")
    _add_image(repo, "gone.png")
    (repo.images_dir / "gone.png").unlink()

    usage = repo.image_usage()
    assert usage.files == 1
    assert usage.missing == 1


def test_discard_unreferenced_image(repo):
    """采集期被拦下的截图：文件已落盘、却不会有记录引用它。"""
    directory = repo.images_dir
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "doomed.png").write_bytes(PNG)

    assert repo.discard_unreferenced_image("images/doomed.png") is True
    assert not (directory / "doomed.png").exists()


def test_discard_unreferenced_image_keeps_shared(repo):
    """同一份文件已被别的记录引用时，回收请求必须被拒绝。

    否则「复制同一张图两次、第二次被隐私拦下」会把第一次的图删掉。
    """
    _add_image(repo, "shared.png")
    path = repo.images_dir / "shared.png"

    assert repo.discard_unreferenced_image("images/shared.png") is False
    assert path.exists()


def test_discard_unreferenced_image_ignores_data_uri(repo):
    assert repo.discard_unreferenced_image("data:image/png;base64,AAAA") is False
    assert repo.discard_unreferenced_image("") is False


# ---- 采集侧：被拦下的截图不留孤儿 ----


@pytest.fixture()
def collector_env(tmp_path):
    from linglong_clip.core.collector import Collector
    from linglong_clip.core.storage import Storage
    from linglong_clip.utils.config import Config

    config = Config(tmp_path / "config.json")

    def build(**overrides):
        config.update(overrides)
        storage = Storage(tmp_path / "history.db")
        return Collector(storage, config), storage

    return build


def _stage_image(storage, name="shot.png"):
    storage.images_dir.mkdir(parents=True, exist_ok=True)
    (storage.images_dir / name).write_bytes(PNG)
    return ClipboardItem(
        kind="image", content=name, image_path=f"images/{name}", hash=None
    )


def test_capture_skipped_by_type_setting_leaves_no_file(collector_env):
    """类型开关关掉时图片不落库，落盘的那份要收回来。

    图片是在读取阶段（`_from_image`）就写下的，判定在之后的 `process()` 里。
    以前这里只返回 `skipped_type` 就完事，文件永远留在磁盘上。
    """
    collector, storage = collector_env(**{"capture.types": ["text"]})
    item = _stage_image(storage)

    outcome, saved = collector.process(item)
    assert outcome == "skipped_type"
    assert saved is None
    assert list(storage.images_dir.iterdir()) == []


def test_capture_blocked_by_privacy_leaves_no_file(collector_env):
    """被隐私拦下的截图，内容恰恰可能是不该留的密钥或证件照。"""
    collector, storage = collector_env(**{"privacy.blocked_apps": ["vault"]})
    item = _stage_image(storage, "secret.png")

    from linglong_clip.platform.base import FocusInfo

    outcome, _ = collector.process(item, FocusInfo(app_id="vault", app_name="vault"))
    assert outcome == "skipped_privacy"
    assert list(storage.images_dir.iterdir()) == [], (
        "拦截不等于没发生过 —— 文件留在磁盘上等于没拦"
    )


def test_capture_sensitive_discard_leaves_no_file(collector_env):
    """敏感记录默认不落盘（`sensitive_retention_seconds = 0`）。"""
    collector, storage = collector_env(**{"privacy.sensitive_retention_seconds": 0})
    item = _stage_image(storage, "key.png")
    item.content = "-----BEGIN RSA PRIVATE KEY-----"

    outcome, _ = collector.process(item)
    assert outcome == "skipped_privacy"
    assert list(storage.images_dir.iterdir()) == []


def test_capture_success_keeps_file(collector_env):
    collector, storage = collector_env()
    item = _stage_image(storage)

    outcome, saved = collector.process(item)
    assert outcome == "captured"
    assert saved is not None
    assert (storage.images_dir / "shot.png").exists(), "正常入库的图当然要留着"


def test_capture_duplicate_merge_keeps_shared_file(collector_env):
    """重复内容被合并时，文件仍被那条存在的记录引用，不能回收。"""
    collector, storage = collector_env()
    first, saved = collector.process(_stage_image(storage))
    assert first == "captured"

    second, merged = collector.process(_stage_image(storage))
    assert second == "merged"
    assert merged.id == saved.id
    assert (storage.images_dir / "shot.png").exists()


# ---- 边界与异常路径 ----


def test_usage_as_dict_and_sweep_as_dict_shapes():
    usage = image_store.ImageUsage(files=2, bytes=10, missing=1)
    assert usage.as_dict() == {"files": 2, "bytes": 10, "missing": 1}

    result = image_store.SweepResult(removed=1, bytes=5, kept=3, dry_run=True)
    payload = result.as_dict()
    assert payload["removed"] == 1
    assert payload["dry_run"] is True
    assert "names" not in payload, "文件名单另行附上，不进汇总字典"


def test_iter_image_files_on_missing_directory(tmp_path):
    assert list(image_store.iter_image_files(tmp_path / "nope")) == []


def test_iter_image_files_honours_pattern(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / "a.png").write_bytes(PNG)
    (images / "b.webp").write_bytes(PNG)

    assert [p.name for p in image_store.iter_image_files(images)] == ["a.png"]
    assert [p.name for p in image_store.iter_image_files(images, pattern="*.webp")] == [
        "b.webp"
    ]


def test_sweep_unlink_failure_is_counted_not_raised(tmp_path, monkeypatch):
    """删不掉时记一笔继续走 —— 一次失败不该让整轮回收崩掉。"""
    images = tmp_path / "images"
    images.mkdir()
    (images / "junk.png").write_bytes(PNG)

    from pathlib import Path

    def boom(self):
        raise OSError("read-only")

    monkeypatch.setattr(Path, "unlink", boom)
    result = image_store.sweep(images, set(), dry_run=False)
    assert result.removed == 0
    assert result.errors == 1
    assert result.truncated is False


def test_unlink_failure_does_not_break_following_files(tmp_path, monkeypatch):
    images = tmp_path / "images"
    images.mkdir()
    (images / "a.png").write_bytes(PNG)
    (images / "b.png").write_bytes(PNG)

    from pathlib import Path

    original = Path.unlink
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("busy")
        return original(self)

    monkeypatch.setattr(Path, "unlink", flaky)
    result = image_store.sweep(images, set(), dry_run=False)
    assert result.errors == 1
    assert result.removed == 1, "一个文件删不掉不该连累后面的"


def test_referenced_names_accepts_generator():
    names = image_store.referenced_names(name for name in ["images/a.png", "b.png"])
    assert names == {"a.png", "b.png"}


def test_iter_image_files_survives_unreadable_directory(tmp_path, monkeypatch):
    images = tmp_path / "images"
    images.mkdir()

    from pathlib import Path

    def boom(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert list(image_store.iter_image_files(images)) == [], "读不了目录就当没有，别抛"


def test_sweep_skips_entry_whose_type_cannot_be_read(tmp_path, monkeypatch):
    images = tmp_path / "images"
    images.mkdir()
    (images / "a.png").write_bytes(PNG)

    from pathlib import Path

    def boom(self):
        raise OSError("stat failed")

    monkeypatch.setattr(Path, "is_symlink", boom)
    result = image_store.sweep(images, set(), dry_run=False)
    assert result.removed == 0
    assert result.errors == 0


# ---- 两个后端：`image_path` 的回填 ----
#
# 同步只带图片摘要，字节按需拉取到位后才回填 `image_path`。
# 两个后端各有一份 `update_fields` 的白名单，漏掉 `image_path` 的后果是
# 「换个存储方式，有的图打得开、有的打不开」—— 同一份语义两处实现，
# 就得用同一组断言把它们钉住。


def test_update_fields_accepts_image_path(repo):
    item = repo.add(
        ClipboardItem(
            kind="image", content="x.png", hash="h-img", metadata={"image_digest": "a" * 32}
        )
    )
    repo.update_fields(item.id, image_path="images/x.png")
    assert repo.get(item.id).image_path == "images/x.png"


def test_update_fields_metadata_patch_keeps_image_path(repo):
    """`metadata` 合并与 `image_path` 回填是两件独立的事，互不覆盖。"""
    item = repo.add(
        ClipboardItem(
            kind="image", content="y.png", hash="h-img2", metadata={"image_digest": "b" * 32}
        )
    )
    repo.update_fields(item.id, image_path="images/y.png")
    repo.update_fields(item.id, metadata={"pinned_by": "laptop"})

    loaded = repo.get(item.id)
    assert loaded.image_path == "images/y.png"
    assert loaded.metadata.get("pinned_by") == "laptop"
    # 摘要没被顺手抹掉 —— 合并语义下这是「别的写方只动自己的键」
    assert loaded.metadata.get("image_digest") == "b" * 32
