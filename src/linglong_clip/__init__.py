"""玲珑剪切板 (Linglong Clipboard) —— 跨平台智能剪切板。"""

__version__ = "1.0.3"
#: 作者与联系渠道。
#:
#: 放在这里而不是只写在 README：装成安装版的用户手里没有 README，
#: 「这是谁做的、去哪反馈」只能从「关于」页看到。两处各写一份迟早分叉，
#: 所以界面与文档都从这里取。
AUTHOR = "科技酱"

#: (显示名, 链接)。顺序即界面展示顺序。
AUTHOR_CHANNELS = (
    ("主站", "https://www.asoe.cn"),
    ("技术文档", "https://docs.asoe.cn"),
    ("GitHub", "https://github.com/techjiang/"),
    ("哔哩哔哩", "https://space.bilibili.com/1768832152"),
    ("玲珑论坛", "https://forums.asoe.cn/"),
    ("邮箱", "official@tech.asoe.cn"),
)

#: QQ 群，界面里单独一行显示
AUTHOR_QQ_GROUPS = ("291974598", "474819022", "631167288")

__all__ = [
    "__version__",
    "AUTHOR",
    "AUTHOR_CHANNELS",
    "AUTHOR_QQ_GROUPS",
]
