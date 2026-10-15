"""``--check`` 里那个"现场合并一次"的自检。

内置样本是**冻结**的，所以这里要同时守住两件事：自检现在是通的，以及自检真的会
在坏掉的时候报错——一个永远返回 ``None`` 的自检比没有自检更糟，它会给出虚假的
安心。

重新生成内置样本（改了 ``fmp4.py`` 的盒式布局、或改了 ``mp4.py`` 的产物形态时才需要）：：

    .venv/bin/python - <<'PY'
    import base64, hashlib, pathlib, sys
    sys.path.insert(0, "tests")
    import fmp4
    from biliclaw import mp4

    video = fmp4.build(handler=b"vide", codec=b"avc1", timescale=16000,
                       sizes=[120, 80, 64, 40], durations=[512] * 4, ctos=[0, 256, 512, 768],
                       media_time=1600, width=640, height=360, per_fragment=2, seed=1)
    audio = fmp4.build(handler=b"soun", codec=b"mp4a", timescale=44100,
                       sizes=[40, 32, 48, 28], durations=[1024] * 4, ctos=[0] * 4,
                       media_time=0, width=0, height=0, per_fragment=2, seed=2)
    # 把两段 blob 的 base64 贴回 _VIDEO_B64 / _AUDIO_B64，
    # 再用 selftest._payload_of 算出摘要贴回 _EXPECTED_DIGEST。
    PY
"""

from __future__ import annotations

import base64

import pytest

from biliclaw import mp4, selftest


def test_builtin_samples_are_readable_fragments(tmp_path) -> None:
    """样本本身要能被内核解析——不然自检失败时说不清是样本坏了还是内核坏了。"""
    for name, encoded, handler in (
        ("video", selftest._VIDEO_B64, b"vide"),
        ("audio", selftest._AUDIO_B64, b"soun"),
    ):
        path = tmp_path / f"{name}.m4s"
        path.write_bytes(base64.b64decode(encoded))
        source = mp4.parse(path)
        try:
            assert source.handler == handler
            assert len(source.samples) == 4, f"{name} 样本的样本数变了，摘要要跟着重算"
            assert len(source.ftyp) > 0
        finally:
            source.close()


def test_check_merge_passes_on_a_healthy_build() -> None:
    assert selftest.check_merge() is None


def test_check_merge_reports_a_broken_kernel(monkeypatch) -> None:
    """合并内核写出来的东西不对时，自检必须报错而不是沉默。

    这是这个模块存在的全部理由：装到用户机器上的构建如果坏了，``--check`` 是最后
    一道能自动发现它的关口。
    """

    def broken(**kwargs):
        kwargs["dest"].write_bytes(b"\x00" * 64)
        return kwargs["dest"]

    monkeypatch.setattr(mp4, "merge", broken)
    failure = selftest.check_merge()
    assert failure is not None
    assert isinstance(failure, str)


def test_check_merge_survives_an_exception(monkeypatch) -> None:
    """自检不能把 ``--check`` 带崩——它的职责是报告，不是中断。"""

    def explode(**kwargs):
        raise OSError("临时目录不可写")

    monkeypatch.setattr(mp4, "merge", explode)
    failure = selftest.check_merge()
    assert failure is not None
    assert "OSError" in failure and "临时目录不可写" in failure


def test_payload_digest_is_pinned_to_the_samples() -> None:
    """摘要是这个自检里唯一"有牙齿"的断言，别让它退化成随便一个值。"""
    assert len(selftest._EXPECTED_DIGEST) == 64
    assert selftest._EXPECTED_DIGEST != "0" * 64
    assert selftest._EXPECTED_PAYLOAD == 120 + 80 + 64 + 40 + 40 + 32 + 48 + 28


@pytest.mark.parametrize("blob", [b"", b"\x00\x00\x00\x04mdat", b"\x00\x00\x00\x40mdat\x00"])
def test_payload_of_rejects_truncated_input(blob: bytes) -> None:
    """顶层盒子走不完时必须认输，不能把半截数据当成有效载荷。"""
    assert selftest._payload_of(blob) is None


# --- --selftest 这个命令行开关 ------------------------------------------------
# 它存在的唯一理由是给构建脚本判读，所以这里断言的是「构建脚本能怎么读它」。


def test_selftest_flag_prints_ascii_and_exits_zero() -> None:
    """``--selftest`` 的输出必须**全是 ASCII**，且以退出码给结论。

    Windows CI 上真的栽过一次：构建脚本原本跑 ``--check`` 再 ``grep "自检通过"``，
    而 Windows 控制台的默认代码页不是 UTF-8，那几个汉字输出后 grep 永远不匹配，
    于是整个 job 失败——失败点看起来还像"合并内核坏了"，排查方向完全是错的。

    退出码是三个平台判读方式唯一一致的信号；ASCII 是让它连输出都不用读。
    """
    import typer.testing

    from biliclaw.cli import app

    result = typer.testing.CliRunner().invoke(app, ["--selftest"])
    assert result.exit_code == 0
    assert result.stdout.strip().isascii()
    assert "OK" in result.stdout


def test_selftest_flag_exits_6_when_the_kernel_is_broken(monkeypatch) -> None:
    """合并内核坏了时，构建脚本必须能靠退出码发现——这是它在 CI 里的全部意义。"""
    import typer.testing

    from biliclaw import mp4
    from biliclaw.cli import app

    def broken(**kwargs):
        kwargs["dest"].write_bytes(b"\x00" * 64)
        return kwargs["dest"]

    monkeypatch.setattr(mp4, "merge", broken)
    result = typer.testing.CliRunner().invoke(app, ["--selftest"])
    assert result.exit_code == 6  # ExitCode.MISSING_DEPENDENCY：安装包本身的问题

