"""MiniMax Hailuo V1 adapter, using the same checkpointed video interface as H3."""

import base64

from save_reel.media_models import BrollSettings
from save_reel.providers.media import MediaError, VideoTask
from save_reel.providers.minimax_video import MiniMaxVideoProvider


class HailuoVideoProvider(MiniMaxVideoProvider):
    def submit(self, image: bytes, prompt: str, settings: BrollSettings) -> str:
        if settings.video_model != "MiniMax-Hailuo-02":
            raise MediaError("The Hailuo adapter requires MiniMax-Hailuo-02 settings")
        if not prompt.strip() or len(prompt) > settings.video_prompt_limit:
            raise MediaError("Hailuo video prompt must contain 1–2000 characters; use template v3")
        if not image or len(image) >= 20 * 1024 * 1024:
            raise MediaError("Hailuo first-frame image must be nonempty and smaller than 20 MB")
        body = self._request("POST", "/v1/video_generation", json={
            "model": settings.video_model,
            "first_frame_image": "data:image/png;base64," + base64.b64encode(image).decode("ascii"),
            "prompt": prompt,
            "prompt_optimizer": False,
            "duration": settings.duration,
            "resolution": settings.resolution,
        })
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise MediaError("MiniMax did not return a task ID; do not resubmit automatically")
        return task_id

    def query(self, task_id: str) -> VideoTask:
        body = self._request("GET", "/v1/query/video_generation", params={"task_id": task_id})
        status = {
            "Preparing": "queued", "Queueing": "queued", "Processing": "running",
            "Success": "succeeded", "Fail": "failed",
        }.get(body.get("status"))
        if status is None:
            raise MediaError("Hailuo returned an unrecognized task status")
        url = None
        if status == "succeeded":
            file_id = body.get("file_id")
            if not isinstance(file_id, str) or not file_id.strip():
                raise MediaError("Hailuo finished without a video file ID")
            result = self._request("GET", "/v1/files/retrieve", params={"file_id": file_id})
            file = result.get("file")
            url = file.get("download_url") if isinstance(file, dict) else None
            if not isinstance(url, str) or not url.strip():
                raise MediaError("Hailuo finished without a video download URL")
        return VideoTask(status=status, download_url=url, usage=body.get("usage") or {})
