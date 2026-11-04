"""互操作 API：板块文件导出/导入、选股公式生成、信号扫描。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quantdesk.core.errors import QuantDeskError
from quantdesk.services import interop
from quantdesk.services.interop import SignalRule
from quantdesk.store.parquet_store import ParquetStore

router = APIRouter(prefix="/api/interop", tags=["interop"])


class BlockExportRequest(BaseModel):
    name: str = Field(description="板块名（将成为文件名，仅中英文数字下划线短横）")
    symbols: list[str] = Field(default_factory=list, description='如 ["600000.SH"]')
    from_watchlist: bool = Field(default=False, description="true=以自选股为源")
    targets: list[str] = Field(default_factory=lambda: ["tdx"],
                              description="tdx / ths / em，可多选")
    write_to_client: bool = Field(default=False, description="是否同时写入客户端安装目录")
    bse_prefix: str = Field(default="", description="通达信北交所市场位（留空则跳过北交所标的）")


class ImportBlockRequest(BaseModel):
    target: str = Field(description="tdx / ths / em")
    content: str = Field(description="板块文件文本内容（从客户端复制出来粘贴）")


class SignalScanRequest(BaseModel):
    kind: str = Field(description="ma_cross / macd_golden / rsi_threshold / volume_spike")
    params: dict = Field(default_factory=dict)
    symbols: list[str] = Field(default_factory=list)
    from_watchlist: bool = False


class FormulaRequest(BaseModel):
    kind: str
    params: dict = Field(default_factory=dict)
    dialect: str = Field(default="tdx", description="tdx / ths")


def _symbols(req: BlockExportRequest | SignalScanRequest) -> list[str]:
    from quantdesk.services.market_data import get_market_service

    syms = list(req.symbols)
    if req.from_watchlist:
        syms += [w["symbol"] for w in get_market_service().watchlist()]
    out, seen = [], set()
    for s in syms:
        if s not in seen:
            seen.add(s)
            out.append(s)
    if not out:
        raise HTTPException(status_code=400, detail="标的列表为空（手工传入或勾选「以自选股为源」）")
    return out


@router.get("/targets")
def targets() -> dict:
    return {
        "blocks": interop.block_targets(),
        "rules": [{"kind": k, "title": v.title} for k, v in interop.RULE_KINDS.items()],
        "formula_dialects": list(interop._DIALECTS),   # noqa: SLF001 — UI 需要这份枚举
        "exports_dir": str(interop.export_dir()),
    }


@router.post("/export/block")
def export_block(req: BlockExportRequest) -> dict:
    symbols = _symbols(req)
    results = []
    skipped_bse: list[str] = []
    for target in req.targets:
        try:
            if target == "tdx":
                content = interop.export_tdx_block(symbols, bse_prefix=req.bse_prefix)
                if not req.bse_prefix:
                    skipped_bse = [s for s in symbols if s.endswith(".BJ")]
            elif target in ("ths", "em"):
                content = interop.export_ths_block(symbols)
            else:
                raise interop.InteropError(f"未知导出目标: {target}")
            results.append(interop.write_block(req.name, content, target,
                                               write_to_client=req.write_to_client))
        except (interop.InteropError, UnicodeEncodeError, OSError) as e:
            raise HTTPException(status_code=400, detail=f"{target} 导出失败: {e}") from e
    return {"count": len(symbols), "files": results,
            "skipped_bse": skipped_bse,
            "note": "通达信板块文件为 GBK 编码；导入路径见 GET /api/interop/targets"}


@router.post("/import/block")
def import_block(req: ImportBlockRequest) -> dict:
    if req.target == "tdx":
        symbols = interop.parse_tdx_block(req.content)
    elif req.target in ("ths", "em"):
        symbols = interop.parse_bare_block(req.content)
    else:
        raise HTTPException(status_code=400, detail=f"未知导入格式: {req.target}")
    if not symbols:
        raise HTTPException(status_code=400, detail="未解析出任何标的（检查格式：通达信 7 位/其余 6 位代码）")
    from quantdesk.services.market_data import get_market_service

    svc = get_market_service()
    before = {w["symbol"] for w in svc.watchlist()}
    for s in symbols:
        svc.watchlist_add(s, name="", note="客户端板块导入")
    after = {w["symbol"] for w in svc.watchlist()}
    return {"parsed": len(symbols), "added": sorted(after - before),
            "already_present": sorted(set(symbols) & before)}


@router.get("/download")
def download(name: str) -> dict:
    """导出件下载（只允许 exports 目录内的文件，名称严格校验防路径穿越）。"""
    d = interop.export_dir().resolve()
    candidate = (d / name).resolve()
    if candidate.parent != d or not candidate.is_file():
        raise HTTPException(status_code=404, detail="文件不存在或不在导出目录")
    from fastapi.responses import FileResponse

    content = candidate.read_bytes()
    try:
        text = content.decode("gbk")
    except UnicodeDecodeError:
        text = content.decode("utf-8", errors="replace")
    return {"name": name, "encoding": "gbk" if candidate.suffix == ".blk" else "utf-8",
            "content": text, "bytes": len(content)}


@router.post("/formula")
def formula(req: FormulaRequest) -> dict:
    try:
        rule = SignalRule(kind=req.kind, params=dict(req.params))
        return {"kind": rule.kind, "title": rule.title, "dialect": req.dialect,
                "formula": interop.compile_formula(rule, req.dialect)}
    except (interop.InteropError, QuantDeskError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/signals")
def signals(req: SignalScanRequest) -> dict:
    """在本地 K 线上扫描规则命中（与导出的客户端公式同源）。"""
    try:
        rule = SignalRule(kind=req.kind, params=dict(req.params))
    except interop.InteropError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    symbols = _symbols(req)
    if not symbols:
        raise HTTPException(status_code=400,
                            detail="未指定标的：symbols 为空且自选股里没有内容（先在行情页加自选）")
    out = interop.scan_signal_detail(ParquetStore(), rule, symbols)
    return {"rule": {"kind": rule.kind, "params": rule.params, "title": rule.title}, **out}
