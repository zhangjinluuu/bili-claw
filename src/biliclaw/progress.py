"""终端进度展示（FR-11）。

对 rich 的一层薄封装，目的是让下载器不必知道自己是否在输出进度——
``--quiet`` 或非 TTY 环境下所有方法都退化成空操作。
"""

from __future__ import annotations

from typing import Self

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

_TASK_ID = TaskID | None


class ProgressReporter:
    """下载进度条。

    典型用法::

        with ProgressReporter(console, enabled=not quiet) as progress:
            task = progress.add("下载视频流", total=size)
            await downloader.download(..., on_progress=lambda n: progress.advance(task, n))
    """

    def __init__(self, console: Console, *, enabled: bool = True) -> None:
        self._console = console
        self._enabled = enabled
        self._progress: Progress | None = None

    def __enter__(self) -> Self:
        if self._enabled:
            self._progress = Progress(
                TextColumn("[bold]{task.description}"),
                BarColumn(bar_width=None),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=self._console,
                transient=False,
            )
            self._progress.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None

    @property
    def active(self) -> bool:
        return self._progress is not None

    def add(self, description: str, *, total: int | None = None) -> _TASK_ID:
        if self._progress is None:
            return None
        return self._progress.add_task(description, total=total)

    def advance(self, task: _TASK_ID, amount: int) -> None:
        if self._progress is not None and task is not None and amount:
            self._progress.update(task, advance=amount)

    def set_completed(self, task: _TASK_ID, completed: int) -> None:
        """按**累计值**校正进度。

        下载回调给的是增量（``advance``），而合并回调给的是"已经写了多少字节"——
        让调用方自己记上一个值再相减，是在每一步都重新算一遍同样的账。
        """
        if self._progress is not None and task is not None:
            self._progress.update(task, completed=completed)

    def set_total(self, task: _TASK_ID, total: int) -> None:
        """探测到真实长度后校正总数。"""
        if self._progress is not None and task is not None and total > 0:
            self._progress.update(task, total=total)

    def set_description(self, task: _TASK_ID, description: str) -> None:
        if self._progress is not None and task is not None:
            self._progress.update(task, description=description)

    def complete(self, task: _TASK_ID) -> None:
        if self._progress is not None and task is not None:
            self._progress.update(task, completed=self._progress.tasks[task].total or 0)

    def print(self, *args: object, **kwargs: object) -> None:
        """在进度条之外打印一行；进度条存在时交给 rich 协调，避免刷屏错乱。"""
        if self._progress is not None:
            self._progress.console.print(*args, **kwargs)
        else:
            self._console.print(*args, **kwargs)
