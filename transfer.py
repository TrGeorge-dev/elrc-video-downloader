"""ELRC MP4 streaming transfer; no credentials or source URLs are saved to disk."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import struct
import threading
from typing import Callable
from urllib.parse import urljoin, urlsplit

import requests


class DownloadError(RuntimeError):
    pass


class Cancelled(DownloadError):
    pass


def validate_mp4(path: Path) -> dict:
    """Check every top-level ISO-BMFF box boundary, including 64-bit sizes."""
    path = Path(path)
    byte_size = path.stat().st_size
    boxes = []
    with path.open("rb") as stream:
        offset = 0
        while offset < byte_size:
            if byte_size - offset < 8:
                raise DownloadError("MP4 文件末尾不完整，保留临时文件供重试。")
            size, kind = struct.unpack(">I4s", stream.read(8))
            header_size = 8
            if size == 1:
                raw = stream.read(8)
                if len(raw) != 8:
                    raise DownloadError("MP4 扩展头不完整。")
                size = struct.unpack(">Q", raw)[0]
                header_size = 16
            elif size == 0:
                size = byte_size - offset
            if size < header_size or offset + size > byte_size:
                raise DownloadError("MP4 数据长度校验失败，文件尚未完成。")
            boxes.append(kind.decode("ascii", errors="replace"))
            offset += size
            stream.seek(offset)
    if not {"ftyp", "moov", "mdat"}.issubset(boxes):
        raise DownloadError("下载内容不是完整 MP4，可能是登录页或服务器错误页。")
    return {"byte_size": byte_size, "boxes": boxes}


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _validator(meta: dict) -> str | None:
    etag = meta.get("etag")
    if isinstance(etag, str) and etag and not etag.startswith("W/"):
        return etag
    return meta.get("last_modified") or None


def _response(session: requests.Session, url: str, headers: dict, cancel: threading.Event):
    initial = urlsplit(url)
    for _ in range(6):
        if cancel.is_set():
            raise Cancelled("已取消，临时文件已保留。")
        try:
            response = session.get(url, headers=headers, stream=True,
                                   allow_redirects=False, timeout=(15, 30))
        except requests.RequestException:
            raise DownloadError("无法连接视频服务器，请检查网络后重试。") from None
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location")
        response.close()
        next_url = urljoin(url, location or "")
        parsed = urlsplit(next_url)
        if (not location or parsed.hostname != initial.hostname or parsed.port != initial.port or parsed.username
                or parsed.password or parsed.scheme not in {"http", "https"}
                or (initial.scheme == "https" and parsed.scheme != "https")):
            raise DownloadError("视频地址发生了不受支持的跳转，请重新解析课程页面。")
        url = next_url
    raise DownloadError("视频地址跳转过多，请重新登录后重试。")


def download_media(session: requests.Session, url: str, target: Path,
                   headers: dict, cancel: threading.Event,
                   progress: Callable[[int, int | None], None],
                   log: Callable[[str], None]) -> Path:
    """Download without overwriting other files; resume only validated same-source bytes."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    meta_path = target.with_name(target.name + ".part.json")
    done_path = target.with_name(target.name + ".elrc.json")
    source_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    if target.exists():
        done = _load(done_path)
        if (done.get("source_sha256") == source_hash
                and done.get("byte_size") == target.stat().st_size):
            validate_mp4(target)
            log("该视频已经下载完成，已校验现有文件。")
            progress(target.stat().st_size, target.stat().st_size)
            return target
        raise DownloadError("保存位置已有同名文件，请改名或选择其他文件夹；未覆盖原文件。")

    old = _load(meta_path)
    offset = part.stat().st_size if part.exists() else 0
    if part.exists() and old.get("source_sha256") != source_hash:
        raise DownloadError("同名临时文件属于其他下载或缺少记录，请改名或选择其他文件夹。")
    resume = offset > 0 and bool(_validator(old))
    if old.get("expected_size") is not None and offset > old["expected_size"]:
        raise DownloadError("临时文件大于记录的视频长度，请换一个文件名重新下载。")
    if offset and offset == old.get("expected_size"):
        if cancel.is_set():
            raise Cancelled("已取消，临时文件已保留。")
        checked = validate_mp4(part)
        if target.exists():
            raise DownloadError("保存位置出现同名文件，未覆盖原文件。")
        os.rename(part, target)
        _save(done_path, {**old, "byte_size": checked["byte_size"]})
        meta_path.unlink(missing_ok=True)
        progress(offset, offset)
        log("临时文件已完整，校验后保存成功。")
        return target
    if offset and not resume:
        log("临时文件没有可验证的版本标记，正在从头下载。")
        offset = 0
    request_headers = {k: v for k, v in headers.items()
                       if k.lower() not in {"range", "if-range", "accept-encoding"}}
    request_headers["Accept-Encoding"] = "identity"
    if resume:
        request_headers.update({"Range": f"bytes={offset}-", "If-Range": _validator(old)})
    response = _response(session, url, request_headers, cancel)
    with response:
        status = response.status_code
        if status in {401, 403}:
            raise DownloadError("登录已失效或当前账号无权访问视频，请重新登录后解析。")
        if status not in {200, 206}:
            raise DownloadError(f"视频服务器返回 HTTP {status}，请稍后重新解析并重试。")
        content_type = response.headers.get("Content-Type", "").lower()
        if "html" in content_type or "json" in content_type or content_type.startswith("text/"):
            raise DownloadError("服务器返回了网页而不是视频，请重新登录后解析。")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise DownloadError("服务器返回压缩传输，无法安全断点续传，请稍后重试。")
        try:
            length = int(response.headers["Content-Length"]) if "Content-Length" in response.headers else None
            if length is not None and length < 0:
                raise ValueError
        except ValueError:
            raise DownloadError("服务器的视频长度格式无效。") from None
        if status == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
            start = offset if resume else 0
            if not match:
                raise DownloadError("服务器未返回正确的断点范围，已保留临时文件。")
            first, last, total = map(int, match.groups())
            if first != start or last < first or last != total - 1 or (length is not None and length != last - first + 1):
                raise DownloadError("服务器返回的断点范围不匹配，已保留临时文件。")
            if resume:
                if (old.get("expected_size") is not None and old["expected_size"] != total
                        or old.get("etag") and response.headers.get("ETag") != old["etag"]
                        or not old.get("etag") and old.get("last_modified")
                        and response.headers.get("Last-Modified") != old["last_modified"]):
                    raise DownloadError("服务器的视频版本已改变，请换一个文件名重新下载。")
        else:
            total = length
            if offset:
                log("服务器未接受续传或没有版本标记，正在从头下载。")
            offset = 0
        meta = {"version": 1, "source_sha256": source_hash, "expected_size": total,
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified")}
        written = offset
        progress(written, total)
        try:
            with part.open("ab" if offset else "wb") as stream:
                # Truncate old data before replacing its version metadata.
                _save(meta_path, meta)
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if cancel.is_set():
                        raise Cancelled("已取消，临时文件已保留，下次可继续。")
                    if not chunk:
                        continue
                    stream.write(chunk)
                    written += len(chunk)
                    progress(written, total)
                stream.flush()
                os.fsync(stream.fileno())
        except requests.RequestException:
            raise DownloadError("下载连接中断，临时文件已保留；再次下载可尝试续传。") from None
    if cancel.is_set():
        raise Cancelled("已取消，临时文件已保留，下次可继续。")
    if total is not None and written != total:
        raise DownloadError("视频长度不完整，临时文件已保留，请再次下载。")
    checked = validate_mp4(part)
    if target.exists():
        raise DownloadError("下载期间出现了同名文件；下载内容保存在 .part，未覆盖原文件。")
    # On Windows rename is atomic and fails if a destination appeared meanwhile.
    os.rename(part, target)
    _save(done_path, {**meta, "byte_size": checked["byte_size"]})
    meta_path.unlink(missing_ok=True)
    log("下载完成，已校验文件长度和 MP4 文件结构。")
    return target
