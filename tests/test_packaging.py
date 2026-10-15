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
import shlex
import shutil
import subprocess
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
    """`.gitattributes` 必须把 `*.sh` 钉成 LF。

    **这条不是任何已发生故障的根因，别把它当解释用。** 曾经把它当成 Windows 构建
    失败的根因写进过注释，那个诊断是错的：`414d3e88` 那次运行发生在 `.gitattributes`
    存在**之前**，而 Windows 的 annotation 里有

        bclaw-build[windows-x64] platform detected: uname -s='MINGW64_NT-10.0-26100'

    ——脚本在 Windows 上跑起来了。加了 `.gitattributes` 之后的那次运行失败在同一处，
    一字不差，等于做了一次干净的对照实验，把换行符这个假设排除掉了。

    留着这条规则是因为它防的是**另一类**问题：git 的 `core.autocrlf` 在不同机器上
    取值不同，某个人的检出真的会得到 CRLF 的 shell 脚本，而症状是

        build.sh: line 19: : command not found
        build.sh: line 19: set: pipefail: invalid option name

    ——错误信息里既没有 "windows" 也没有任何指向换行符的线索。CI 上恰好没踩到，不代表
    下一个人在别的机器上踩不到；钉死编码是零成本的（本地复现：
    `git show HEAD:packaging/build.sh | sed 's/$/\\r/' > /tmp/b.sh && bash /tmp/b.sh`）。
    """
    assert ATTRIBUTES.is_file(), "缺了 .gitattributes，别的检出可能得到 CRLF 的 shell 脚本"
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
    **那个诊断也是错的**——PyInstaller 冻结后 `sys.stdout.encoding` 恒为 `utf-8`，
    中文照样输出（Windows 那次失败的真正位置在 PyInstaller 的 EXE 阶段，与编码无关，
    见 `test_shell_scripts_are_pinned_to_lf` 里记的那次对照实验）。

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


def _extract_bash_function(name: str) -> str:
    """把 `build.sh` 里的一个函数原样抠出来，好在测试里单独驱动它。"""
    text = (PACKAGING / "build.sh").read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    return text[start : text.index("\n}\n", start) + 3]


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash 才能驱动 shell 函数")
def test_annotation_tail_escapes_workflow_commands_correctly(tmp_path: Path) -> None:
    """日志尾部转成 annotation 时的转义必须干净：`%` → `%25`，换行 → `%0A`。

    这个函数**只在 CI 失败的那一刻执行**，本地正常构建永远走不到，所以它坏掉不会
    有任何征兆——直到最需要它的那次失败。两个坑都真踩过：

      * 顺序反了：先转换行再转 `%`，新写出来的 `%0A` 会被自己再转一遍变成 `%250A`，
        annotation 里就是一堆乱码，等于白做。
      * `${text//'%'/'%25'}` 里的引号是**字面量**，`%` 会变成 `'%25'`（引号进结果）。

    还要守住最关键的一条：**得从报错那一行开始截**，而不是死板取最后 N 行。
    PyInstaller 失败时 traceback 前面压着几十行 INFO，只取尾部恰好会把原因切掉——
    Windows 那次连续两轮什么都没查出来，就是这个原因。
    """
    log = tmp_path / "build.log"
    log.write_text(
        "".join(f"{i} INFO: progress 100% done\n" for i in range(1, 31))
        + "Traceback (most recent call last):\n"
        + '  File "x.py", line 1\n'
        + "PermissionError: [WinError 5] Access is denied\n",
        encoding="utf-8",
    )
    driver = tmp_path / "drive.sh"
    driver.write_text(
        "set -euo pipefail\n" + _extract_bash_function("annotation_tail") + '\nannotation_tail "$1" 25\n',
        encoding="utf-8",
    )

    result = subprocess.run(["bash", str(driver), str(log)], capture_output=True, text=True, check=True)
    out = result.stdout

    assert "Traceback" in out and "WinError 5" in out, "报错那一段被切掉了"
    assert "%250A" not in out, "换行被二次转义了：`%0A` 自己又被转了一遍"
    assert "100%25 done" in out, "真正的 `%` 必须转义成 `%25`，否则 annotation 会被截断"
    assert "'" not in out, "参数展开里的引号是字面量，% 会变成 '%25'"
    assert "%0A" in out, "换行必须编码成 %0A，否则 annotation 只有第一行"


#: 一整行里 `#` 之前的部分，用来把行尾注释也剔掉。
_CODE_BEFORE_COMMENT = re.compile(r"^[^#]*")
#: `printf '<格式串>'` 里的格式串。
_PRINTF_FORMAT = re.compile(r"printf\s+'((?:[^'\\]|\\.)*)'")
#: 三引号块（模块 docstring 之类）。它只是字符串字面量，永远不会被打印。
_DOCSTRING = re.compile(r'""".*?"""', re.DOTALL)
#: printf 里真正吃参数的转换说明，例如 `%s` / `%0A` / `%-20s`。`%%` 不算。
_CONVERSION = re.compile(r"%(?!%)[-+ #0']*\d*(?:\.\d+)?[a-zA-Z]")


