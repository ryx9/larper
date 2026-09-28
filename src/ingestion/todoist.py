from __future__ import annotations

import asyncio
from email.utils import parsedate_to_datetime
import json
from datetime import date, datetime, time, timedelta, timezone

import httpx


class TodoistClient:
    """Small Todoist REST API client using the user's personal API token."""

    BASE_URL = "https://api.todoist.com/api/v1"

    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20.0,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, endpoint: str, **kwargs) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        for attempt in range(5):
            response = await self._client.request(
                method, endpoint, headers=headers, **kwargs
            )
            if response.status_code != 429:
                response.raise_for_status()
                return response
            if attempt == 4:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After", "1")
            try:
                delay = max(0.0, float(retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    delay = max(
                        0.0,
                        (retry_at - datetime.now(timezone.utc)).total_seconds(),
                    )
                except (TypeError, ValueError, OverflowError):
                    delay = 1.0
            await asyncio.sleep(delay)
        raise RuntimeError("Todoist request retry limit exceeded")

    async def _list(self, endpoint: str, params: dict | None = None) -> list[dict]:
        results: list[dict] = []
        query = {"limit": 200, **(params or {})}
        while True:
            response = await self._request("GET", endpoint, params=query)
            payload = response.json()
            if isinstance(payload, list):
                results.extend(payload)
                break
            results.extend(payload.get("results", []))
            cursor = payload.get("next_cursor")
            if not cursor:
                break
            query["cursor"] = cursor
        return results

    async def list_tasks(self) -> list[dict]:
        return await self._list("/tasks")

    async def list_projects(self) -> list[dict]:
        return await self._list("/projects")

    async def list_recently_completed(self) -> list[dict]:
        today = date.today()
        start = today - timedelta(days=89)
        return await self._list(
            "/tasks/completed/by_completion_date",
            {
                "since": datetime.combine(start, time.min, timezone.utc).isoformat(),
                "until": datetime.combine(today, time.max, timezone.utc).isoformat(),
            },
        )

    async def create_task(self, task: dict, request_id: str | None = None) -> dict:
        headers = {"X-Request-Id": request_id} if request_id else None
        response = await self._request(
            "POST", "/tasks", json=self._task_payload(task), headers=headers or {}
        )
        return response.json()

    async def update_task(
        self,
        task_id: str,
        task: dict,
        current_project_id: str | None = None,
    ) -> dict:
        target_project_id = task.get("todoist_project_id")
        if target_project_id and str(target_project_id) != str(current_project_id):
            await self.move_task(task_id, str(target_project_id))

        payload = self._task_payload(task)
        payload.pop("project_id", None)
        response = await self._request(
            "POST", f"/tasks/{task_id}", json=payload
        )
        return response.json()

    async def move_task(self, task_id: str, project_id: str) -> None:
        await self._request(
            "POST",
            f"/tasks/{task_id}/move",
            json={"project_id": str(project_id)},
        )

    @staticmethod
    def _task_payload(task: dict) -> dict:
        payload = {
            "content": task["title"],
            "priority": _todoist_priority(task["priority"]),
            "labels": _labels(task["tags"]),
        }
        due_date = task.get("due_date")
        if due_date:
            if "T" in due_date:
                payload["due_datetime"] = due_date
                if task.get("todoist_due_timezone"):
                    payload["due_timezone"] = task["todoist_due_timezone"]
            else:
                payload["due_date"] = due_date[:10]
        project_id = task.get("todoist_project_id")
        if project_id:
            payload["project_id"] = str(project_id)
        return payload

    async def set_completed(self, task_id: str, is_done: bool) -> None:
        action = "close" if is_done else "reopen"
        await self._request("POST", f"/tasks/{task_id}/{action}")

    async def delete_task(self, task_id: str) -> None:
        try:
            await self._request("DELETE", f"/tasks/{task_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise


def _todoist_priority(value: object) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        return {"low": 2, "medium": 3, "high": 4}.get(str(value).lower(), 1)
    return max(1, min(priority, 4))


def _labels(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(label).lstrip("#") for label in value if str(label).strip()]
    try:
        decoded = json.loads(str(value))
        if isinstance(decoded, list):
            return [str(label).lstrip("#") for label in decoded if str(label).strip()]
    except (TypeError, ValueError):
        pass
    return [
        label.strip().lstrip("#")
        for label in str(value).replace(",", " ").split()
        if label.strip()
    ]