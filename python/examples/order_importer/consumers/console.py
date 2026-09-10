from pathlib import Path

from cordis import Context, plugin

from ..definitions import ImportReport


@plugin
def console(ctx: Context, _config: None) -> None:
    def completed(report: ImportReport) -> None:
        print(
            f"[导入] {report.path.name}: "
            f"成功 {report.accepted} 行，拒绝 {len(report.rejected)} 行",
            flush=True,
        )
        for issue in report.rejected:
            print(f"  第 {issue.line} 行: {issue.reason}", flush=True)

    def failed(path: Path, error: Exception) -> None:
        print(f"[导入失败] {path.name}: {error}", flush=True)

    ctx.on("orders/imported", completed)
    ctx.on("orders/import-failed", failed)
