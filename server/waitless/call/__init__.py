"""通话引擎（D 线；textcall.py 归 E 线；line_protocol.py 共享冻结）。

本包的 __init__ 不导入任何子模块：text-sim 不能因为导入 waitless.call 而加载 Pipecat（AC-87）。
"""
