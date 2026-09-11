"""FastAPI 应用入口。

本机运行：
    uvicorn punchtape.app.main:app --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import init_db
from .routers import codetables, jobs, tapes


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="电传机穿孔纸带识读与数字修复 API",
    description="接收分段扫描图，校正倾斜/透视，沿走纸孔估算节距并拼接连续孔列，"
                "按 ITA2 或自定义码表解码，定位损伤并枚举修复候选。",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(tapes.router)
app.include_router(codetables.router)
app.include_router(jobs.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
