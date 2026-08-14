#!/usr/bin/env python3
"""MCP Server：将智谱 GLM-4.6V-Flash 视觉模型封装为 MCP 工具。

支持：
- 本地图片路径（自动转 base64 data URI）、http(s) 图片 URL、data URI
- 一张或多张图片同时分析（GLM-4.6V-Flash 也支持视频 URL，可一并传入）
- 思考模式开关（thinking）

接口（OpenAI 兼容）：
POST https://open.bigmodel.cn/api/paas/v4/chat/completions
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import mimetypes
import os
import ssl
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp.server import MCPServer
from pydantic import Field

# 减少 httpx 调试日志对 stdio 输出的干扰
logging.getLogger("httpx").setLevel(logging.WARNING)

# Windows 下 mimetypes 可能不识别 webp，补上显式映射
mimetypes.add_type("image/webp", ".webp")

logger = logging.getLogger("glm-vision")

DEFAULT_MODEL = "glm-4.6v-flash"
DEFAULT_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_TOKENS = 1024
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0

mcp = MCPServer(name="glm-vision", version="0.1.0")


MAX_ENV_LOOKUP_LEVELS = 5


def _find_env_file() -> Path | None:
    """从当前工作目录开始，再回退到 server.py 所在目录，逐级向上查找 .env。

    兼容“用户直接运行某个脚本/Notebook 单元”的场景：无论从哪里启动，
    只要 .env 在项目内（最多向上 5 层），都能被找到。
    """
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        cur = start.resolve()
        for _ in range(MAX_ENV_LOOKUP_LEVELS + 1):
            candidate = cur / ".env"
            if candidate.is_file():
                return candidate
            if cur.parent == cur:
                break
            cur = cur.parent
    return None


def _load_dotenv(override: bool = True) -> None:
    """极简 .env 加载：向上查找 .env（最多 5 层），读到后写入 os.environ。

    override=True 时覆盖已存在的同名变量，保证 Notebook/脚本在任意目录
    运行时都能拿到项目 .env 里的配置（因此 .env 与 Codex config.toml
    中注入的 Key 应保持一致）。
    """
    env_file = _find_env_file()
    if env_file is None:
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


_load_dotenv()


def _get_api_key() -> str:
    key = os.getenv("ZHIPU_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "缺少 ZHIPU_API_KEY。请先到 https://open.bigmodel.cn/ 创建 API Key，"
            "然后设置环境变量，例如 PowerShell：$env:ZHIPU_API_KEY=\"你的Key\""
        )
    return key


def _to_image_ref(image: str) -> str:
    """把本地路径 / URL / data URI 统一成接口需要的 url 字段。"""
    image = image.strip()
    if image.startswith(("http://", "https://", "data:")):
        return image
    path = Path(image)
    if not path.is_file():
        raise FileNotFoundError(f"找不到图片文件：{image}")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
) -> httpx.Response:
    """POST 请求，仅对 429 限流与临时网络错误做指数退避重试。"""
    for attempt in range(max_retries + 1):
        try:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 429 and attempt < max_retries:
                delay = base_delay * (2**attempt)
                logger.warning("智谱 API 限流(429)，%.1f 秒后第 %d 次重试", delay, attempt + 1)
                await asyncio.sleep(delay)
                continue
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError:
            raise  # 非 429 的 HTTP 错误（或 429 重试耗尽）统一由上层处理
        except (httpx.TimeoutException, httpx.HTTPError, ssl.SSLError, OSError) as exc:
            if attempt >= max_retries:
                raise
            delay = base_delay * (2**attempt)
            logger.warning(
                "智谱 API 临时网络错误(%s)，%.1f 秒后第 %d 次重试",
                type(exc).__name__,
                delay,
                attempt + 1,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


async def analyze_images(
    images: list[str],
    prompt: str = "请详细描述这张图片的内容。",
    thinking: bool = False,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    include_reasoning: bool = False,
    *,
    api_key: str | None = None,
    model: str | None = None,
    api_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """调用智谱 GLM-4.6V-Flash，分析一张或多张图片并返回文本结果。"""
    if not images:
        raise ValueError("images 不能为空，请至少提供一张图片（本地路径 / URL / data URI）。")

    payload: dict[str, Any] = {
        "model": model or os.getenv("GLM_VISION_MODEL", DEFAULT_MODEL),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _to_image_ref(img)}}
                    for img in images
                ]
                + [{"type": "text", "text": prompt}],
            }
        ],
        "max_tokens": max_tokens or int(os.getenv("GLM_VISION_MAX_TOKENS", DEFAULT_MAX_TOKENS)),
    }
    if thinking:
        payload["thinking"] = {"type": "enabled"}

    headers = {
        "Authorization": f"Bearer {api_key or _get_api_key()}",
        "Content-Type": "application/json",
    }
    url = api_url or os.getenv("GLM_VISION_API_URL", DEFAULT_API_URL)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await _post_with_retry(client, url, headers, payload)
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500]
        raise RuntimeError(f"智谱 API 返回 {exc.response.status_code}：{detail}") from exc
    except httpx.TimeoutException as exc:
        raise TimeoutError("请求智谱 API 超时，请稍后重试或调大 GLM_VISION_TIMEOUT。") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"请求智谱 API 失败：{exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"请求智谱 API 失败：{exc}") from exc

    try:
        choice = data["choices"][0]
        message = choice.get("message", {})
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"智谱 API 响应格式异常：{data}") from exc

    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    content = str(content).strip()

    reasoning = message.get("reasoning_content")
    if include_reasoning and reasoning:
        return f"[思考过程]\n{reasoning}\n\n[回答]\n{content}".strip()
    return content


@mcp.tool()
async def analyze_image(
    image_path: str | None = Field(
        default=None,
        description="单张图片：本地文件路径 / http(s) 图片或视频 URL / data URI（与 images 二选一）",
    ),
    images: list[str] | None = Field(
        default=None,
        description="多张图片列表，每项为本地文件路径 / http(s) URL / data URI（与 image_path 二选一；同时传入时会在列表后追加 image_path）",
    ),
    prompt: str = Field(
        default="请详细描述这张图片的内容。",
        description="要模型执行的指令，例如 OCR、表格解析、内容理解、缺陷检测等",
    ),
    thinking: bool = Field(
        default=False,
        description="是否开启思考模式（true 时模型会先输出 reasoning_content，响应更慢但更细致）",
    ),
    max_tokens: int = Field(
        default=DEFAULT_MAX_TOKENS,
        description="生成的最大 token 数",
        ge=1,
        le=8192,
    ),
    include_reasoning: bool = Field(
        default=False,
        description="开启思考模式时，是否把思考过程一并返回",
    ),
) -> str:
    """分析一张或多张图片（支持 image_path 单图或 images 多图；本地路径、http(s) 链接或 data URI），可用于 OCR、图表解析、内容理解、缺陷检测等。"""
    if images is None and image_path is None:
        raise ValueError("请至少提供 image_path 或 images 中的一个。")
    if images is None:
        images = [image_path]
    elif image_path is not None:
        images = list(images) + [image_path]
    return await analyze_images(
        images=images,
        prompt=prompt,
        thinking=thinking,
        max_tokens=max_tokens,
        include_reasoning=include_reasoning,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="智谱 GLM-4.6V-Flash 视觉模型 MCP Server"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP 传输方式，默认 stdio（绝大多数客户端使用）",
    )
    parser.add_argument(
        "--test-image",
        help="不使用 MCP，直接对一张图片发起一次识别请求（用于验证 API Key 是否可用）",
    )
    parser.add_argument("--prompt", default="请详细描述这张图片的内容。")
    parser.add_argument("--thinking", action="store_true", help="开启思考模式")
    parser.add_argument("--max-tokens", type=int, default=None)
    args = parser.parse_args()

    if args.test_image:
        try:
            result = asyncio.run(
                analyze_images(
                    [args.test_image],
                    prompt=args.prompt,
                    thinking=args.thinking,
                    max_tokens=args.max_tokens,
                    include_reasoning=True,
                )
            )
            print(result)
        except (RuntimeError, FileNotFoundError, TimeoutError, OSError) as exc:
            print(f"测试失败：{exc}", file=sys.stderr)
            sys.exit(1)
        return

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
