import os
import json
import time
import base64
import asyncio
import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.message_components import Image, Plain

# ── Yaya-decode integration (bundled) ────────────────────────

try:
    from .yaya_decode import decode_duck_to_file as _decode_duck_to_file

    _YAYA_AVAILABLE = True
except ImportError as _e:
    _YAYA_AVAILABLE = False
    _YAYA_IMPORT_ERROR = str(_e)

# ── Defaults (overridden by config panel) ───────────────────

_DEFAULT_API_KEY = ""
_DEFAULT_BASE_URL = "https://www.runninghub.ai"
_DEFAULT_APP_ID = ""
_DEFAULT_MAX_POLL = 180
_DEFAULT_POLL_INTERVAL = 5
_DEFAULT_MAX_RETRIES = 2


# ── Plugin registration ─────────────────────────────────────

@register(
    "astrbot_plugin_Edit_Images_Hub",
    "SoYa",
    "RunningHub AI 图片编辑插件。引用图片 + /RH <提示词> 即可让 AI 修改图片；也支持 LLM 自动调用。",
    "1.1.0",
)
class EditImagesHub(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config or {}

        # ── Read config from panel ──
        self.api_key = str(cfg.get("api_key", _DEFAULT_API_KEY)).strip()
        self.base_url = str(cfg.get("base_url", _DEFAULT_BASE_URL)).strip().rstrip("/")
        self.app_id = str(cfg.get("app_id", _DEFAULT_APP_ID)).strip()
        self.max_poll = int(cfg.get("max_poll_seconds", _DEFAULT_MAX_POLL))
        self.poll_interval = int(cfg.get("poll_interval", _DEFAULT_POLL_INTERVAL))
        self.max_retries = int(cfg.get("max_retries", _DEFAULT_MAX_RETRIES))

        # ── Paths ──
        self.data_dir = str(StarTools.get_data_dir("astrbot_plugin_Edit_Images_Hub"))
        self.temp_dir = os.path.join(self.data_dir, "temp")
        os.makedirs(self.temp_dir, exist_ok=True)
        self._counter = 0

        logger.info(
            f"[RunningHub] 插件已初始化 "
            f"(poll={self.max_poll}s, interval={self.poll_interval}s, "
            f"retries={self.max_retries}, yaya={'OK' if _YAYA_AVAILABLE else 'MISSING'})"
        )

    async def terminate(self) -> None:
        logger.info("[RunningHub] 插件已停止。")

    # ═══════════════════════════════════════════════════════════
    #  Core pipeline  (shared by command + LLM tool)
    # ═══════════════════════════════════════════════════════════

    async def _do_edit(
        self,
        event: AstrMessageEvent,
        image_ref: str,
        prompt: str,
    ) -> str:
        """
        Execute the full RunningHub pipeline and send the result via event.send().

        Returns a status string suitable for the LLM to interpret.
        """
        last_error = None

        for attempt in range(1, self.max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    # 1. Download source image
                    local_path = await self._download_image(session, image_ref)
                    logger.info(f"[RunningHub] 图片已下载: {local_path}")

                    # 2. Upload to RunningHub
                    file_name = await self._upload_image(session, local_path)
                    logger.info(f"[RunningHub] 图片已上传: {file_name}")

                    # 3. Submit AI task
                    task_id = await self._submit_task(session, file_name, prompt)
                    logger.info(f"[RunningHub] 任务已提交: {task_id} (attempt {attempt})")

                    # 4. Poll until completion
                    result_url = await self._poll_task(session, task_id)

                    # 5. Download result
                    duck_path = await self._download_image(session, result_url)

                    # 6. Decode via yaya-decode
                    final_path = await self._decode_if_possible(duck_path)

                    # 7. Send the image to user
                    await event.send(event.chain_result([
                        Image.fromFileSystem(final_path),
                    ]))

                    return f"系统提示：已成功修改图片，提示词：{prompt}"

            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"[RunningHub] attempt {attempt}/{self.max_retries} 失败: {exc}"
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(2)

        raise last_error  # type: ignore[misc]

    async def _decode_if_possible(self, duck_path: str) -> str:
        """Decode a duck image via yaya-decode, falling back to the original."""
        if not _YAYA_AVAILABLE:
            logger.warning("[RunningHub] yaya-decode 不可用，使用原始图片")
            return duck_path

        logger.info("[RunningHub] 正在解码鸭图...")
        try:
            decoded = _decode_duck_to_file(
                duck_path=duck_path,
                output_dir=self.temp_dir,
                password="",
                output_stem="rh_result",
            )
            logger.info(
                f"[RunningHub] 解码成功: {decoded.path} "
                f"(ext={decoded.ext}, size={decoded.size})"
            )
            return str(decoded.path)
        except Exception as exc:
            logger.warning(f"[RunningHub] 鸭图解码失败，使用原始图片: {exc}")
            return duck_path

    # ═══════════════════════════════════════════════════════════
    #  Command handler  /RH <prompt>
    # ═══════════════════════════════════════════════════════════

    @filter.command("RH", alias={"rh"})
    async def cmd_rh(
        self,
        event: AstrMessageEvent,
        p1: str = "",
        p2: str = "",
        p3: str = "",
        p4: str = "",
        p5: str = "",
        p6: str = "",
        p7: str = "",
        p8: str = "",
        p9: str = "",
        p10: str = "",
    ):
        """Handle /RH <prompt> — edit the image being replied to."""

        # 1. Build prompt from command arguments
        prompt = " ".join(
            str(item) for item in [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10] if item
        ).strip()
        if not prompt:
            prompt = self._parse_prompt_from_text(event)

        if not prompt:
            yield event.plain_result(
                "请提供提示词，例如：\n/RH 把猫变成狗"
            )
            event.stop_event()
            return

        # 2. Extract image from reply
        image_ref = self._get_reply_image(event)
        if not image_ref:
            yield event.plain_result(
                "请引用（回复）一张图片再使用 /RH 命令。\n"
                "用法：在 QQ 中长按图片 → 引用，然后输入 /RH <提示词>"
            )
            event.stop_event()
            return

        # 3. Acknowledge
        yield event.plain_result("不要着急哦~我现在就重新拍一下,不会很久~")

        # 4. Run pipeline (sends result via event.send internally)
        try:
            await self._do_edit(event, image_ref, prompt)
        except Exception as e:
            logger.error(f"[RunningHub] 处理失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 处理失败：{e}")

        event.stop_event()

    # ═══════════════════════════════════════════════════════════
    #  LLM tool  (auto-invoked when user wants to edit an image)
    # ═══════════════════════════════════════════════════════════

    @filter.llm_tool(name="edit_image_runninghub")
    async def tool_edit_image(self, event: AstrMessageEvent, prompt: str) -> str:
        """
        使用 AI 修改用户引用/回复的图片。当用户在聊天中引用了一张图片并表达出想要修改该图片的意图时，调用此工具来执行图片编辑。

        Args:
            prompt (string): 图片编辑提示词，用中文或英文描述想要对图片做什么修改。根据用户的语言和意图自动生成合适的提示词。
        """
        # Unwrap the event (may be nested in LLM context)
        event = getattr(getattr(event, "context", event), "event", event)

        # Extract image from the replied message
        image_ref = self._get_reply_image(event)
        if not image_ref:
            return (
                "系统提示：用户未引用图片。请告知用户需要先引用（回复）一张图片，"
                "然后再用自然语言提出修改需求，无需使用 /RH 命令。"
            )

        # Send a quick heads-up
        await event.send(event.plain_result("好吧~ 我再拍一下..."))

        # Run the full pipeline
        try:
            return await self._do_edit(event, image_ref, prompt)
        except Exception as e:
            logger.error(f"[RunningHub] LLM 工具失败: {e}", exc_info=True)
            return f"系统提示：图片编辑失败 ({e})。请告知用户稍后重试，或建议用户尝试使用 /RH 命令手动操作。"

    # ═══════════════════════════════════════════════════════════
    #  Prompt parsing
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _parse_prompt_from_text(event: AstrMessageEvent) -> str:
        """Fallback: extract prompt when AstrBot args are empty."""
        text = event.get_message_str().strip()
        if not text:
            return ""

        lower = text.lower()
        for prefix in ("/rh ", "/rh"):
            if lower.startswith(prefix):
                result = text[len(prefix):].strip()
                if result:
                    return result

        if not text.startswith("/"):
            return text
        return ""

    # ═══════════════════════════════════════════════════════════
    #  Image extraction from reply
    # ═══════════════════════════════════════════════════════════

    def _get_reply_image(self, event: AstrMessageEvent) -> str | None:
        """Extract image URL/path from the message being replied to."""
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return None

        # Strategy 1: quote object (most reliable for OneBot)
        quote_obj = getattr(msg_obj, "quote", None)
        if quote_obj is not None:
            found = self._search_image_in_obj(quote_obj)
            if found:
                return found

        # Strategy 2: Reply component with embedded chain
        for comp in event.get_messages():
            if type(comp).__name__ == "Reply":
                chain = getattr(comp, "chain", None)
                if chain:
                    found = self._search_image_in_obj(chain)
                    if found:
                        return found

        return None

    def _search_image_in_obj(self, obj) -> str | None:
        """Recursively search an object tree for an Image component."""
        if obj is None:
            return None
        if isinstance(obj, str):
            return None

        if isinstance(obj, Image) or type(obj).__name__ == "Image":
            return self._extract_image_ref(obj)

        if isinstance(obj, (list, tuple)):
            for item in obj:
                found = self._search_image_in_obj(item)
                if found:
                    return found
            return None

        if hasattr(obj, "__dict__"):
            skip_keys = {"context", "star", "bot", "config", "session", "provider"}
            for key, val in vars(obj).items():
                if key in skip_keys:
                    continue
                found = self._search_image_in_obj(val)
                if found:
                    return found

        return None

    @staticmethod
    def _extract_image_ref(img) -> str | None:
        """Given an Image-like object, extract its URL or local file path."""
        url = getattr(img, "url", None)
        if url:
            return str(url)
        for attr in ("path", "file", "file_path"):
            val = getattr(img, attr, None)
            if val:
                return str(val)
        return None

    # ═══════════════════════════════════════════════════════════
    #  HTTP / IO helpers
    # ═══════════════════════════════════════════════════════════

    def _temp_path(self, prefix: str, ext: str = "png") -> str:
        self._counter += 1
        ts = int(time.time() * 1000)
        return os.path.join(self.temp_dir, f"{prefix}_{ts}_{self._counter}.{ext}")

    async def _download_image(
        self, session: aiohttp.ClientSession, url: str
    ) -> str:
        if os.path.isfile(url):
            return url

        if url.startswith("data:image"):
            header, encoded = url.split(",", 1)
            ext = "png"
            if "/" in header:
                mime_part = header.split("/")[1]
                ext = mime_part.split(";")[0] if ";" in mime_part else mime_part
            file_path = self._temp_path("src", ext)
            data = base64.b64decode(encoded)
            with open(file_path, "wb") as f:
                f.write(data)
            return file_path

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"下载图片失败，HTTP {resp.status}")

            chunks = []
            total = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > 20 * 1024 * 1024:
                    raise RuntimeError("图片超过 20MB 大小限制")
                chunks.append(chunk)
            data = b"".join(chunks)

            content_type = resp.headers.get("Content-Type", "")
            ext = "png"
            if "jpeg" in content_type or "jpg" in content_type:
                ext = "jpg"
            elif "gif" in content_type:
                ext = "gif"
            elif "webp" in content_type:
                ext = "webp"

            file_path = self._temp_path("src", ext)
            with open(file_path, "wb") as f:
                f.write(data)
            return file_path

    async def _upload_image(
        self, session: aiohttp.ClientSession, file_path: str
    ) -> str:
        url = f"{self.base_url}/openapi/v2/media/upload/binary"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        with open(file_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f, filename=os.path.basename(file_path))
            async with session.post(url, headers=headers, data=form) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"上传图片失败: HTTP {resp.status}, {text}")
                result = await resp.json()
                inner = result.get("data", result)
                file_name = inner.get("fileName")
                if file_name:
                    return file_name
                download_url = inner.get("download_url")
                if download_url:
                    return download_url
                raise RuntimeError(f"上传成功但未获取到文件引用: {result}")

    async def _submit_task(
        self, session: aiohttp.ClientSession, image_ref: str, prompt: str
    ) -> str:
        url = f"{self.base_url}/openapi/v2/run/ai-app/{self.app_id}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "nodeInfoList": [
                {
                    "nodeId": "7",
                    "fieldName": "image",
                    "fieldValue": image_ref,
                },
                {
                    "nodeId": "10",
                    "fieldName": "text",
                    "fieldValue": prompt,
                },
                {
                    "nodeId": "30",
                    "fieldName": "prompt",
                    "fieldValue": (
                        "解码工具：https://github.com/T8mars/yaya-decode\n"
                        "整合包：https://github.com/T8mars/yaya-decode/releases/tag/v1.0"
                    ),
                },
            ],
            "instanceType": "default",
            "usePersonalQueue": "false",
        }

        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"提交任务失败: HTTP {resp.status}, {text}")
            result = await resp.json()
            task_id = result.get("taskId")
            if not task_id:
                raise RuntimeError(f"提交成功但未获取到 taskId: {result}")
            return task_id

    async def _poll_task(
        self, session: aiohttp.ClientSession, task_id: str
    ) -> str:
        url = f"{self.base_url}/openapi/v2/query"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        elapsed = 0
        while elapsed < self.max_poll:
            await asyncio.sleep(self.poll_interval)
            elapsed += self.poll_interval

            async with session.post(
                url, headers=headers, json={"taskId": task_id}
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"查询状态失败: HTTP {resp.status}, {text}")
                result = await resp.json()
                status = result.get("status", "UNKNOWN")

                if status == "SUCCESS":
                    results = result.get("results", [])
                    if results and len(results) > 0:
                        output_url = results[0].get("url")
                        if output_url:
                            return output_url
                    raise RuntimeError("任务完成但未获取到结果图片 URL")

                elif status in ("RUNNING", "QUEUED"):
                    logger.debug(
                        f"[RunningHub] {task_id} status={status} elapsed={elapsed}s"
                    )
                    continue

                else:
                    error_msg = result.get("errorMessage", "未知错误")
                    raise RuntimeError(f"RunningHub 任务失败: {error_msg}")

        raise RuntimeError(f"任务超时（超过 {self.max_poll} 秒）")
