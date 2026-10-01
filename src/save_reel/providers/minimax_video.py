"""MiniMax H3 V2: submit, query, and download using separate authenticated requests."""

import base64
from urllib.parse import quote, urlparse

import httpx

from save_reel.media_models import BrollSettings
from save_reel.providers.media import MediaError, VideoTask


class MiniMaxVideoProvider:
    base_url = "https://api.minimax.io"

    def __init__(self, api_key: str, client: httpx.Client) -> None:
        self._api_key = api_key
        # The client must not have default Authorization headers: downloads use CDN URLs.
        if "authorization" in client.headers:
            raise ValueError("MiniMax client must not have default Authorization headers")
        self.client = client

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.client.request(
                method,
                self.base_url + path,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=120,
                follow_redirects=False,
                **kwargs,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MediaError(
                f"MiniMax request failed (HTTP {exc.response.status_code}). "
                "Check the pay-as-you-go API key, model access, and balance."
            ) from None
        except httpx.RequestError:
            raise MediaError(
                "MiniMax connection error or timeout; no generation retry was sent"
            ) from None
        try:
            body = response.json()
        except ValueError:
            raise MediaError("MiniMax returned a non-JSON response") from None
        if not isinstance(body, dict) or body.get("type") == "error" or body.get("error"):
            raise MediaError("MiniMax returned an API error")
        return body

    def submit(self, image: bytes, prompt: str, settings: BrollSettings) -> str:
        if not prompt.strip() or len(prompt) > 7000:
            raise MediaError("MiniMax video prompt must contain 1–7000 characters")
        if not image or len(image) > 30 * 1024 * 1024:
            raise MediaError("MiniMax first-frame image must be nonempty and at most 30 MB")
        data_url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        body = self._request(
            "POST",
            "/v2/video_generation",
            json={
                "model": settings.video_model,
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}, "role": "first_frame"},
                ],
                "duration": settings.duration,
                "resolution": settings.resolution,
                "ratio": "adaptive",
            },
        )
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise MediaError("MiniMax did not return a task ID; do not resubmit automatically")
        return task_id

    def query(self, task_id: str) -> VideoTask:
        body = self._request("GET", f"/v2/query/video_generation/{quote(task_id, safe='')}")
        task = body.get("task")
        if not isinstance(task, dict) or task.get("status") not in {
            "queued",
            "running",
            "succeeded",
            "failed",
            "cancelled",
        }:
            raise MediaError("MiniMax returned an unrecognized task status")
        content = task.get("content") or {}
        url = content.get("url") if isinstance(content, dict) else None
        if task["status"] == "succeeded" and not isinstance(url, str):
            raise MediaError("MiniMax finished without a video download URL")
        return VideoTask(status=task["status"], download_url=url, usage=task.get("usage") or {})

    def download(self, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise MediaError("MiniMax video download URL must use HTTPS without credentials")
        try:
            # Never send the API key to the media CDN, including after redirects.
            with self.client.stream("GET", url, timeout=120, follow_redirects=True) as response:
                response.raise_for_status()
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > 256 * 1024 * 1024:
                        raise MediaError("MiniMax video exceeds the 256 MB download limit")
                    chunks.append(chunk)
        except httpx.HTTPError:
            raise MediaError(
                "MiniMax video download failed; resume to obtain a fresh download URL"
            ) from None
        content = b"".join(chunks)
        if len(content) < 12 or content[4:8] != b"ftyp":
            raise MediaError("MiniMax download was not an MP4 file")
        return content
