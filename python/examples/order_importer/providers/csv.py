from __future__ import annotations

import asyncio
import csv

from cordis import Context, plugin

from ..definitions import (
    IMPORTER,
    STORE,
    VALIDATOR,
    ImportReport,
    ImportRequest,
    OrderStore,
    OrderValidator,
    RowIssue,
    ValidateOrderRequest,
)


class CsvImporter:
    def __init__(
        self, owner: Context, validator: OrderValidator, store: OrderStore
    ) -> None:
        self.owner = owner
        self.validator = validator
        self.store = store

    def submit(self, request: ImportRequest) -> asyncio.Task[ImportReport]:
        return self.owner.spawn(
            self._import(request),
            policy="drain",
            timeout=5,
            name=f"import:{request.path.name}",
        )

    async def _import(self, request: ImportRequest) -> ImportReport:
        path = request.path
        accepted = 0
        issues: list[RowIssue] = []
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.reader(stream, strict=True)
            if next(reader, None) != ["order_id", "customer", "amount"]:
                raise ValueError(f"{path.name}: 表头必须是 order_id,customer,amount")
            for row in reader:
                try:
                    if len(row) != 3:
                        raise ValueError("每行必须有三列")
                    order = self.validator.validate(
                        ValidateOrderRequest(row[0], row[1], row[2])
                    )
                except ValueError as error:
                    issues.append(RowIssue(reader.line_num, str(error)))
                else:
                    self.store.save(order)
                    accepted += 1
                # Yield between rows so config changes and teardown can progress.
                await asyncio.sleep(0)
        report = ImportReport(path, accepted, tuple(issues))
        return report


@plugin(inject=[STORE, VALIDATOR], provide=[IMPORTER])
def importer(ctx: Context, _config: None) -> None:
    ctx.provide(IMPORTER, CsvImporter(ctx, ctx.service(VALIDATOR), ctx.service(STORE)))
