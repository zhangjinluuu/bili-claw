"""PyInstaller 的显式入口。

为什么不用 ``-m biliclaw.cli``：``-m`` 会让 PyInstaller 以 runpy 的方式把包塞进
``__main__``，冻结之后 ``sys.modules`` 里会出现两份 ``__main__`` 语义，遇到
「以模块身份再导入一次」的代码就会出岔子。一个只负责调用 ``main()`` 的真模块最省心，
也让构建脚本里的入口路径与 ``pyproject.toml`` 的 ``[project.scripts]`` 保持同源。

``main()`` 自己不返回退出码——它把领域异常翻译成提示后用 ``SystemExit`` 抛出，
所以这里不需要再包一层 try/except，``raise SystemExit`` 会原样传出去。
"""

from __future__ import annotations

import sys

from biliclaw.cli import main

if __name__ == "__main__":
    main()
    sys.exit(0)
