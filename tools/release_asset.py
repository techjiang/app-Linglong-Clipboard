#!/usr/bin/env python3
"""CNB Release 附件上传的两个小工具，给 .cnb.yml 的发布流水线用。

    release_asset.py rid upload.out                      # 从版本详情里取 release id
    release_asset.py upload upload.out PKG REPO RID      # 传文件并确认

为什么要单独一个脚本：cnb CLI 的输出是 YAML（没有 --json），
verify_url 里的 upload_token / asset_path 藏在 path 里而不是 query 里，
这两件事用 shell 拼都不好看，也容易拼错。
"""

from __future__ import annotations

import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import yaml


def load(path: str) -> dict[str, Any]:
    """读 cnb CLI 的输出。它前面会打 status/contentType 之类的行，data: 之后才是正文。"""
    text = Path(path).read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise SystemExit(f"{path} 不是预期的 YAML 输出")
    return parsed


def release_id(path: str) -> str:
    data = load(path).get("data") or {}
    return str(data.get("id") or "")


def parse_verify_url(url: str) -> tuple[str, str]:
    """从 verify_url 里拆出 (upload_token, asset_path)。

    形如
      .../asset-upload-confirmation/<token>/<URL 编码的 asset_path>?ttl=0
    token 和 asset_path 都在 path 段里，query 只有 ttl。
    """
    segments = urllib.parse.urlparse(url).path.split("/")
    if "asset-upload-confirmation" not in segments:
        raise SystemExit(f"verify_url 结构不认识：{url}")
    index = segments.index("asset-upload-confirmation")
    token = segments[index + 1]
    asset_path = urllib.parse.unquote("/".join(segments[index + 2 :]))
    if not token or not asset_path:
        raise SystemExit(f"verify_url 里缺 upload_token 或 asset_path：{url}")
    return token, asset_path


def upload(spec_path: str, package: str, repo: str, rid: str) -> None:
    data = load(spec_path)["data"]

    # 内容直传。用 -F 会变成 multipart，服务端拿到的不是原始字节。
    subprocess.run(
        [
            "curl", "-fsS", "-X", "PUT",
            "-H", "Content-Type: application/octet-stream",
            "--data-binary", f"@{package}",
            data["upload_url"],
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    token, asset_path = parse_verify_url(data["verify_url"])
    subprocess.run(
        [
            "cnb", "releases", "post-release-asset-upload-confirmation",
            "--repo", repo,
            "--release-id", rid,
            "--upload-token", token,
            "--asset-path", asset_path,
            "--ttl", "0",
        ],
        check=True,
    )


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    command = argv[1]
    if command == "rid":
        print(release_id(argv[2]))
    elif command == "upload":
        if len(argv) != 6:
            print("用法：release_asset.py upload upload.out PKG REPO RID", file=sys.stderr)
            return 2
        upload(argv[2], argv[3], argv[4], argv[5])
    else:
        print(f"未知命令：{command}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