def _without_comments(text: str) -> str:
    """整行注释**与行尾注释**都剔掉。

    行尾注释也得剔：`from biliclaw import __version__  # noqa: E402 - 中文说明` 这种
    写法在 spec 里就有，只剔整行会把中文说明留下来，断言当场就红。

    不处理"`#` 出现在字符串字面量里"的情形——这里只用来**减少**检查范围，剔多了
    顶多少查一点，不会误报。
    """
    return "\n".join(_CODE_BEFORE_COMMENT.match(line).group(0) for line in text.splitlines())


def _without_comments_or_docstrings(text: str) -> str:
    """再去掉三引号块。

    spec 顶部的模块 docstring 是整篇中文说明——它只是字符串字面量，永远不会被打印
    到 stdout，留在检查范围里只会逼着人把注释写成英文（第一版就是这么误报的）。
    去掉之后，剩下的才是真正会被执行、可能产生输出的代码。
    """
    return _without_comments(_DOCSTRING.sub("", text))


def test_spec_source_is_pure_ascii_outside_comments() -> None:
    """`bclaw.spec` 里会被执行的代码必须是纯 ASCII。

    **这就是 Windows 构建失败的真正原因**，而且它伪装得极好。spec 是被 PyInstaller
    在自己的进程里 exec 的，那个进程在 Windows 上的 stdout 编码是 cp1252。spec 末尾
    原本写着：

        print(f"BiliClaw {__version__} → {exe.name}")

    `→`（U+2192）在 cp1252 里没有对应字符，于是抛：

        File "packaging/bclaw.spec", line 98, in <module>
          print(f"BiliClaw {__version__} → {exe.name}")
        UnicodeEncodeError: 'charmap' codec can't encode character '\\u2192'
        in position 15: character maps to <undefined>

    要命的是位置：它发生在**构建已经成功之后**。PYZ 与 PKG 都已建好，PyInstaller 的
    日志正好停在 `checking EXE` 那一行，于是现场看起来像 EXE 阶段崩了——而
    `EXE.assemble()` 其实压根没开始。五个平台里只有 Windows 挂，因为只有它的控制台
    代码页不是 UTF-8。查了好几轮才落到这一行上。

    `build.sh` 里那些 `::notice::` / `::error::` 标记同为 ASCII，理由是同一个。
    """
    spec = (PACKAGING / "bclaw.spec").read_text(encoding="utf-8")
    offenders = [
        f"bclaw.spec:{lineno}: {line.strip()}"
        for lineno, line in enumerate(_without_comments_or_docstrings(spec).splitlines(), 1)
        if line.strip() and not line.isascii()
    ]
    assert not offenders, (
        "spec 会被执行的代码里有非 ASCII 字符——Windows 上 stdout 是 cp1252，"
        "打印它会抛 UnicodeEncodeError，而且现场看起来像是别的地方崩了：\n  "
        + "\n  ".join(offenders)
    )


def test_printf_format_strings_never_swallow_their_argument() -> None:
    """`printf` 的格式串必须能吃下自己的参数。

    真正的写法是 `printf '::error::build log:%%0A%s\\n' "$text"`。少写一个 `%` 时，
    `%0A` **不是"换行的转义"**，而是 printf 自己的格式说明符——`%A` 是十六进制浮点，
    前面的 `0` 是补零标志。于是 `$text` 被当成浮点数解析掉了，annotation 里只剩下：

        build log:0X0P+0

    本该是最有用的一条注解，结果比没有还糟：连"日志没取到"这个信息都没给出来。
    本地演示：

        $ printf 'A) %0A%s\\n' "正文"
        A) 0X0P+0
        bash: printf: 正文: invalid number

    只检查**含转换说明**的格式串：`printf '\\n把它加进 PATH：\\n'` 这种纯字面量输出
    本来就不吃参数，要求它"保住 sentinel"是测试自己搞错了。

    这条用**真的 bash 跑一遍**而不是查字符串，因为要判的正是 printf 的解析行为。
    """
    checked = 0
    for path in [WORKFLOW, *_shell_scripts()]:
        text = _strip_comments(path.read_text(encoding="utf-8"))
        for fmt in _PRINTF_FORMAT.findall(text):
            if not _CONVERSION.search(fmt):
                continue  # 纯字面量，无参数可吃
            sentinel = "SENTINEL-argument-survived"
            result = subprocess.run(
                ["bash", "-c", f"printf {shlex.quote(fmt)} {shlex.quote(sentinel)}"],
                capture_output=True,
                text=True,
                check=False,
            )
            checked += 1
            assert sentinel in result.stdout, (
                f"{path.name}: printf 格式串把参数吃掉了：{fmt!r}\n"
                f"  输出：{result.stdout!r}\n"
                f"  stderr：{result.stderr.strip()}"
            )

    assert checked >= 5, f"只检查到 {checked} 个带参数的 printf 格式串——正则可能失效了"
