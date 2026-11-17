from __future__ import annotations
from datetime import date
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import settings as settings_repo


def _parse_day(raw: str | None) -> date:
    text = (raw or "").strip()
    if not text:
        raise HTTPException(400, "营业日不能为空")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise HTTPException(400, "营业日格式应为 YYYY-MM-DD")


def get_day() -> dict[str, Any]:
    conn = connect()
    try:
        return {"business_day": settings_repo.get(conn, "business_day")}
    finally:
        conn.close()


def set_day(body: dict[str, Any]) -> dict[str, Any]:
    """拨钟写入口（全局唯一）。

    互斥策略「以未封扫描为准、有未封则拒拨」：目标营业日严格新于当前钟面时，
    若 close_repo.unsealed_days 扫出任一 business_day < 目标日且尚无 sealed
    现行批的营业日，拨钟失败（HTTP 409），钟面必须保持拨前值，回执原样列出
    未封日；须先封账后才能拨到目标日。回拨与同日设置不触发该互斥。

    收口约束：
    - 拒拨路径在任何写钟面动作之前抛出，禁止「回执失败钟已变」；
    - 回执清单与 /close/unsealed、/dashboard 共用同一扫描，不经任何过滤，
      禁止「清单藏未封」；扫描本身排除已 sealed 日，禁止「清单含已封」。
    """
    target = _parse_day(body.get("business_day"))
    conn = connect(immediate=True)
    try:
        current_raw = settings_repo.get(conn, "business_day")
        if current_raw:
            try:
                current = date.fromisoformat(current_raw)
            except ValueError:
                current = None
        else:
            current = None

        if current is None or target > current:
            unsealed = close_repo.unsealed_days(conn, target.isoformat())
            if unsealed:
                # 先拒后留：不写钟面、不提交，rollback 后钟面仍是拨前值。
                raise HTTPException(
                    409,
                    detail={
                        "message": (
                            f"存在 {len(unsealed)} 个未封账营业日，须先封账后才能拨到 {target.isoformat()}"
                        ),
                        "business_day": current_raw,
                        "target_business_day": target.isoformat(),
                        "unsealed_days": unsealed,
                    },
                )

        settings_repo.set_value(conn, "business_day", target.isoformat())
        conn.commit()
        return {
            "business_day": target.isoformat(),
            "previous_business_day": current_raw,
            "unsealed_days": [],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
