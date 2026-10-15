"""打包脚本与 CI 配置的静态检查。

这些文件平时不参与 `pytest` 的主流程——它们只在发版时跑一次，所以坏了也要等到发版
那一刻才发现。而发版那一刻恰恰是最不适合debugging的时候（出错的是别人机器上的
`unbound variable`，或者一个连日志都没有的 `queued`）。

这里把两次真实事故固化成断言，代价是几行正则，收益是"不用靠人记得"。

**注意所有断言都要先剥掉注释**：这些文件的注释里大量出现"不要写 macos-13""而不是
ubuntu-latest"这类反面教材，不剥的话每一条断言都会被自己的警告文案绊倒。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
ATTRIBUTES = ROOT / ".gitattributes"

#: `$NAME` / `${NAME}` 的写法，用于判断变量名后面紧跟了什么。
_VARIABLE = re.compile(r"(?<![\\$])\$\{?([A-Za-z_]\w*)\}?")
#: matrix 里的一行：`- { runner: macos-14, tag: macos-arm64 }`
_MATRIX_ENTRY = re.compile(r"runner:\s*([\w.\-]+)\s*,\s*tag:\s*([\w.\-]+)")


def _strip_comments(text: str) -> str:
    """去掉整行注释。这些文件的注释里写满了"别这么干"的警告，必须排除。"""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _shell_scripts() -> list[Path]:
    return sorted(PACKAGING.glob("*.sh"))


def _build_job() -> str:
    """只取 `build` job 那一段——`publish` job 用 `ubuntu-latest` 是无害的。"""
    return _strip_comments(WORKFLOW.read_text(encoding="utf-8")).split("\n  publish:", 1)[0]


def test_the_files_under_test_still_exist() -> None:
    """脚本被删掉或改名时，下面几条用例会"通过"——先把前提钉住。"""
    assert [p.name for p in _shell_scripts()] == ["build.sh", "install.sh"]
    assert WORKFLOW.is_file()


@pytest.mark.parametrize("path", _shell_scripts(), ids=lambda p: p.name)
def test_no_variable_is_glued_to_a_non_ascii_character(path: Path) -> None:
    """`$VAR` 后面不能紧跟非 ASCII 字符。

    这个坑在 `packaging/build.sh` 里踩过两次，症状一模一样：

        echo "▸ 产物：$BIN（...）"       → build.sh: line 87: BIN（: unbound variable
        echo "  ✓ ...（$SELFTEST_OUT）"   → build.sh: line 107: SELFTEST_OUT）: unbound variable

    bash 的变量名只认 ASCII，但它**不会在非 ASCII 字节处停下**——那几个 UTF-8
    字节被并进变量名，于是 `set -u` 报一个名字里带全角括号的"未定义变量"。消息
    看着莫名其妙，实际上只是缺了一对花括号。

    两种修法都接受：写 `${VAR}`，或者不让全角字符跟在变量后面。
    """
    text = _strip_comments(path.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for lineno, line in enumerate(text.splitlines(), 1):
        for match in _VARIABLE.finditer(line):
            if line[match.start() : match.start() + 2] == "${":
                continue  # 已经加了花括号
            rest = line[match.end() :]
            if rest and not rest[0].isascii():
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")

    assert not offenders, (
        "变量后面紧跟了非 ASCII 字符，bash 会把它并进变量名（加花括号即可）：\n  "
        + "\n  ".join(offenders)
    )


def test_shell_scripts_are_pinned_to_lf() -> None:
    """`.gitattributes` 必须把 `*.sh` 钉成 LF——这是 Windows 构建失败的真正根因。

    Windows runner 的系统 git 配置是 `core.autocrlf=true`，没有 `.gitattributes`
    时检出会得到 CRLF 的 `build.sh`。bash 随后把回车当成选项名的一部分：

        build.sh: line 19: : command not found
        build.sh: line 19: set: pipefail: invalid option name

    脚本在第三行就死了，错误信息里既没有 "windows"，也没有任何指向换行符的线索——
    四个平台里只有 Windows 挂，而且挂得完全看不出原因。本地能复现：

        git show HEAD:packaging/build.sh | sed 's/$/\\r/' > /tmp/b.sh && bash /tmp/b.sh

    （这条断言和上面那条"用 --selftest 不用 --check"曾经指向同一个故障，但那条
    的诊断是错的：PyInstaller 冻结后的 `sys.stdout.encoding` 恒为 utf-8，中文输出
    在 Windows 上并不会丢。真正的原因是换行符。--selftest 仍然保留，理由变成了
    "它是给脚本判读的信号：纯 ASCII、不碰网络、退出码即结论"。）
    """
    assert ATTRIBUTES.is_file(), "缺了 .gitattributes，Windows 检出会得到 CRLF 的 shell 脚本"
    text = _strip_comments(ATTRIBUTES.read_text(encoding="utf-8"))
    assert re.search(r"^\*\.sh\s+text\s+eol=lf\s*$", text, re.MULTILINE), (
        "`*.sh` 必须显式钉成 `text eol=lf`；只靠 `* text=auto` 挡不住 autocrlf=true"
    )


@pytest.mark.parametrize("path", _shell_scripts(), ids=lambda p: p.name)
def test_shell_scripts_in_the_working_tree_have_no_carriage_return(path: Path) -> None:
    """工作区里的 shell 脚本此刻就必须是 LF——CRLF 的话它根本跑不起来。

    上一条断言守的是"规则还在"，这一条守的是"规则真的生效了"。`.gitattributes`
    写对了但没被应用（比如文件加得太晚、或本地 `core.autocrlf` 已经改过工作区）
    时，测试会在这里红，而不是等到 Windows CI 上。
    """
    raw = path.read_bytes()
    assert b"\r\n" not in raw, f"{path.name} 含 CRLF 换行，bash 会在 set -euo pipefail 那行直接死掉"


def test_build_smoke_test_uses_selftest_not_check() -> None:
    """冒烟测试必须用 `--selftest`，不能靠 grep `--check` 的中文输出。

    早期版本是 `"$BIN" --check | grep -q "自检通过"`。那个写法有两处不好：判读依赖
    人读的文案（文案一改，CI 就红），而且 Windows 上真的整个 job 失败过一次。

    当时把原因归结为"Windows 控制台代码页不是 UTF-8，grep 匹配不上中文"，事后证明
    **那个诊断是错的**——PyInstaller 冻结后 `sys.stdout.encoding` 恒为 `utf-8`，
    中文照样输出（真正的故障是 `.gitattributes` 缺位导致的 CRLF，见上）。

    但 `--selftest` 依然该留着，理由换成了更站得住的那个：它是**给脚本判读的信号**，
    不是给人读的文案——纯 ASCII、不碰网络、退出码就是结论，三个平台的判读方式完全
    一致。文案怎么改都不会影响 CI。
    """
    text = _strip_comments((PACKAGING / "build.sh").read_text(encoding="utf-8"))
    assert "--selftest" in text
    assert "--check" not in text, "构建脚本不该再依赖 --check 的输出"


def test_ci_matrix_covers_the_five_supported_targets() -> None:
    """五个平台一个都不能少，且标签必须是真实存在、当前可用的 runner。

    `macos-13` 已被 GitHub 下线，但**标签还在、语法也合法**：它不报错，只是永远
    等不到机器，症状是整个 run 停在 `queued`（不是失败！），`publish` 因此永不
    触发，日志里一个字都没有。发版时在这上面空等了 40 分钟，值得一条断言。
    """
    found = dict((tag, runner) for runner, tag in _MATRIX_ENTRY.findall(_build_job()))
    assert found == {
        "macos-arm64": "macos-14",
        "macos-x64": "macos-15-intel",
        "windows-x64": "windows-latest",
        "linux-x64": "ubuntu-22.04",
        "linux-arm64": "ubuntu-24.04-arm",
    }


def test_linux_x64_is_pinned_to_the_oldest_lts() -> None:
    """glibc 向前兼容、向后不兼容：钉老了能跑新的，钉新了老用户全废。

    只约束 build job——`publish` 用 `ubuntu-latest` 没问题，它不产出任何东西。
    """
    assert "ubuntu-latest" not in _build_job()
